"""Engine layer - every interaction with the INNER dockerd (dind).

Design rules (unchanged from v1):
  * The API ONLY touches objects labeled sandbox.managed=true.
  * All docker SDK calls raise EngineDown on connection/daemon errors;
    the API layer maps that to 503 + Retry-After.
  * Creation is rollback-safe: any failure after partial progress tears
    down everything this sandbox created.

v2 additions:
  * every sandbox container gets the in-sandbox agent injected (put_archive
    of /sbx/agent before start) and - in `init` mode - wrapped as PID 1:
        entrypoint = ["/sbx/agent", "init", "--"]
        command    = image.Entrypoint + (spec.command or image.Cmd)
    so the workload's own lifecycle is untouched and the API gains an
    HTTP control channel that never relies on `docker exec`;
  * readiness = every container's agent answers + (for port templates) the
    identity port answers HTTP;
  * `isolation="gvisor"` -> containers run under the runsc runtime;
  * `events()` exposes the daemon's event stream for the informer;
  * all engine access goes through the DockerEngine class so the
    simulator can substitute a FakeEngine with the same surface.
"""
import io
import logging
import tarfile
import urllib.request

import docker
import docker.errors

from . import clock, config, egress, templates
from .agent import AgentClient, NodeClient
from .volumes import VolumeManager

log = logging.getLogger("sandbox-api.engine")


class EngineDown(Exception):
    """The inner daemon is unreachable/unhealthy."""


def labels_for(sid: str, name: str, template: str, isolation: str) -> dict[str, str]:
    return {
        config.LABEL_MANAGED: "true",
        config.LABEL_ID: sid,
        config.LABEL_NAME: name,
        config.LABEL_TEMPLATE: template,
        config.LABEL_ISOLATION: isolation,
    }


_agent_bytes: bytes | None = None


def agent_binary() -> bytes:
    global _agent_bytes
    if _agent_bytes is None:
        with open(config.AGENT_BINARY, "rb") as f:
            _agent_bytes = f.read()
    return _agent_bytes


def agent_tar(extra_files: dict[str, bytes] | None = None) -> bytes:
    """Tar with /sbx/agent (0755) plus optional extra files (e.g. a CA)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        d = tarfile.TarInfo("sbx")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tar.addfile(d)
        info = tarfile.TarInfo("sbx/agent")
        data = agent_binary()
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
        for name, content in (extra_files or {}).items():
            fi = tarfile.TarInfo(f"sbx/{name}")
            fi.size = len(content)
            fi.mode = 0o644
            tar.addfile(fi, io.BytesIO(content))
    return buf.getvalue()


class DockerEngine:
    def __init__(self, node: NodeClient | None = None):
        self.node = node or NodeClient()
        self.volumes = VolumeManager(self.node)
        self._image_cfg_cache: dict[str, dict] = {}

    # -- client ------------------------------------------------------------------

    def _client(self) -> docker.DockerClient:
        try:
            return docker.DockerClient(base_url=config.DOCKER_HOST,
                                       timeout=config.DOCKER_TIMEOUT)
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def ping(self) -> bool:
        try:
            self._client().ping()
            return True
        except Exception:
            return False

    def info(self) -> dict:
        try:
            inf = self._client().info()
        except Exception as e:
            raise EngineDown(str(e)) from e
        return {
            "server_version": inf.get("ServerVersion"),
            "runtimes": sorted((inf.get("Runtimes") or {}).keys()),
            "default_runtime": inf.get("DefaultRuntime"),
            "containers": inf.get("Containers"),
            "images": inf.get("Images"),
            "kernel": inf.get("KernelVersion"),
            "cgroup_version": inf.get("CgroupVersion"),
            "memory_bytes": inf.get("MemTotal"),
            "cpus": inf.get("NCPU"),
        }

    def has_runtime(self, name: str) -> bool:
        try:
            return name in self.info()["runtimes"]
        except EngineDown:
            return False

    # -- images ------------------------------------------------------------------

    def ensure_image(self, image: str, log_event) -> None:
        """Guarantee `image` exists in the inner engine (build or pull)."""
        client = self._client()
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
                client.images.build(fileobj=ctx, custom_context=True, tag=image,
                                    rm=True, pull=True)
            else:
                log_event("pull", f"pulling {image} into dind")
                client.images.pull(image)
        except docker.errors.BuildError as e:
            tail = "".join(
                (l.get("stream") or l.get("error") or "")
                for l in list(e.build_log)[-15:]) if getattr(e, "build_log", None) else ""
            raise RuntimeError(f"build of {image} failed: {e}\n{tail[-800:]}") from e
        except docker.errors.APIError as e:
            raise RuntimeError(f"image prep {image} failed: {e}") from e

    def _image_config(self, client, image: str) -> dict:
        if image not in self._image_cfg_cache:
            cfg = client.images.get(image).attrs.get("Config") or {}
            self._image_cfg_cache[image] = {
                "Entrypoint": list(cfg.get("Entrypoint") or []),
                "Cmd": list(cfg.get("Cmd") or []),
            }
        return self._image_cfg_cache[image]

    # -- sandbox lifecycle -------------------------------------------------------

    def create_sandbox_resources(self, sid: str, name: str, template: str,
                                 port: int | None, isolation: str, token: str,
                                 log_event, extra_files: dict[str, bytes] | None = None,
                                 extra_env: dict[str, str] | None = None,
                                 options: dict | None = None) -> None:
        """Create network + containers (agent injected). Rolls back on ANY
        failure. On success containers are running; the caller waits for
        readiness and marks READY.

        options.stateful=true mounts an overlay volume (see volumes.py) at
        every data path the template declares; options.layers[role] is a
        list of immutable layer dirs to stack under it (fork/restore) and
        options.copies[name] seeds plain dirs (e.g. a WAL archive)."""
        client = self._client()
        spec = templates.TEMPLATES[template]["spec"](sid, name, port)
        tmpl = templates.TEMPLATES[template]
        labels = labels_for(sid, name, template, isolation)
        network_name = spec["network"]
        created = []
        network_created = False
        options = options or {}
        stateful = bool(options.get("stateful"))
        layers = options.get("layers") or {}
        copies = options.get("copies") or {}
        st_spec = tmpl.get("stateful") or {}
        extra_ports = options.get("extra_ports") or {}        # {"9340/tcp": 9340, ...}
        opt_env = {str(k): str(v) for k, v in (options.get("env") or {}).items()}
        eg = options.get("egress")
        eg_env: dict[str, str] = {}
        eg_files: dict[str, bytes] = {}
        proxy_ip = None
        if eg:
            cert_pem, key_pem = egress.generate_ca(sid)
            eg_files = {"ca.pem": cert_pem}
            aliases = [a for c in spec["containers"] for a in c.get("network_aliases", [])]
            eg_env = egress.sandbox_env(aliases)
            pspec = egress.proxy_container_spec(sid, eg.get("peer_networks") or [])
            pspec["image"] = templates.image_tag("sandbox-egress-proxy")
            spec = {"network": spec["network"], "containers": [pspec] + list(spec["containers"])}
            proxy_files = egress.ca_tar_files(cert_pem, key_pem, egress.policy_json(eg))
        inject_app = agent_tar({**(extra_files or {}), **eg_files})

        def rollback():
            for c in created:
                try:
                    c.remove(force=True)
                except docker.errors.DockerException:
                    log.warning("rollback: failed to remove %s", c.name)
            if network_created:
                try:
                    client.networks.get(network_name).remove()
                except docker.errors.DockerException:
                    log.warning("rollback: failed to remove net %s", network_name)
            if stateful:
                try:
                    self.volumes.teardown(sid)
                except Exception:
                    log.warning("rollback: volume teardown failed for %s", sid)
            if eg:
                try:
                    self.node.sh(egress.unfence_script(sid), timeout=60)
                except Exception:
                    log.warning("rollback: unfence failed for %s", sid)

        try:
            client.networks.create(network_name, driver="bridge", labels=labels,
                                   check_duplicate=True)
            network_created = True
            by_name = {}
            for cspec in spec["containers"]:
                wait = cspec.get("wait_for")
                if wait:
                    self._wait_healthy_or_running(by_name[wait["container"]], timeout=120)

                img_cfg = self._image_config(client, cspec["image"])
                agent_mode = cspec.get("agent_mode", "init")
                is_proxy = cspec.get("role") == "egress"
                env = {**(cspec.get("environment") or {}), **(extra_env or {}),
                       **({} if is_proxy else eg_env), **({} if is_proxy else opt_env)}
                binds: dict[str, dict] = {}
                cmd_override = None
                role_st = st_spec.get(cspec["role"]) if stateful else None
                if role_st:
                    role_layers = layers.get(cspec["role"]) or []
                    merged = self.volumes.prepare(sid, cspec["role"], role_layers)
                    binds[merged] = {"bind": role_st["path"], "mode": "rw"}
                    for bname, cpath in (role_st.get("binds") or {}).items():
                        host = self.volumes.plain_dir(sid, bname, copies.get(bname),
                                                      owner=(role_st.get("chown") or {}).get(bname))
                        binds[host] = {"bind": cpath, "mode": "rw"}
                    cmd_override = role_st.get("command")
                    log_event("volume", f"{cspec['role']}: overlay at {role_st['path']}"
                                        + (f" on {len(role_layers)} layer(s)" if role_layers else ""))
                if agent_mode == "init":
                    workload = list(img_cfg["Entrypoint"])
                    cmd = cmd_override if cmd_override is not None else cspec.get("command")
                    workload += list(cmd) if cmd is not None else list(img_cfg["Cmd"])
                    entrypoint = ["/sbx/agent", "init", "--"]
                    command = workload
                    env["SBX_AGENT_TOKEN"] = token
                    env["SBX_AGENT_FS_ROOT"] = tmpl.get("fs_root", "/")
                    env.update(cspec.get("agent_env") or {})
                else:
                    entrypoint = None
                    command = cmd_override if cmd_override is not None else cspec.get("command")

                kwargs = {
                    "image": cspec["image"], "name": cspec["name"],
                    "command": command, "detach": True, "network": network_name,
                    "environment": env,
                    "labels": {**labels, config.LABEL_ROLE: cspec["role"],
                               **({config.LABEL_PORT: str(port)} if cspec.get("port") else {})},
                }
                if entrypoint:
                    kwargs["entrypoint"] = entrypoint
                ports = dict(cspec.get("ports") or {})
                if extra_ports and cspec["role"] == "app":
                    ports.update({str(k): int(v) for k, v in extra_ports.items()})
                if ports:
                    kwargs["ports"] = ports
                if cspec.get("privileged") and tmpl.get("allow_privileged"):
                    kwargs["privileged"] = True
                if cspec.get("healthcheck"):
                    hc = cspec["healthcheck"]
                    kwargs["healthcheck"] = docker.types.Healthcheck(
                        test=hc["test"], interval=hc["interval"],
                        timeout=hc["timeout"], retries=hc["retries"])
                if cspec.get("shm_size"):
                    kwargs["shm_size"] = cspec["shm_size"]
                if binds:
                    kwargs["volumes"] = binds
                for k in ("mem_limit", "nano_cpus", "pids_limit", "cap_add", "security_opt"):
                    if cspec.get(k) is not None:
                        kwargs[k] = cspec[k]
                if isolation == "gvisor":
                    kwargs["runtime"] = "runsc"

                container = client.containers.create(**kwargs)
                created.append(container)
                # agent lands at /sbx/agent (+ CA cert for proxied sandboxes;
                # the proxy itself gets the CA key + policy instead)
                container.put_archive("/", agent_tar(proxy_files) if is_proxy else inject_app)
                for alias in cspec.get("network_aliases", []):
                    client.networks.get(network_name).disconnect(container)
                    client.networks.get(network_name).connect(container, aliases=[alias])
                for pn in cspec.get("peer_networks", []):
                    client.networks.get(pn).connect(container)
                container.start()
                if is_proxy:
                    container.reload()
                    proxy_ip = container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
                by_name[cspec["name"]] = container
                log_event("create", f"container {cspec['name']} started"
                                    f"{' (gvisor)' if isolation == 'gvisor' else ''}")
                if agent_mode == "exec":
                    self._start_agent_exec(client, container, token, cspec, tmpl)
                    log_event("agent", f"agent started via exec in {cspec['name']}")
            if eg:
                net_id = client.networks.get(network_name).id
                self.node.sh(egress.fence_script(sid, egress.bridge_name(net_id), proxy_ip), timeout=60)
                log_event("egress", f"policy {eg.get('mode', 'deny')}: proxy {proxy_ip}, "
                                    f"bridge {egress.bridge_name(net_id)} fenced (chain {egress.chain_name(sid)})")
        except Exception:
            rollback()
            raise

    def _start_agent_exec(self, client, container, token: str, cspec: dict, tmpl: dict):
        env = [f"SBX_AGENT_TOKEN={token}", f"SBX_AGENT_FS_ROOT={tmpl.get('fs_root', '/')}"]
        env += [f"{k}={v}" for k, v in (cspec.get("agent_env") or {}).items()]
        ex = client.api.exec_create(container.id, ["/sbx/agent", "serve"],
                                    environment=env, user="root")
        client.api.exec_start(ex["Id"], detach=True)

    def _wait_healthy_or_running(self, container, timeout: int) -> None:
        deadline = clock.now_mono() + timeout
        while clock.now_mono() < deadline:
            container.reload()
            state = container.attrs.get("State", {})
            health = state.get("Health", {}).get("Status")
            if health == "healthy" or (health is None and state.get("Running")):
                return
            if state.get("Status") in ("exited", "dead"):
                raise RuntimeError(
                    f"dependency {container.name} exited: "
                    f"{container.logs(tail=20).decode(errors='replace')}")
            clock.sleep(1)
        raise RuntimeError(f"dependency {container.name} not ready in {timeout}s")

    def wait_ready(self, sid: str, template: str, port: int | None, token: str,
                   timeout: int, log_event=None) -> None:
        """Block until every agent answers and (port templates) the app
        answers HTTP on the identity port."""
        deadline = clock.now_mono() + timeout
        pending = {c["name"]: c["ip"] for c in self.sandbox_containers(sid) if c["ip"]}
        while pending and clock.now_mono() < deadline:
            for cname, ip in list(pending.items()):
                if AgentClient(ip, token).alive():
                    del pending[cname]
            if pending:
                clock.sleep(1)
        if pending:
            raise RuntimeError(f"agent not reachable in {timeout}s: {sorted(pending)}")
        if log_event:
            log_event("agent", "all agents answering")
        health = templates.TEMPLATES[template]["health"]
        if health["kind"] != "http" or not port:
            return
        # Probe the template's REAL health path (coreapp: nginx -> backend ->
        # db), so READY means the whole chain answers, not just the first hop.
        url = f"http://{config.SANDBOX_HOST}:{port}{health.get('path', '/')}"
        while clock.now_mono() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status < 500:
                        return
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return               # 401/404 still means "app is up"
            except Exception:
                pass
            clock.sleep(1)
        raise RuntimeError(f"sandbox did not answer at {url} in {timeout}s")

    def destroy_sandbox_resources(self, sid: str) -> None:
        """Remove every managed container + the network of a sandbox. Idempotent."""
        client = self._client()
        try:
            containers = client.containers.list(
                all=True, filters={"label": f"{config.LABEL_ID}={sid}"})
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e
        for c in containers:
            try:
                c.remove(force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.DockerException as e:
                raise EngineDown(str(e)) from e
        for n in client.networks.list(filters={"label": f"{config.LABEL_ID}={sid}"}):
            try:
                n.remove()
            except docker.errors.DockerException:
                pass
        try:
            self.volumes.teardown(sid)           # no-op when the sandbox had no volumes
        except Exception as e:
            log.warning("volume teardown for %s failed: %s", sid, e)
        try:
            self.node.sh(egress.unfence_script(sid), timeout=60)   # no-op without a fence
        except Exception as e:
            log.warning("unfence for %s failed: %s", sid, e)

    def pause_sandbox(self, sid: str) -> None:
        client = self._client()
        try:
            for c in client.containers.list(filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.reload()
                if c.status == "running":
                    c.pause()
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def unpause_sandbox(self, sid: str) -> None:
        client = self._client()
        try:
            for c in client.containers.list(all=True, filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.reload()
                if c.status == "paused":
                    c.unpause()
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def stop_sandbox(self, sid: str) -> None:
        client = self._client()
        try:
            for c in client.containers.list(filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.stop(timeout=5)
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def start_sandbox(self, sid: str, token: str | None = None) -> None:
        """Start every stopped container; re-launch exec-mode agents."""
        client = self._client()
        try:
            for c in client.containers.list(all=True,
                                            filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.reload()
                if c.status != "running":
                    c.start()
                    tmpl_name = c.labels.get(config.LABEL_TEMPLATE)
                    if tmpl_name and token:
                        self._maybe_restart_exec_agent(client, c, token, tmpl_name)
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def _maybe_restart_exec_agent(self, client, container, token, tmpl_name):
        tmpl = templates.TEMPLATES.get(tmpl_name)
        if not tmpl:
            return
        for cspec in tmpl["spec"]("x", "x", 0)["containers"]:
            if cspec.get("agent_mode") == "exec" and cspec["role"] == container.labels.get(config.LABEL_ROLE):
                self._start_agent_exec(client, container, token, cspec, tmpl)

    # -- engine truth ----------------------------------------------------------------

    def _managed(self, client=None):
        client = client or self._client()
        try:
            return client.containers.list(
                all=True, filters={"label": f"{config.LABEL_MANAGED}=true"})
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e

    def engine_sandbox_ids(self) -> set[str]:
        return {c.labels.get(config.LABEL_ID) for c in self._managed()} - {None}

    def engine_port_map(self) -> dict[str, int]:
        out = {}
        for c in self._managed():
            sid = c.labels.get(config.LABEL_ID)
            port = c.labels.get(config.LABEL_PORT)
            if sid and port:
                out[sid] = int(port)
        return out

    def engine_sandboxes(self) -> dict[str, dict]:
        """sid -> {name, template, port, isolation, containers:[{name,status,role}]}"""
        out: dict[str, dict] = {}
        for c in self._managed():
            sid = c.labels.get(config.LABEL_ID)
            if not sid:
                continue
            sb = out.setdefault(sid, {
                "name": c.labels.get(config.LABEL_NAME, sid),
                "template": c.labels.get(config.LABEL_TEMPLATE, "unknown"),
                "isolation": c.labels.get(config.LABEL_ISOLATION, "container"),
                "port": None, "containers": []})
            if c.labels.get(config.LABEL_PORT):
                sb["port"] = int(c.labels[config.LABEL_PORT])
            sb["containers"].append({"name": c.name, "status": c.status,
                                     "role": c.labels.get(config.LABEL_ROLE, "?")})
        return out

    def sandbox_containers(self, sid: str) -> list[dict]:
        """Live view of the containers inside one sandbox (with inner IPs)."""
        client = self._client()
        try:
            containers = client.containers.list(
                all=True, filters={"label": f"{config.LABEL_ID}={sid}"})
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e
        out = []
        for c in containers:
            c.reload()
            state = c.attrs.get("State", {})
            nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
            ip = next((n.get("IPAddress") for n in nets.values() if n.get("IPAddress")), None)
            out.append({
                "name": c.name,
                "role": c.labels.get(config.LABEL_ROLE, "?"),
                "image": c.image.tags[0] if c.image.tags else "<built>",
                "status": c.status,
                "health": state.get("Health", {}).get("Status"),
                "started_at": state.get("StartedAt"),
                "exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled"),
                "port": c.labels.get(config.LABEL_PORT),
                "ip": ip,
                "runtime": c.attrs.get("HostConfig", {}).get("Runtime"),
            })
        out.sort(key=lambda x: x["name"])
        return out

    def container_ip(self, sid: str, container_name: str | None, role: str | None) -> tuple[str, str]:
        """Resolve (container_name, ip) for exec/session targeting."""
        cs = [c for c in self.sandbox_containers(sid) if c["status"] == "running" and c["ip"]]
        if container_name:
            for c in cs:
                if c["name"] == container_name:
                    return c["name"], c["ip"]
            raise RuntimeError(f"container '{container_name}' not running in this sandbox")
        if role:
            for c in cs:
                if c["role"] == role:
                    return c["name"], c["ip"]
        if not cs:
            raise RuntimeError("no running container in sandbox")
        return cs[0]["name"], cs[0]["ip"]

    # -- event stream (informer) ---------------------------------------------------------

    def events(self):
        """Yields (action, sid, container_name, attributes) for managed
        containers, forever (caller handles reconnects on EngineDown)."""
        client = self._client()
        try:
            stream = client.events(decode=True, filters={
                "type": "container", "label": f"{config.LABEL_MANAGED}=true"})
            for ev in stream:
                attrs = (ev.get("Actor") or {}).get("Attributes") or {}
                yield (ev.get("Action") or ev.get("status") or "?",
                       attrs.get(config.LABEL_ID), attrs.get("name"), attrs)
        except docker.errors.DockerException as e:
            raise EngineDown(str(e)) from e
