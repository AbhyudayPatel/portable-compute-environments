"""sandbox-mcp - the platform as a Model Context Protocol server.

Any MCP-capable agent (Claude Code, IDE agents, custom harnesses) can
create and drive sandboxes with standard tool calls instead of a bespoke
RPC. Every tool is a thin, typed wrapper over the platform's REST APIs:

  sandbox-api          lifecycle, exec, files, fingerprints, GUI, egress policy
  sandbox-checkpoint   checkpoint / fork / restore

Multi-tenancy: a bearer token identifies a tenant (MCP_TOKENS="tok1:alice,
tok2:bob"). Sandboxes created through this server are named
"<tenant>-<name>"; list/get/exec only see the caller's own sandboxes.
Every call is recorded (tool, tenant, sandbox, args digest, duration,
outcome) in SQLite and exposed at GET /calls - a replayable audit trail.

Transport: Streamable HTTP at /mcp (stateless). Side endpoints: /healthz,
/calls, /tools (plain JSON for curl).
"""
import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextvars import ContextVar

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-mcp")

API = os.environ.get("SANDBOX_API_URL", "http://api:8000").rstrip("/")
CKPT = os.environ.get("CHECKPOINT_URL", "").rstrip("/")
DB = os.environ.get("DB_PATH", "/data/mcp.db")
TOKENS = {t.split(":")[0]: t.split(":")[1] for t in os.environ.get("MCP_TOKENS", "dev-token:dev").split(",") if ":" in t}

current_tenant: ContextVar[str] = ContextVar("tenant", default="anon")
_lock = threading.Lock()


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tenant TEXT, "
                  "tool TEXT, sandbox TEXT, args_sha TEXT, ms INTEGER, ok INTEGER, error TEXT)")


def record(tool: str, args: dict, sandbox: str | None, t0: float, ok: bool, error: str | None = None):
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
    with _lock, conn() as c:
        c.execute("INSERT INTO calls(ts,tenant,tool,sandbox,args_sha,ms,ok,error) VALUES (?,?,?,?,?,?,?,?)",
                  (time.time(), current_tenant.get(), tool, sandbox, digest, int((time.time() - t0) * 1000),
                   int(ok), (error or "")[:300]))


class ToolError(Exception):
    pass


def api(method: str, path: str, **kw) -> requests.Response:
    kw.setdefault("timeout", 120)
    try:
        r = requests.request(method, API + path, **kw)
    except requests.RequestException as e:
        raise ToolError(f"sandbox-api unreachable: {e}")
    if r.status_code >= 400:
        try:
            d = r.json().get("detail")
        except ValueError:
            d = r.text[:200]
        raise ToolError(f"sandbox-api {r.status_code}: {d}")
    return r


def tenant_name(name: str) -> str:
    return f"{current_tenant.get()}-{name}"


def own(sid: str) -> dict:
    """Fetch a sandbox and prove it belongs to the caller's tenant."""
    s = api("GET", f"/sandboxes/{sid}").json()
    if not s["name"].startswith(current_tenant.get() + "-"):
        raise ToolError(f"sandbox {sid} is not yours")
    return s


def tracked(tool):
    """Decorator: record every call with tenant, sandbox, timing, outcome."""
    import functools

    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        sid = kwargs.get("sandbox_id") or (args[0] if args and isinstance(args[0], str) and len(args[0]) == 12 else None)
        try:
            out = tool(*args, **kwargs)
            record(tool.__name__, kwargs, sid, t0, True)
            return out
        except ToolError as e:
            record(tool.__name__, kwargs, sid, t0, False, str(e))
            raise
        except Exception as e:
            record(tool.__name__, kwargs, sid, t0, False, repr(e))
            raise ToolError(repr(e))
    return wrapper


mcp = FastMCP("agentos-sandboxes", stateless_http=True, json_response=True)


# -- lifecycle ----------------------------------------------------------------

@mcp.tool()
@tracked
def list_templates() -> dict:
    """Available sandbox templates with their images and isolation modes."""
    return api("GET", "/templates").json()


@mcp.tool()
@tracked
def create_sandbox(name: str, template: str = "web", isolation: str = "container",
                   stateful: bool = False, egress_mode: str | None = None,
                   egress_allow: list[str] | None = None, ttl_seconds: int | None = None) -> dict:
    """Create a sandbox (idempotent by name within your tenant). Returns id,
    state, port, url. Poll wait_ready afterwards. egress_mode: deny|allow|record|replay."""
    options: dict = {}
    if stateful:
        options["stateful"] = True
    if egress_mode:
        options["egress"] = {"mode": egress_mode, "allow": egress_allow or []}
    body = {"name": tenant_name(name), "template": template, "isolation": isolation,
            "ttl_seconds": ttl_seconds, **({"options": options} if options else {})}
    s = api("POST", "/sandboxes", json=body).json()
    return {k: s[k] for k in ("id", "name", "state", "port", "url", "template", "isolation")}


@mcp.tool()
@tracked
def wait_ready(sandbox_id: str, timeout_seconds: int = 180) -> dict:
    """Block until the sandbox is READY (or FAILED). Returns the final state."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        s = own(sandbox_id)
        if s["state"] in ("READY", "FAILED", "DEGRADED"):
            return {"id": sandbox_id, "state": s["state"], "url": s["url"], "error": s["error"]}
        time.sleep(2)
    raise ToolError("timed out waiting for READY")


@mcp.tool()
@tracked
def list_sandboxes() -> list[dict]:
    """Your tenant's sandboxes."""
    pre = current_tenant.get() + "-"
    return [{k: s[k] for k in ("id", "name", "state", "template", "port", "url")}
            for s in api("GET", "/sandboxes").json() if s["name"].startswith(pre)]


@mcp.tool()
@tracked
def get_sandbox(sandbox_id: str) -> dict:
    """Inspect one sandbox (state, port, containers)."""
    s = own(sandbox_id)
    s["containers"] = [{k: c[k] for k in ("name", "role", "status", "ip")} for c in api("GET", f"/sandboxes/{sandbox_id}/containers").json()]
    return s


@mcp.tool()
@tracked
def delete_sandbox(sandbox_id: str) -> dict:
    """Destroy a sandbox (its event log stays readable)."""
    own(sandbox_id)
    api("DELETE", f"/sandboxes/{sandbox_id}")
    return {"id": sandbox_id, "deleted": True}


# -- runtime ------------------------------------------------------------------

@mcp.tool()
@tracked
def exec(sandbox_id: str, cmd: list[str], timeout_seconds: int = 60, container: str | None = None) -> dict:
    """Run a command inside the sandbox (killed on timeout). Returns exit_code, stdout, stderr."""
    own(sandbox_id)
    body = {"cmd": cmd, "timeout": timeout_seconds}
    if container:
        body["container"] = container
    r = api("POST", f"/sandboxes/{sandbox_id}/exec", json=body, timeout=timeout_seconds + 30).json()
    return {k: r.get(k) for k in ("exit_code", "stdout", "stderr", "timed_out", "truncated", "container")}


@mcp.tool()
@tracked
def read_file(sandbox_id: str, path: str, container: str | None = None, max_bytes: int = 1 << 20) -> str:
    """Read a text file from the sandbox."""
    own(sandbox_id)
    params = {"path": path, "max": max_bytes}
    if container:
        params["container"] = container
    return api("GET", f"/sandboxes/{sandbox_id}/fs/read", params=params).content.decode(errors="replace")


@mcp.tool()
@tracked
def write_file(sandbox_id: str, path: str, content: str, container: str | None = None, mode: str = "0644") -> dict:
    """Write a text file into the sandbox (parents created)."""
    own(sandbox_id)
    params = {"path": path, "mode": mode}
    if container:
        params["container"] = container
    return api("PUT", f"/sandboxes/{sandbox_id}/fs/write", params=params, data=content.encode()).json()


@mcp.tool()
@tracked
def list_dir(sandbox_id: str, path: str, container: str | None = None) -> list[dict]:
    """List a directory inside the sandbox."""
    own(sandbox_id)
    params = {"path": path}
    if container:
        params["container"] = container
    return api("GET", f"/sandboxes/{sandbox_id}/fs", params=params).json()["entries"]


@mcp.tool()
@tracked
def fingerprint(sandbox_id: str, path: str | None = None, container: str | None = None) -> dict:
    """Merkle fingerprint (root hash, counts) of a path - compare states cheaply."""
    own(sandbox_id)
    params = {}
    if path:
        params["path"] = path
    if container:
        params["container"] = container
    fp = api("GET", f"/sandboxes/{sandbox_id}/fingerprint", params=params).json()
    return {k: fp[k] for k in ("root", "path", "files", "dirs", "bytes")}


@mcp.tool()
@tracked
def diff(sandbox_a: str, sandbox_b: str, path: str | None = None) -> dict:
    """Semantic diff of two sandboxes (files added/removed/modified; DB rows for coreapp)."""
    own(sandbox_a)
    own(sandbox_b)
    params = {"a": sandbox_a, "b": sandbox_b}
    if path:
        params["path"] = path
    return api("GET", "/diff", params=params).json()


@mcp.tool()
@tracked
def events(sandbox_id: str, since: int = 0) -> list[dict]:
    """The sandbox's ordered event log (what the platform did to it)."""
    own(sandbox_id)
    return api("GET", f"/sandboxes/{sandbox_id}/events", params={"since": since}).json()


# -- gui ----------------------------------------------------------------------

@mcp.tool()
@tracked
def screenshot(sandbox_id: str) -> Image:
    """PNG screenshot of a desktop sandbox."""
    own(sandbox_id)
    png = api("GET", f"/sandboxes/{sandbox_id}/screenshot", timeout=60).content
    return Image(data=png, format="png")


@mcp.tool()
@tracked
def gui_action(sandbox_id: str, type: str, x: int = 0, y: int = 0, text: str = "", keys: str = "",
               x2: int = 0, y2: int = 0, button: int = 1, count: int = 1, direction: str = "down") -> dict:
    """Drive a desktop sandbox: click|dblclick|rightclick|move|type|key|scroll|drag."""
    own(sandbox_id)
    body = {"type": type, "x": x, "y": y, "text": text, "keys": keys, "x2": x2, "y2": y2,
            "button": button, "count": count, "direction": direction}
    return api("POST", f"/sandboxes/{sandbox_id}/gui", json=body, timeout=60).json()


# -- egress policy --------------------------------------------------------------

@mcp.tool()
@tracked
def set_egress(sandbox_id: str, mode: str, allow: list[str] | None = None) -> dict:
    """Change a sandbox's live egress policy: deny|allow|record|replay."""
    own(sandbox_id)
    return api("PUT", f"/sandboxes/{sandbox_id}/egress", json={"mode": mode, "allow": allow or []}).json()


# -- checkpoints ----------------------------------------------------------------

def ckpt(method: str, path: str, **kw) -> requests.Response:
    if not CKPT:
        raise ToolError("checkpoint service not configured")
    kw.setdefault("timeout", 300)
    try:
        r = requests.request(method, CKPT + path, **kw)
    except requests.RequestException as e:
        raise ToolError(f"checkpoint service unreachable: {e}")
    if r.status_code >= 400:
        raise ToolError(f"checkpoint {r.status_code}: {r.text[:200]}")
    return r


@mcp.tool()
@tracked
def checkpoint(sandbox_id: str, label: str = "") -> dict:
    """Checkpoint a stateful sandbox (filesystem + database). Returns the checkpoint id."""
    own(sandbox_id)
    m = ckpt("POST", f"/sandboxes/{sandbox_id}/checkpoints", json={"label": label, "ship": True}).json()
    return {"id": m["id"], "parent": m.get("parent"), "lsn": m.get("lsn"), "layers": list(m["layers"])}


@mcp.tool()
@tracked
def fork(checkpoint_id: str, name: str) -> dict:
    """New sandbox from a checkpoint (copy-on-write)."""
    s = ckpt("POST", f"/checkpoints/{checkpoint_id}/fork", json={"name": tenant_name(name)}).json()
    return {k: s[k] for k in ("id", "name", "state", "port", "url")}


@mcp.tool()
@tracked
def restore(checkpoint_id: str, sandbox_id: str | None = None) -> dict:
    """Roll a sandbox back onto a checkpoint in place."""
    if sandbox_id:
        own(sandbox_id)
    return ckpt("POST", f"/checkpoints/{checkpoint_id}/restore", json={"sandbox": sandbox_id}).json()


# -- resources ------------------------------------------------------------------

@mcp.resource("sandbox://{sandbox_id}/events")
def events_resource(sandbox_id: str) -> str:
    """Event log of a sandbox as NDJSON."""
    own(sandbox_id)
    ev = api("GET", f"/sandboxes/{sandbox_id}/events").json()
    return "\n".join(json.dumps(e) for e in ev)


# -- HTTP app: auth middleware + side endpoints -----------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp"):
            tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            tenant = TOKENS.get(tok)
            if not tenant:
                return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)
            token = current_tenant.set(tenant)
            try:
                return await call_next(request)
            finally:
                current_tenant.reset(token)
        return await call_next(request)


async def healthz(request):
    ok = True
    try:
        api("GET", "/healthz", timeout=5)
    except ToolError:
        ok = False
    return JSONResponse({"mcp": "up", "sandbox_api": "up" if ok else "down",
                         "checkpoint": "configured" if CKPT else "off", "tenants": sorted(TOKENS.values())})


async def calls(request):
    limit = int(request.query_params.get("limit", "100"))
    with conn() as c:
        rows = c.execute("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return JSONResponse([dict(r) for r in rows])


async def tools(request):
    return JSONResponse(sorted(t.name for t in (await mcp.list_tools())))


mcp_app = mcp.streamable_http_app()
app = Starlette(routes=[Route("/healthz", healthz), Route("/calls", calls), Route("/tools", tools),
                        Mount("/", app=mcp_app)],
                lifespan=mcp_app.router.lifespan_context)
app.add_middleware(AuthMiddleware)
init_db()
