"""Engine layer - every interaction with the INNER dockerd (dind).

Design rules:
  * The API ONLY touches objects labeled sandbox.managed=true. Anything a
    user created by hand inside dind is invisible to us (edge case #5).
  * All docker SDK calls raise EngineDown on connection/daemon errors;
    the API layer maps that to 503 + Retry-After (edge case #8).
  * Creation is rollback-safe: any failure after partial progress tears
    down everything this sandbox created (edge cases #2, #3).
  * reconcile() runs at boot and is the crash-recovery path (edge case #4).
"""
import logging
import time
import urllib.request

import docker
import docker.errors

from . import config, store, templates

log = logging.getLogger("sandbox-api.engine")


class EngineDown(Exception):
    """The inner daemon is unreachable/unhealthy."""


def _client() -> docker.DockerClient:
    try:
        return docker.DockerClient(base_url=config.DOCKER_HOST,
                                   timeout=config.DOCKER_TIMEOUT)
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e


def ping() -> bool:
    try:
        _client().ping()
        return True
    except Exception:
        return False


def labels_for(sid: str, name: str, template: str) -> dict[str, str]:
    return {
        config.LABEL_MANAGED: "true",
        config.LABEL_ID: sid,
        config.LABEL_NAME: name,
        config.LABEL_TEMPLATE: template,
    }


# -- image preparation --------------------------------------------------------

def ensure_image(client: docker.DockerClient, image: str, log_event) -> None:
    """Guarantee `image` exists in the inner engine.

    Buildable images are built inside dind from an API-uploaded tar context
    (cached afterwards); anything else is pulled. Both are idempotent.
    """
    try:
        client.images.get(image)
        return
    except docker.errors.ImageNotFound:
        pass
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e

    ctx = templates.build_context_for(image)
    try:
        if ctx is not None:
            log_event("build", f"building {image} inside dind (uploaded context)")
            client.images.build(fileobj=ctx, custom_context=True,
                                tag=image, rm=True, pull=True)
        else:
            log_event("pull", f"pulling {image} into dind")
            client.images.pull(image)
    except docker.errors.BuildError as e:
        raise RuntimeError(f"build of {image} failed: {e}") from e
    except docker.errors.APIError as e:
        raise RuntimeError(f"image prep {image} failed: {e}") from e


# -- sandbox lifecycle --------------------------------------------------------

def create_sandbox_resources(sid: str, name: str, template: str,
                             port: int | None, log_event) -> None:
    """Create network + containers for a sandbox. Rolls back on ANY failure.

    On success the sandbox's containers are running (or starting); the
    caller then waits for readiness and marks READY.
    """
    client = _client()
    spec = templates.TEMPLATES[template]["spec"](sid, name, port)
    labels = labels_for(sid, name, template)
    network_name = spec["network"]
    created_containers = []
    network_created = False

    def rollback():
        for c in created_containers:
            try:
                c.remove(force=True)
            except docker.errors.DockerException:
                log.warning("rollback: failed to remove %s", c.name)
        if network_created:
            try:
                client.networks.get(network_name).remove()
            except docker.errors.DockerException:
                log.warning("rollback: failed to remove net %s", network_name)

    try:
        client.networks.create(network_name, driver="bridge",
                               labels=labels, check_duplicate=True)
        network_created = True

        containers_by_name = {}
        for cspec in spec["containers"]:
            # dependency wait (template ordering, cheap depends_on)
            wait = cspec.get("wait_for")
            if wait:
                dep = containers_by_name[wait["container"]]
                _wait_healthy_or_running(client, dep, timeout=120)

            kwargs = {
                "image": cspec["image"],
                "name": cspec["name"],
                "command": cspec.get("command"),
                "detach": True,
                "network": network_name,
                "environment": cspec.get("environment") or {},
                "labels": {**labels, config.LABEL_ROLE: cspec["role"],
                           **({config.LABEL_PORT: str(port)}
                              if cspec.get("port") else {})},
            }
            if cspec.get("ports"):
                kwargs["ports"] = cspec["ports"]
            if cspec.get("healthcheck"):
                hc = cspec["healthcheck"]
                kwargs["healthcheck"] = docker.types.Healthcheck(
                    test=hc["test"], interval=hc["interval"],
                    timeout=hc["timeout"], retries=hc["retries"])
            container = client.containers.create(**kwargs)
            # network aliases (backend needs to resolve as "backend")
            for alias in cspec.get("network_aliases", []):
                client.networks.get(network_name).disconnect(container)
                client.networks.get(network_name).connect(
                    container, aliases=[alias])
            container.start()
            created_containers.append(container)
            containers_by_name[cspec["name"]] = container
            log_event("create", f"container {cspec['name']} started")
    except Exception:
        rollback()
        raise


def _wait_healthy_or_running(client, container, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State", {})
        health = state.get("Health", {}).get("Status")
        if health == "healthy" or (health is None and state.get("Running")):
            return
        if state.get("Status") in ("exited", "dead"):
            raise RuntimeError(
                f"dependency {container.name} exited: "
                f"{container.logs(tail=20).decode(errors='replace')}")
        time.sleep(1)
    raise RuntimeError(f"dependency {container.name} not ready in {timeout}s")


def wait_ready(sid: str, template: str, port: int | None) -> None:
    """Block until the sandbox app answers (or its container is just up)."""
    kind = templates.TEMPLATES[template]["health"]["kind"]
    if kind == "running":
        return
    timeout = (config.READY_TIMEOUT_COREAPP if template == "coreapp"
               else config.READY_TIMEOUT_WEB)
    deadline = time.monotonic() + timeout
    url = f"http://{config.SANDBOX_HOST}:{port}/"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"sandbox did not become ready at {url} in {timeout}s")


def destroy_sandbox_resources(sid: str) -> None:
    """Remove every managed container + the network of a sandbox. Idempotent."""
    client = _client()
    for c in client.containers.list(
            all=True, filters={"label": f"{config.LABEL_ID}={sid}"}):
        try:
            c.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e
    for n in client.networks.list(
            filters={"label": f"{config.LABEL_ID}={sid}"}):
        try:
            n.remove()
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException:
            pass  # best effort; network vanishes with its containers


def engine_sandbox_ids() -> set[str]:
    """Sandbox ids that ACTUALLY exist in the engine (label-derived)."""
    try:
        containers = _client().containers.list(
            all=True, filters={"label": f"{config.LABEL_MANAGED}=true"})
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e
    return {c.labels.get(config.LABEL_ID) for c in containers} - {None}


def engine_port_map() -> dict[str, int]:
    """sandbox.id -> published port, from container labels (engine truth)."""
    out = {}
    try:
        containers = _client().containers.list(
            all=True, filters={"label": f"{config.LABEL_MANAGED}=true"})
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e
    for c in containers:
        sid = c.labels.get(config.LABEL_ID)
        port = c.labels.get(config.LABEL_PORT)
        if sid and port:
            out[sid] = int(port)
    return out


# -- reconciliation (crash recovery, edge case #4) ----------------------------

def reconcile() -> None:
    """Bring the DB in line with the engine after an API restart.

      - engine has it, DB doesn't  -> ADOPT (insert row, state from engine)
      - DB has it, engine doesn't  -> mark DELETED (containers vanished)
      - both                       -> refresh state from engine truth
    Foreign (unmanaged) containers are never looked at.
    """
    client = _client()
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{config.LABEL_MANAGED}=true"})
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e

    by_sandbox: dict[str, list] = {}
    for c in containers:
        sid = c.labels.get(config.LABEL_ID)
        if sid:
            by_sandbox.setdefault(sid, []).append(c)

    db_rows = {r["id"]: r for r in store.list_all(include_deleted=True)}

    for sid, clist in by_sandbox.items():
        any_running = any(c.status == "running" for c in clist)
        engine_state = "READY" if any_running else "STOPPED"
        if sid not in db_rows:
            first = clist[0]
            port = first.labels.get(config.LABEL_PORT)
            store.create({
                "id": sid,
                "name": first.labels.get(config.LABEL_NAME, sid),
                "template": first.labels.get(config.LABEL_TEMPLATE, "unknown"),
                "spec": {"adopted": True},
                "state": engine_state,
                "port": int(port) if port else None,
                "ttl_seconds": None,
            })
            store.add_event(sid, "reconcile",
                            f"adopted from engine as {engine_state}")
        else:
            row = db_rows[sid]
            if row["state"] == "DELETED":
                # crash between engine teardown and DB update, or vice versa
                destroy_sandbox_resources(sid)
                store.add_event(sid, "reconcile",
                                "finished interrupted delete")
            elif row["state"] in ("CREATING",):
                # crashed mid-create: partial resources are unsafe -> roll back
                destroy_sandbox_resources(sid)
                store.set_state(sid, "FAILED", "api restarted mid-create")
                store.add_event(sid, "reconcile",
                                "rolled back interrupted create")
            elif row["state"] != engine_state:
                store.set_state(sid, engine_state)
                store.add_event(sid, "reconcile",
                                f"state {row['state']} -> {engine_state}")

    for sid, row in db_rows.items():
        if sid not in by_sandbox and row["state"] != "DELETED":
            store.set_state(sid, "DELETED")
            store.add_event(sid, "reconcile",
                            "containers vanished while API was down")


# -- introspection (what's INSIDE a sandbox) ----------------------------------

def sandbox_containers(sid: str) -> list[dict]:
    """Live view of the containers inside one sandbox, from the engine."""
    client = _client()
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{config.LABEL_ID}={sid}"})
    except docker.errors.DockerException as e:
        raise EngineDown(str(e)) from e
    out = []
    for c in containers:
        c.reload()
        state = c.attrs.get("State", {})
        out.append({
            "name": c.name,
            "role": c.labels.get(config.LABEL_ROLE, "?"),
            "image": c.image.tags[0] if c.image.tags else "<built>",
            "status": c.status,                       # running | exited | ...
            "health": state.get("Health", {}).get("Status"),  # or None
            "started_at": state.get("StartedAt"),
            "port": c.labels.get(config.LABEL_PORT),
        })
    out.sort(key=lambda x: x["name"])
    return out


# -- exec (edge case #6) ------------------------------------------------------

def exec_in_sandbox(sid: str, cmd: list[str], timeout: int,
                    container_name: str | None = None) -> dict:
    """Run cmd in a sandbox container with timeout + output caps.

    Target selection: explicit `container_name` if given, else the
    role=app container, else any running container of the sandbox.
    """
    client = _client()
    all_containers = client.containers.list(
        all=True, filters={"label": f"{config.LABEL_ID}={sid}"})
    if container_name:
        candidates = [c for c in all_containers
                      if c.name == container_name and c.status == "running"]
        if not candidates:
            raise RuntimeError(
                f"container '{container_name}' not running in this sandbox")
    else:
        candidates = [c for c in all_containers
                      if c.labels.get(config.LABEL_ROLE) == "app"
                      and c.status == "running"]
        if not candidates:
            candidates = [c for c in all_containers if c.status == "running"]
    if not candidates:
        raise RuntimeError("no running container in sandbox")
    container = candidates[0]

    try:
        exec_id = client.api.exec_create(
            container.id, cmd, stdout=True, stderr=True, tty=False)["Id"]
        # socket=True yields a socket.SocketIO wrapper around the raw
        # multiplexed stream. We deliberately read from the RAW socket
        # (._sock): the SocketIO wrapper poisons itself after the first
        # read timeout (OSError on all subsequent reads), while a raw
        # socket recovers fine - and we rely on timeouts to poll for
        # output while enforcing our own deadline.
        sock = client.api.exec_start(exec_id, socket=True, demux=True)
    except docker.errors.APIError as e:
        raise EngineDown(str(e)) from e

    out, err = bytearray(), bytearray()
    buf = bytearray()          # unparsed frame bytes (handles split frames)
    timed_out = False
    truncated = False
    deadline = time.monotonic() + timeout
    raw = sock._sock
    raw.settimeout(1.0)
    try:
        while time.monotonic() < deadline:
            try:
                chunk = raw.recv(65536)
            except (TimeoutError, InterruptedError):
                continue
            except (ConnectionResetError, BrokenPipeError):
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # demux framing: 8-byte header (stream byte + 3 pad + length)
            while len(buf) >= 8:
                stream = buf[0]
                size = int.from_bytes(buf[4:8], "big")
                if len(buf) < 8 + size:
                    break                      # partial frame; wait for more
                payload = bytes(buf[8:8 + size])
                del buf[:8 + size]
                target = out if stream == 1 else err
                if len(target) < config.EXEC_OUTPUT_LIMIT:
                    room = config.EXEC_OUTPUT_LIMIT - len(target)
                    target += payload[:room]
                    if len(payload) > room:
                        truncated = True
                else:
                    truncated = True
        else:
            timed_out = True                   # deadline hit, process may
                                             # still run server-side; we
                                             # detach rather than kill it
    finally:
        try:
            sock.close()
        except OSError:
            pass

    exit_code = None
    if not timed_out:
        try:
            exit_code = client.api.exec_inspect(exec_id).get("ExitCode")
        except docker.errors.APIError:
            pass
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
        "container": container.name,
    }
