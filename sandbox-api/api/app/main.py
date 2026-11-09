"""sandbox-api v2 - the REST control plane (T01, extended).

Lifecycle (unchanged contract)
  GET  /healthz                     api + dind + node proxy + informer
  GET  /status                      pool, caps, engine facts, informer stats
  GET  /templates                   catalogue
  POST /sandboxes                   create (idempotent by name); isolation field
  GET  /sandboxes[/{id}]            list / inspect
  POST /sandboxes/{id}/stop|start   stop / start (start also heals DEGRADED)
  DELETE /sandboxes/{id}            destroy
  GET  /sandboxes/{id}/containers   what is inside, live (now with inner IPs)
  GET  /sandboxes/{id}/events       ordered log; ?follow=1 -> server-sent events
  GET  /events                      global SSE stream (?cursor= to resume)

Through the in-sandbox agent (never docker exec)
  POST /sandboxes/{id}/exec                      one-shot, kills on timeout
  POST /sandboxes/{id}/sessions                  durable exec session (pty optional)
  GET  /sandboxes/{id}/sessions                  list across containers
  GET  /sandboxes/{id}/sessions/{s}              state
  GET  /sandboxes/{id}/sessions/{s}/stream       NDJSON frames ?since=&follow=
  POST /sandboxes/{id}/sessions/{s}/stdin        ?eof=1
  POST /sandboxes/{id}/sessions/{s}/signal       {signal}
  POST /sandboxes/{id}/sessions/{s}/resize       {cols, rows}
  DELETE /sandboxes/{id}/sessions/{s}
  GET  /sandboxes/{id}/fs?path=                  list       (all ?container=)
  GET  /sandboxes/{id}/fs/read?path=             bytes
  PUT  /sandboxes/{id}/fs/write?path=&mode=      bytes
  GET  /sandboxes/{id}/fs/tar?path=              tar download
  PUT  /sandboxes/{id}/fs/tar?path=              tar upload
  POST /sandboxes/{id}/fs/mkdir?path=
  DELETE /sandboxes/{id}/fs?path=
  GET  /sandboxes/{id}/ps  /stats                process list / cgroup stats
  GET  /sandboxes/{id}/fingerprint?path=         merkle fingerprint (fs)
  GET  /sandboxes/{id}/fingerprint/db            table fingerprints (coreapp)
  GET  /diff?a=&b=                               semantic diff of two sandboxes
  GET  /sandboxes/{id}/screenshot                PNG (desktop template)
  POST /sandboxes/{id}/gui                       {type: click|type|key|...}
  GET  /sandboxes/{id}/gui/windows  /gui/display
  GET  /node/info                                engine node facts

Session ids returned by this API are "<container>:<agent-session-id>", so
every later call knows which container's agent to talk to without state.
"""
import json
import logging
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import clock, config, egress, fingerprint, templates
from .volumes import BASE as VOL_BASE
from .agent import AgentClient, AgentError, NodeClient
from .bus import bus
from .core import ApiError, Core
from .engine import DockerEngine, EngineDown
from .informer import Informer
from .store import Store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-api")

app = FastAPI(title="sandbox-api", version="2.0.0")

store = Store(config.DB_PATH)
node = NodeClient()
engine = DockerEngine(node)
core = Core(store, engine)
informer = Informer(core)


# -- request models -----------------------------------------------------------

class CreateRequest(BaseModel):
    name: str
    template: str = "web"
    ttl_seconds: int | None = Field(default=None, ge=10, le=86400)
    isolation: str | None = None            # container | gvisor
    options: dict | None = None             # free-form, part of the idempotency spec


class ExecRequest(BaseModel):
    cmd: list[str] = Field(min_length=1)
    timeout: int = Field(default=config.EXEC_DEFAULT_TIMEOUT, ge=1, le=config.EXEC_MAX_TIMEOUT)
    container: str | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None


class SessionRequest(BaseModel):
    cmd: list[str] = Field(min_length=1)
    container: str | None = None
    tty: bool = False
    cols: int = 120
    rows: int = 40
    env: dict[str, str] | None = None
    cwd: str | None = None
    timeout_seconds: int = Field(default=0, ge=0, le=86400)
    stdin: bool = False
    buffer_bytes: int = Field(default=0, ge=0, le=64 << 20)


class StdinBody(BaseModel):
    data: str = ""


class SignalBody(BaseModel):
    signal: str = "TERM"


class ResizeBody(BaseModel):
    cols: int = Field(ge=1, le=1000)
    rows: int = Field(ge=1, le=500)


# -- helpers ------------------------------------------------------------------

def _http(e: ApiError) -> HTTPException:
    return HTTPException(e.status, detail=e.detail, headers=e.headers or None)


def _agent_http(e: AgentError) -> HTTPException:
    return HTTPException(e.status if e.status in (400, 401, 403, 404, 409, 413, 502) else 502,
                         detail=e.detail)


def _agent_for(sid: str, container: str | None = None, role: str | None = None
               ) -> tuple[str, AgentClient]:
    row = core.get_or_404(sid)
    if row["state"] not in ("READY", "DEGRADED"):
        raise HTTPException(409, f"requires READY/DEGRADED (state={row['state']})")
    tmpl = templates.TEMPLATES.get(row["template"], {})
    try:
        cname, ip = engine.container_ip(sid, container, role or tmpl.get("default_exec_role"))
    except EngineDown:
        raise HTTPException(503, "inner engine (dind) unavailable", headers={"Retry-After": "5"})
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return cname, AgentClient(ip, row.get("agent_token"))


def _split_session(s: str) -> tuple[str, str]:
    if ":" not in s:
        raise HTTPException(400, "session id must be <container>:<session>")
    cname, sess = s.split(":", 1)
    return cname, sess


def _provision_thread(sid: str) -> None:
    def run():
        try:
            for _ in core.provision_steps(sid):
                pass
        except Exception:
            log.exception("provision %s crashed", sid)
    threading.Thread(target=run, daemon=True, name=f"provision-{sid}").start()


# -- lifecycle ----------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    store.init()
    try:
        stats = core.reconcile()                                # edge case #4
        log.info("reconciliation complete: %s", stats)
    except EngineDown:
        log.warning("dind unavailable at boot; will serve 503s until ready")
    except Exception:
        log.exception("reconcile failed at boot")
    informer.start()


# -- routes: platform ---------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"api": "up", "dind": "up" if engine.ping() else "down",
            "node": "up" if node.healthz() else "down",
            "informer": "connected" if informer.connected else "disconnected"}


@app.get("/templates")
def list_templates():
    return templates.catalog()


@app.get("/status")
def platform_status():
    dind_up = engine.ping()
    used = sorted(store.used_ports())
    info = {}
    if dind_up:
        try:
            info = engine.info()
        except EngineDown:
            pass
    return {"api": "up", "dind": "up" if dind_up else "down",
            "node": "up" if node.healthz() else "down",
            "pool": {"start": config.POOL_START, "end": config.POOL_END, "used": used,
                     "free": [p for p in range(config.POOL_START, config.POOL_END + 1)
                              if p not in used]},
            "max_sandboxes": config.MAX_SANDBOXES,
            "sandboxes": len(store.list_all()),
            "engine": info, "informer": informer.status(),
            "isolation_available": ["container"] + (["gvisor"] if "runsc" in info.get("runtimes", []) else [])}


@app.get("/node/info")
def node_info():
    try:
        return node.info()
    except AgentError as e:
        raise _agent_http(e)


# -- routes: sandboxes --------------------------------------------------------

@app.post("/sandboxes", status_code=201)
def create_sandbox(req: CreateRequest):
    try:
        outcome, row = core.create(req.name, req.template, req.ttl_seconds,
                                   req.isolation, req.options)
    except ApiError as e:
        raise _http(e)
    if outcome == "existing":
        return JSONResponse(status_code=200, content=core.view(row))
    _provision_thread(row["id"])
    return JSONResponse(status_code=201, content=core.view(row))


@app.get("/sandboxes")
def list_sandboxes():
    return [core.view(r) for r in store.list_all()]


@app.get("/sandboxes/{sid}")
def get_sandbox(sid: str):
    try:
        return core.view(core.get_or_404(sid))
    except ApiError as e:
        raise _http(e)


@app.post("/sandboxes/{sid}/stop")
def stop_sandbox(sid: str):
    try:
        return core.view(core.stop(sid))
    except ApiError as e:
        raise _http(e)


@app.post("/sandboxes/{sid}/start")
def start_sandbox(sid: str):
    try:
        return core.view(core.start(sid))
    except ApiError as e:
        raise _http(e)


@app.post("/sandboxes/{sid}/pause")
def pause_sandbox(sid: str):
    try:
        return core.view(core.pause(sid))
    except ApiError as e:
        raise _http(e)


@app.post("/sandboxes/{sid}/unpause")
def unpause_sandbox(sid: str):
    try:
        return core.view(core.unpause(sid))
    except ApiError as e:
        raise _http(e)


def _stateful_roles(row: dict) -> dict:
    st = templates.TEMPLATES.get(row["template"], {}).get("stateful") or {}
    stateful = bool(((row.get("spec") or {}).get("options") or {}).get("stateful"))
    return st if stateful else {}


@app.get("/sandboxes/{sid}/volumes")
def sandbox_volumes(sid: str):
    """Stateful volumes of a sandbox as seen on the engine node."""
    try:
        row = core.get_or_404(sid)
        st = _stateful_roles(row)
        out = {"stateful": bool(st), "roles": {}, "plain": {}}
        for role, v in st.items():
            out["roles"][role] = {"path": v["path"], "node_dir": engine.volumes.vol_dir(sid, role),
                                  "upper_bytes": engine.volumes.upper_size(sid, role)}
            for bname, cpath in (v.get("binds") or {}).items():
                out["plain"][bname] = {"path": cpath, "node_dir": f"{VOL_BASE}/vol/{sid}/{bname}"}
        if st:
            out["mounts"] = engine.volumes.mounts(sid)
        return out
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


class SnapshotBody(BaseModel):
    dest: str = Field(min_length=1)       # node dir under /var/lib/sbx/ckpt/


@app.post("/sandboxes/{sid}/volumes/snapshot")
def volumes_snapshot(sid: str, body: SnapshotBody):
    """Copy every upper layer (and plain dirs) of a PAUSED or STOPPED
    stateful sandbox into dest/<role>|<name>. Returns sizes. This is the
    primitive a checkpoint is made of."""
    try:
        row = core.get_or_404(sid)
        if row["state"] not in ("PAUSED", "STOPPED"):
            raise HTTPException(409, f"snapshot requires PAUSED or STOPPED (state={row['state']})")
        st = _stateful_roles(row)
        if not st:
            raise HTTPException(400, "sandbox is not stateful")
        res = engine.volumes.snapshot(sid, st, body.dest)
        store.add_event(sid, "checkpoint", f"snapshot -> {body.dest} ({sum(v['bytes'] for v in res.values())} bytes)")
        return res
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


class RestoreBody(BaseModel):
    layers: dict[str, list[str]] = {}     # role -> layer dirs (newest first)
    copies: dict[str, str] = {}           # plain name -> source dir


@app.post("/sandboxes/{sid}/volumes/restore")
def volumes_restore(sid: str, body: RestoreBody):
    """Replace the layer stacks of a STOPPED stateful sandbox (fresh upper
    over the given layers) and re-seed plain dirs. Start it afterwards."""
    try:
        row = core.get_or_404(sid)
        if row["state"] != "STOPPED":
            raise HTTPException(409, f"restore requires STOPPED (state={row['state']})")
        st = _stateful_roles(row)
        if not st:
            raise HTTPException(400, "sandbox is not stateful")
        res = engine.volumes.restore(sid, st, body.layers, body.copies)
        store.add_event(sid, "checkpoint", f"volumes restored from {len(body.layers)} layer stack(s)")
        return res
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


# -- egress policy (proxy container + iptables fence) --------------------------

def _egress_agent(sid: str):
    row = core.get_or_404(sid)
    if not ((row.get("spec") or {}).get("options") or {}).get("egress"):
        raise HTTPException(400, "sandbox has no egress policy (create it with options.egress)")
    return _agent_for(sid, f"sbx-{sid}-egress")[1]


@app.get("/sandboxes/{sid}/egress")
def egress_get(sid: str):
    try:
        agent = _egress_agent(sid)
        pol = json.loads(agent.fs_read("/sbx/policy.json") or b"{}")
        try:
            stats = json.loads(agent.fs_read("/sbx/stats.json") or b"{}")
        except AgentError:
            stats = {}
        fence = node.exec(["sh", "-c", egress.fence_status_script(sid)]).get("stdout", "")
        return {"policy": pol, "stats": stats, "fence": fence.strip().splitlines()}
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


class EgressPolicyBody(BaseModel):
    mode: str = Field(pattern="^(deny|allow|record|replay)$")
    allow: list[str] = []
    secrets: dict[str, dict] = {}
    strict_replay: bool = True


@app.put("/sandboxes/{sid}/egress")
def egress_put(sid: str, body: EgressPolicyBody):
    """Change the live policy: the proxy re-reads /sbx/policy.json on the
    next request (mtime watch)."""
    try:
        agent = _egress_agent(sid)
        agent.fs_write("/sbx/policy.json", egress.policy_json(body.model_dump()))
        store.add_event(sid, "egress", f"policy -> {body.mode} allow={body.allow}")
        return {"ok": True, "mode": body.mode}
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.get("/sandboxes/{sid}/egress/cassette")
def egress_cassette_get(sid: str):
    try:
        agent = _egress_agent(sid)
        try:
            data = agent.fs_read("/sbx/cassette.jsonl", max_bytes=64 << 20)
        except AgentError as e:
            if e.status == 404:
                data = b""
            else:
                raise
        return Response(content=data, media_type="application/x-ndjson")
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.put("/sandboxes/{sid}/egress/cassette")
async def egress_cassette_put(sid: str, request: Request):
    data = await request.body()
    try:
        agent = _egress_agent(sid)
        res = agent.fs_write("/sbx/cassette.jsonl", data)
        store.add_event(sid, "egress", f"cassette loaded ({len(data)} bytes)")
        return res
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.delete("/sandboxes/{sid}/egress/cassette")
def egress_cassette_delete(sid: str):
    try:
        agent = _egress_agent(sid)
        try:
            agent.fs_delete("/sbx/cassette.jsonl")
        except AgentError as e:
            if e.status != 404:
                raise
        return {"deleted": True}
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


# -- node ops (restricted to the sandbox state area) -------------------------

def _node_path(p: str) -> str:
    p = str(p)
    if not p.startswith("/var/lib/sbx/") or ".." in p.split("/"):
        raise HTTPException(400, "path must be under /var/lib/sbx/")
    return p


@app.get("/node/tar")
def node_tar(path: str):
    """Stream a tar of a node directory under /var/lib/sbx (checkpoint
    layers -> blob store)."""
    p = _node_path(path)
    try:
        return StreamingResponse(engine.volumes.tar_stream(p), media_type="application/x-tar")
    except AgentError as e:
        raise _agent_http(e)


@app.put("/node/untar")
async def node_untar(path: str, request: Request):
    """Extract a tar body into a node directory under /var/lib/sbx (blob
    store -> checkpoint layer)."""
    p = _node_path(path)
    data = await request.body()
    try:
        return engine.volumes.untar(p, data)
    except AgentError as e:
        raise _agent_http(e)


@app.delete("/node/dir")
def node_rmdir(path: str):
    p = _node_path(path)
    if not p.startswith("/var/lib/sbx/ckpt/"):
        raise HTTPException(400, "only /var/lib/sbx/ckpt/... may be removed")
    try:
        engine.volumes.rmdir(p)
        return {"removed": p}
    except AgentError as e:
        raise _agent_http(e)


@app.get("/node/du")
def node_du(path: str):
    p = _node_path(path)
    try:
        return {"path": p, "bytes": engine.volumes.du(p)}
    except AgentError as e:
        raise _agent_http(e)


@app.delete("/sandboxes/{sid}", status_code=204)
def delete_sandbox(sid: str):
    try:
        core.delete(sid)
    except ApiError as e:
        raise _http(e)
    return Response(status_code=204)


@app.get("/sandboxes/{sid}/containers")
def sandbox_containers(sid: str):
    try:
        core.get_or_404(sid)
        return engine.sandbox_containers(sid)
    except ApiError as e:
        raise _http(e)
    except EngineDown:
        raise HTTPException(503, "inner engine (dind) unavailable", headers={"Retry-After": "5"})


# -- routes: events (log + SSE) -----------------------------------------------

def _sse(gen):
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse_stream(initial: list[dict], match, max_seconds: int):
    """Replay `initial`, then live bus events accepted by `match`, with
    15 s heartbeats so proxies keep the connection open."""
    q = bus.subscribe()
    try:
        for ev in initial:
            yield f"id: {ev['cursor']}\ndata: {json.dumps(ev)}\n\n"
        deadline = clock.now_mono() + max_seconds
        while clock.now_mono() < deadline:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if match(ev):
                yield f"id: {ev['cursor']}\ndata: {json.dumps(ev)}\n\n"
        yield "event: end\ndata: {\"reason\": \"max_seconds\"}\n\n"
    finally:
        bus.unsubscribe(q)


@app.get("/sandboxes/{sid}/events")
def sandbox_events(sid: str, since: int = 0, follow: int = 0, timeout: int = 300):
    # The event log is the audit trail: readable after deletion. 404 only
    # if the sandbox NEVER existed.
    if not store.get(sid):
        raise HTTPException(404, f"sandbox {sid} never existed")
    initial = store.events(sid, since)
    if not follow:
        return initial
    last_cursor = initial[-1]["cursor"] if initial else store.last_cursor()
    return _sse(_sse_stream(initial,
                            lambda ev: ev["sandbox_id"] == sid and ev["cursor"] > last_cursor,
                            timeout))


@app.get("/events")
def global_events(cursor: int = 0, follow: int = 1, timeout: int = 300, limit: int = 500):
    initial = store.events_after(cursor, limit)
    if not follow:
        return initial
    last_cursor = initial[-1]["cursor"] if initial else max(cursor, store.last_cursor())
    return _sse(_sse_stream(initial, lambda ev: ev["cursor"] > last_cursor, timeout))


# -- routes: exec + sessions (via agent) --------------------------------------

@app.post("/sandboxes/{sid}/exec")
def exec_sandbox(sid: str, req: ExecRequest):
    try:
        cname, agent = _agent_for(sid, req.container)
        result = agent.exec(req.cmd, req.timeout, req.env, req.cwd)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    result["container"] = cname
    result["session_id"] = f"{cname}:{result.get('session_id')}"
    store.add_event(sid, "action",
                    f"exec {' '.join(req.cmd)[:120]} -> exit={result['exit_code']} "
                    f"timed_out={result['timed_out']}")
    return result


@app.post("/sandboxes/{sid}/sessions", status_code=201)
def session_create(sid: str, req: SessionRequest):
    try:
        cname, agent = _agent_for(sid, req.container)
        body = req.model_dump(exclude={"container"})
        sess = agent.session_create(body)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    sess["id"] = f"{cname}:{sess['id']}"
    sess["container"] = cname
    store.add_event(sid, "action", f"session {sess['id']} started: {' '.join(req.cmd)[:100]}")
    return sess


@app.get("/sandboxes/{sid}/sessions")
def session_list(sid: str):
    try:
        row = core.get_or_404(sid)
        out = []
        for c in engine.sandbox_containers(sid):
            if c["status"] != "running" or not c["ip"]:
                continue
            try:
                for s in AgentClient(c["ip"], row.get("agent_token")).session_list():
                    s["id"] = f"{c['name']}:{s['id']}"
                    s["container"] = c["name"]
                    out.append(s)
            except AgentError:
                pass
        return out
    except ApiError as e:
        raise _http(e)
    except EngineDown:
        raise HTTPException(503, "inner engine (dind) unavailable", headers={"Retry-After": "5"})


@app.get("/sandboxes/{sid}/sessions/{session}")
def session_get(sid: str, session: str):
    cname, sess = _split_session(session)
    try:
        _, agent = _agent_for(sid, cname)
        s = agent.session_get(sess)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    s["id"] = session
    s["container"] = cname
    return s


@app.get("/sandboxes/{sid}/sessions/{session}/stream")
def session_stream(sid: str, session: str, since: int = 0, follow: int = 0, timeout: int = 30):
    cname, sess = _split_session(session)
    try:
        _, agent = _agent_for(sid, cname)
        agent.session_get(sess)             # 404 early instead of mid-stream
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)

    def gen():
        try:
            for line in agent.session_stream(sess, since, bool(follow), timeout):
                yield line + b"\n"
        except AgentError as e:
            yield json.dumps({"error": e.detail}).encode() + b"\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no"})


@app.post("/sandboxes/{sid}/sessions/{session}/stdin")
async def session_stdin(sid: str, session: str, request: Request, eof: int = 0):
    cname, sess = _split_session(session)
    data = await request.body()
    try:
        _, agent = _agent_for(sid, cname)
        return agent.session_stdin(sess, data, bool(eof))
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.post("/sandboxes/{sid}/sessions/{session}/signal")
def session_signal(sid: str, session: str, body: SignalBody):
    cname, sess = _split_session(session)
    try:
        _, agent = _agent_for(sid, cname)
        return agent.session_signal(sess, body.signal)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.post("/sandboxes/{sid}/sessions/{session}/resize")
def session_resize(sid: str, session: str, body: ResizeBody):
    cname, sess = _split_session(session)
    try:
        _, agent = _agent_for(sid, cname)
        return agent.session_resize(sess, body.cols, body.rows)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.delete("/sandboxes/{sid}/sessions/{session}", status_code=204)
def session_delete(sid: str, session: str):
    cname, sess = _split_session(session)
    try:
        _, agent = _agent_for(sid, cname)
        agent.session_delete(sess)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    return Response(status_code=204)


# -- routes: files ------------------------------------------------------------

@app.get("/sandboxes/{sid}/fs")
def fs_list(sid: str, path: str, container: str | None = None):
    try:
        _, agent = _agent_for(sid, container)
        return agent.fs_list(path)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.get("/sandboxes/{sid}/fs/read")
def fs_read(sid: str, path: str, container: str | None = None, max: int = 1 << 20):
    try:
        _, agent = _agent_for(sid, container)
        data = agent.fs_read(path, max)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    return Response(content=data, media_type="application/octet-stream")


@app.put("/sandboxes/{sid}/fs/write")
async def fs_write(sid: str, path: str, request: Request, container: str | None = None,
                   mode: str = "0644"):
    data = await request.body()
    try:
        _, agent = _agent_for(sid, container)
        res = agent.fs_write(path, data, mode)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    store.add_event(sid, "action", f"fs write {path} ({len(data)} bytes)")
    return res


@app.post("/sandboxes/{sid}/fs/mkdir")
def fs_mkdir(sid: str, path: str, container: str | None = None):
    try:
        _, agent = _agent_for(sid, container)
        return agent.fs_mkdir(path)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.delete("/sandboxes/{sid}/fs")
def fs_delete(sid: str, path: str, container: str | None = None):
    try:
        _, agent = _agent_for(sid, container)
        res = agent.fs_delete(path)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    store.add_event(sid, "action", f"fs delete {path}")
    return res


@app.get("/sandboxes/{sid}/fs/tar")
def fs_tar_get(sid: str, path: str, container: str | None = None):
    try:
        _, agent = _agent_for(sid, container)
        chunks = agent.fs_get_tar(path)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    return StreamingResponse(chunks, media_type="application/x-tar")


@app.put("/sandboxes/{sid}/fs/tar")
async def fs_tar_put(sid: str, path: str, request: Request, container: str | None = None):
    data = await request.body()
    try:
        _, agent = _agent_for(sid, container)
        res = agent.fs_put_tar(path, data)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    store.add_event(sid, "action", f"fs tar upload -> {path} ({res.get('files')} files)")
    return res


# -- routes: introspection ----------------------------------------------------

@app.get("/sandboxes/{sid}/ps")
def sandbox_ps(sid: str, container: str | None = None):
    try:
        cname, agent = _agent_for(sid, container)
        return {"container": cname, **agent.ps()}
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.get("/sandboxes/{sid}/stats")
def sandbox_stats(sid: str, container: str | None = None):
    try:
        row = core.get_or_404(sid)
        out = {}
        for c in engine.sandbox_containers(sid):
            if container and c["name"] != container:
                continue
            if c["status"] != "running" or not c["ip"]:
                out[c["name"]] = {"status": c["status"]}
                continue
            try:
                out[c["name"]] = AgentClient(c["ip"], row.get("agent_token")).stats()
            except AgentError as e:
                out[c["name"]] = {"error": e.detail}
        return out
    except ApiError as e:
        raise _http(e)
    except EngineDown:
        raise HTTPException(503, "inner engine (dind) unavailable", headers={"Retry-After": "5"})


@app.get("/sandboxes/{sid}/fingerprint")
def sandbox_fingerprint(sid: str, path: str | None = None, container: str | None = None,
                        exclude: str = "", detail: int = 0):
    try:
        row = core.get_or_404(sid)
        tmpl = templates.TEMPLATES.get(row["template"], {})
        cname, agent = _agent_for(sid, container)
        fp = agent.fingerprint(path or tmpl.get("fingerprint_path", "/"), exclude, bool(detail))
        fp["container"] = cname
        return fp
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.get("/sandboxes/{sid}/fingerprint/db")
def sandbox_fingerprint_db(sid: str, rows: int = 0):
    try:
        row = core.get_or_404(sid)
        db = templates.TEMPLATES.get(row["template"], {}).get("db")
        if not db:
            raise HTTPException(400, f"template {row['template']} has no database role")
        _, agent = _agent_for(sid, None, db["role"])
        return fingerprint.db_fingerprint(agent, db["user"], db["database"], bool(rows))
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/diff")
def diff_sandboxes(a: str, b: str, path: str | None = None, exclude: str = "",
                   container: str | None = None, db: int = 1):
    """Semantic diff between two sandboxes: files (merkle entries) and, for
    database templates, tables and rows."""
    try:
        ra, rb = core.get_or_404(a), core.get_or_404(b)
        ta, tb = templates.TEMPLATES[ra["template"]], templates.TEMPLATES[rb["template"]]
        p = path or ta.get("fingerprint_path", "/")
        _, aga = _agent_for(a, container)
        _, agb = _agent_for(b, container)
        fa = aga.fingerprint(p, exclude, True)
        fb = agb.fingerprint(p, exclude, True)
        out = {"a": a, "b": b, "path": p, "fs": fingerprint.diff_fs(fa, fb)}
        if db and ta.get("db") and tb.get("db"):
            _, dba = _agent_for(a, None, ta["db"]["role"])
            _, dbb = _agent_for(b, None, tb["db"]["role"])
            da = fingerprint.db_fingerprint(dba, ta["db"]["user"], ta["db"]["database"], True)
            dbf = fingerprint.db_fingerprint(dbb, tb["db"]["user"], tb["db"]["database"], True)
            out["db"] = fingerprint.diff_db(da, dbf)
        out["identical"] = out["fs"]["identical"] and out.get("db", {}).get("identical", True)
        return out
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# -- routes: GUI (desktop template) -------------------------------------------

def _gui_agent(sid: str):
    row = core.get_or_404(sid)
    if not templates.TEMPLATES.get(row["template"], {}).get("gui"):
        raise HTTPException(400, f"template {row['template']} has no GUI")
    return _agent_for(sid, None, "app")[1]


@app.get("/sandboxes/{sid}/screenshot")
def gui_screenshot(sid: str):
    try:
        png = _gui_agent(sid).gui_screenshot()
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    return Response(content=png, media_type="image/png")


@app.post("/sandboxes/{sid}/gui")
def gui_action(sid: str, action: dict):
    try:
        res = _gui_agent(sid).gui_action(action)
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)
    store.add_event(sid, "gui", f"{action.get('type')} {json.dumps(action)[:100]}")
    return res


@app.get("/sandboxes/{sid}/gui/windows")
def gui_windows(sid: str):
    try:
        return _gui_agent(sid).gui_windows()
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


@app.get("/sandboxes/{sid}/gui/display")
def gui_display(sid: str):
    try:
        return _gui_agent(sid).gui_display()
    except ApiError as e:
        raise _http(e)
    except AgentError as e:
        raise _agent_http(e)


# -- web console (static SPA) -------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
