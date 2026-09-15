"""sandbox-api - the REST control plane (T01).

Endpoints
  GET  /healthz                     api + dind liveness (never hangs)
  GET  /templates                   available sandbox templates
  POST /sandboxes                   create (idempotent by name)
  GET  /sandboxes                   list (non-deleted)
  GET  /sandboxes/{id}              inspect one
  POST /sandboxes/{id}/stop|start   stop / start
  POST /sandboxes/{id}/exec         run a command inside (timeout + caps)
  GET  /sandboxes/{id}/events       per-sandbox ordered event log
  DELETE /sandboxes/{id}            destroy (idempotent semantics below)

Every non-happy path is an explicit edge case; see docs/EDGE-CASES.md.
"""
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config, engine, store, templates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-api")

app = FastAPI(title="sandbox-api", version="1.0.0")

# Per-sandbox locks: serializes reaper vs manual delete vs stop/start so
# racing operations are both-idempotent instead of torn (edge case #9).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(sid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(sid, threading.Lock())


# -- request models -----------------------------------------------------------

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class CreateRequest(BaseModel):
    name: str
    template: str = "web"
    ttl_seconds: int | None = Field(default=None, ge=10, le=86400)

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                "name must match ^[a-z0-9][a-z0-9-]{1,40}$")  # edge #11
        return v

    @field_validator("template")
    @classmethod
    def known_template(cls, v: str) -> str:
        if v not in templates.TEMPLATES:
            raise ValueError(
                f"unknown template; valid: {sorted(templates.TEMPLATES)}")
        return v

    def spec(self) -> dict:
        return {"template": self.template, "ttl_seconds": self.ttl_seconds}


class ExecRequest(BaseModel):
    cmd: list[str] = Field(min_length=1)
    timeout: int = Field(default=config.EXEC_DEFAULT_TIMEOUT,
                         ge=1, le=config.EXEC_MAX_TIMEOUT)
    container: str | None = None     # target a specific container by name


# -- helpers ------------------------------------------------------------------

def _503() -> HTTPException:
    return HTTPException(503, detail="inner engine (dind) unavailable",
                         headers={"Retry-After": "5"})


def _view(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "template": row["template"],
        "state": row["state"], "port": row["port"],
        "url": (f"http://localhost:{row['port']}" if row["port"] else None),
        "ttl_seconds": row["ttl_seconds"], "error": row["error"],
        "created_at": row["created_at"],
    }


def _get_or_404(sid: str) -> dict:
    row = store.get(sid)
    if not row or row["state"] == "DELETED":
        raise HTTPException(404, f"sandbox {sid} not found")
    return row


def _destroy_and_mark(sid: str, reason: str) -> None:
    """Shared teardown path for manual delete AND the TTL reaper.

    Caller holds the per-sandbox lock, so the two never tear the same
    sandbox apart concurrently (edge case #9).
    """
    engine.destroy_sandbox_resources(sid)
    store.set_state(sid, "DELETED")
    store.add_event(sid, "state", f"DELETED ({reason})")


# -- lifecycle ----------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    store.init()
    try:
        engine.reconcile()                      # edge case #4
        log.info("reconciliation complete")
    except engine.EngineDown:
        # dind may still be starting; endpoints will 503 until it is up and
        # the next create attempt re-checks. Never crash the API for this.
        log.warning("dind unavailable at boot; will serve 503s until ready")

    def reaper():
        while True:
            time.sleep(config.REAPER_INTERVAL)
            for row in store.expired(time.monotonic()):
                with _lock_for(row["id"]):
                    fresh = store.get(row["id"])
                    if not fresh or fresh["state"] == "DELETED":
                        continue                 # lost the race to a manual delete
                    try:
                        store.add_event(row["id"], "state", "TTL expired")
                        _destroy_and_mark(row["id"], "ttl")
                    except engine.EngineDown:
                        log.warning("reaper: dind down, retry next sweep")

    threading.Thread(target=reaper, daemon=True).start()


# -- routes -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"api": "up", "dind": "up" if engine.ping() else "down"}


@app.get("/templates")
def list_templates():
    return {name: {"needs_port": t["needs_port"]}
            for name, t in templates.TEMPLATES.items()}


@app.post("/sandboxes", status_code=201)
def create_sandbox(req: CreateRequest, request: Request):
    if not engine.ping():
        raise _503()                                             # edge #8

    # -- edge case #1: idempotent create by name --------------------------
    existing = store.get_by_name(req.name)
    if existing and existing["state"] != "DELETED":
        if existing["spec"] == req.spec():
            return JSONResponse(status_code=200, content=_view(existing))
        raise HTTPException(409, f"name '{req.name}' exists with a "
                                 "different spec; delete it or pick a new name")
    # Name reuse after delete: tombstone the old row's name so the UNIQUE
    # constraint never blocks a legitimate recreate (edge case #7b).
    store.free_name(req.name)

    # -- edge case #10: engine-derived capacity cap -----------------------
    try:
        live = engine.engine_sandbox_ids()
    except engine.EngineDown:
        raise _503()
    if len(live) >= config.MAX_SANDBOXES:
        raise HTTPException(429, f"capacity reached ({config.MAX_SANDBOXES} "
                                 "sandboxes)", headers={"Retry-After": "30"})

    sid = uuid.uuid4().hex[:12]
    deadline = (time.monotonic() + req.ttl_seconds) if req.ttl_seconds else None
    store.create({
        "id": sid, "name": req.name, "template": req.template,
        "spec": req.spec(), "state": "CREATING", "port": None,
        "ttl_seconds": req.ttl_seconds, "deadline_mono": deadline,
    })
    store.add_event(sid, "state", "CREATING")

    needs_port = templates.TEMPLATES[req.template]["needs_port"]
    port = None
    if needs_port:
        port = store.alloc_port(sid)
        if port is None:
            # -- edge case #2: port exhaustion, clean rollback ------------
            store.set_state(sid, "FAILED", "port pool exhausted")
            store.add_event(sid, "error",
                            f"port pool {config.POOL_START}-{config.POOL_END} "
                            "exhausted; nothing created")
            raise HTTPException(429, "sandbox port pool exhausted",
                                headers={"Retry-After": "30"})
        store.add_event(sid, "alloc", f"allocated port {port}")

    def provision():
        """Runs off-request so the API stays responsive during image builds."""
        with _lock_for(sid):
            def log_event(t, m):
                store.add_event(sid, t, m)
            try:
                client = engine._client()
                for image in templates.TEMPLATES[req.template]["images"]:
                    engine.ensure_image(client, image, log_event)
                engine.create_sandbox_resources(sid, req.name, req.template,
                                                port, log_event)
                engine.wait_ready(sid, req.template, port)
            except Exception as e:                      # edge case #3
                store.set_state(sid, "FAILED", str(e)[:500])
                store.add_event(sid, "error", f"create failed: {e}")
                # free the port so a FAILED sandbox never holds a slot
                store.set_port(sid, None)
                return
            store.set_state(sid, "READY")
            store.add_event(sid, "state", "READY")

    threading.Thread(target=provision, daemon=True).start()
    row = store.get(sid)
    return JSONResponse(status_code=201, content=_view(row))


@app.get("/sandboxes")
def list_sandboxes():
    return [_view(r) for r in store.list_all()]


@app.get("/sandboxes/{sid}")
def get_sandbox(sid: str):
    return _view(_get_or_404(sid))


@app.post("/sandboxes/{sid}/stop")
def stop_sandbox(sid: str):
    row = _get_or_404(sid)
    with _lock_for(sid):
        if row["state"] != "READY":
            raise HTTPException(409, f"cannot stop from state {row['state']}")
        try:
            client = engine._client()
            for c in client.containers.list(
                    filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.stop(timeout=5)
        except engine.EngineDown:
            raise _503()
        store.set_state(sid, "STOPPED")
        store.add_event(sid, "state", "STOPPED")
    return _view(store.get(sid))


@app.post("/sandboxes/{sid}/start")
def start_sandbox(sid: str):
    row = _get_or_404(sid)
    with _lock_for(sid):
        if row["state"] != "STOPPED":
            raise HTTPException(409, f"cannot start from state {row['state']}")
        try:
            client = engine._client()
            for c in client.containers.list(
                    all=True, filters={"label": f"{config.LABEL_ID}={sid}"}):
                c.start()
        except engine.EngineDown:
            raise _503()
        store.set_state(sid, "READY")
        store.add_event(sid, "state", "READY (started)")
    return _view(store.get(sid))


@app.get("/status")
def platform_status():
    """One-call overview for the console: engine health + pool usage."""
    dind_up = engine.ping()
    used = sorted(store.used_ports())
    pool = {"start": config.POOL_START, "end": config.POOL_END,
            "used": used,
            "free": [p for p in range(config.POOL_START, config.POOL_END + 1)
                     if p not in used]}
    return {"api": "up", "dind": "up" if dind_up else "down",
            "pool": pool, "max_sandboxes": config.MAX_SANDBOXES,
            "sandboxes": len(store.list_all())}


@app.get("/sandboxes/{sid}/containers")
def sandbox_containers(sid: str):
    """What's INSIDE the sandbox, live from the engine."""
    _get_or_404(sid)
    try:
        return engine.sandbox_containers(sid)
    except engine.EngineDown:
        raise _503()


@app.post("/sandboxes/{sid}/exec")
def exec_sandbox(sid: str, req: ExecRequest):
    row = _get_or_404(sid)
    if row["state"] != "READY":                        # edge case #6a
        raise HTTPException(409, f"exec requires READY (state={row['state']})")
    try:
        result = engine.exec_in_sandbox(sid, req.cmd, req.timeout,
                                        req.container)
    except engine.EngineDown:
        raise _503()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    store.add_event(sid, "action",
                    f"exec {' '.join(req.cmd)[:120]} -> "
                    f"exit={result['exit_code']} timed_out={result['timed_out']}")
    return result


@app.get("/sandboxes/{sid}/events")
def sandbox_events(sid: str):
    # The event log is the audit trail: it stays readable after deletion.
    # 404 only if the sandbox NEVER existed (no row at all).
    if not store.get(sid):
        raise HTTPException(404, f"sandbox {sid} never existed")
    return store.events(sid)                           # edge case #12


@app.delete("/sandboxes/{sid}", status_code=204)
def delete_sandbox(sid: str):
    row = store.get(sid)
    # -- edge case #7: delete idempotency ---------------------------------
    if not row or row["state"] == "DELETED":
        raise HTTPException(404, f"sandbox {sid} not found")
    if row["state"] == "CREATING":
        raise HTTPException(409, "sandbox is CREATING; retry in a moment")
    with _lock_for(sid):
        fresh = store.get(sid)
        if not fresh or fresh["state"] == "DELETED":
            raise HTTPException(404, f"sandbox {sid} not found")
        try:
            _destroy_and_mark(sid, "manual delete")
        except engine.EngineDown:
            raise _503()
    return JSONResponse(status_code=204, content=None)


# -- web console (static SPA) -------------------------------------------------
# Registered AFTER every API route so nothing is shadowed. The console is a
# dependency-free vanilla JS app - the "window into the platform".
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
