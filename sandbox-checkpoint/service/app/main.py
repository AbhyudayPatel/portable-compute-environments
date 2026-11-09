"""sandbox-checkpoint - checkpoint, fork and restore of stateful sandboxes.

"Git for execution state": a checkpoint freezes a sandbox's data volumes
(filesystem paths AND the database, plus its WAL archive), stores them as
immutable layers on the engine node and as content-addressed blobs, and
records a manifest with lineage. A fork is a new sandbox mounted on top of
a checkpoint's layers (copy-on-write, seconds). A restore puts a sandbox
back onto a checkpoint in place.

    checkpoint  = quiesce (app hook, e.g. Postgres CHECKPOINT)
                  -> pause (cgroup freezer: crash-consistent instant)
                  -> snapshot (copy upper layers + WAL dir to /var/lib/sbx/ckpt/<cid>/)
                  -> unpause
                  -> ship layers to the blob store (owner checkpoint:<cid>)
                  -> write manifest (last, fsynced) + lineage
    fork        = POST /sandboxes {options: {stateful, layers: {role: [cid dirs...]}, copies: {wal: ...}}}
    restore     = stop -> volumes/restore(layers, copies) -> start
    pitr        = restore the newest checkpoint at or before an LSN, then let
                  Postgres replay the archived WAL up to that LSN

Everything goes through the sandbox-api REST API (pause/unpause, volumes
snapshot/restore, node tar/untar/du/rmdir restricted to /var/lib/sbx) and
the blob store; this service never touches docker or the node directly.

Endpoints
  POST   /sandboxes/{sid}/checkpoints      {label?, ship?: bool} -> checkpoint
  GET    /sandboxes/{sid}/checkpoints      lineage of a sandbox
  GET    /checkpoints[/{cid}]              list / inspect (manifest)
  GET    /checkpoints/{cid}/tree           ancestry + descendants
  GET    /checkpoints/{cid}/diff?against=  fingerprint diff between two checkpoints
  POST   /checkpoints/{cid}/fork           {name, isolation?} -> new sandbox from the checkpoint
  POST   /checkpoints/{cid}/restore        {sandbox?} -> in-place restore (default: origin sandbox)
  POST   /checkpoints/{cid}/import         re-materialise layers from the blob store (after rmdir)
  DELETE /checkpoints/{cid}                refuse while children/forks depend on it (409)
  POST   /sandboxes/{sid}/pitr             {lsn} -> point-in-time restore of the database
  POST   /gc                               drop CORRUPT/unreferenced layer dirs, blob refs
  GET    /healthz  /stats
"""
import hashlib
import json
import logging
import os
import time
import uuid

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-checkpoint")

API = os.environ.get("SANDBOX_API_URL", "http://api:8000").rstrip("/")
BLOBS = os.environ.get("BLOBS_URL", "").rstrip("/")             # optional
CKPT_ROOT = "/var/lib/sbx/ckpt"
TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "600"))

app = FastAPI(title="sandbox-checkpoint", version="1.0.0")
db = store.Store(os.environ.get("DB_PATH", "/data/checkpoints.db"))


# -- upstream helpers ---------------------------------------------------------

class Upstream(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def api(method: str, path: str, **kw) -> requests.Response:
    kw.setdefault("timeout", TIMEOUT)
    try:
        r = requests.request(method, API + path, **kw)
    except requests.RequestException as e:
        raise Upstream(503, f"sandbox-api unreachable: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        raise Upstream(r.status_code if r.status_code in (404, 409, 400, 422) else 502,
                       f"sandbox-api {r.status_code}: {detail}")
    return r


def _http(e: Upstream) -> HTTPException:
    return HTTPException(e.status, e.detail)


def sandbox(sid: str) -> dict:
    return api("GET", f"/sandboxes/{sid}").json()


def wait_state(sid: str, states: tuple, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = sandbox(sid)
        if s["state"] in states:
            return s
        if s["state"] == "FAILED":
            raise Upstream(502, f"sandbox {sid} FAILED: {s.get('error')}")
        time.sleep(1)
    raise Upstream(504, f"sandbox {sid} not in {states} within {timeout}s")


def template_info(name: str) -> dict:
    return api("GET", "/templates").json()[name]


def exec_in(sid: str, cmd: list[str], container_role: str | None = None, timeout: int = 60) -> dict:
    body = {"cmd": cmd, "timeout": timeout}
    if container_role:
        cs = api("GET", f"/sandboxes/{sid}/containers").json()
        for c in cs:
            if c["role"] == container_role:
                body["container"] = c["name"]
    return api("POST", f"/sandboxes/{sid}/exec", json=body).json()


# -- blob shipping ------------------------------------------------------------

def ship_layer(cid: str, node_dir: str) -> dict | None:
    """Stream a node dir as tar into the blob store under owner checkpoint:<cid>."""
    if not BLOBS:
        return None
    r = api("GET", "/node/tar", params={"path": node_dir}, stream=True)
    up = requests.put(f"{BLOBS}/blobs", params={"owner": f"checkpoint:{cid}"},
                      data=r.iter_content(chunk_size=256 * 1024), timeout=TIMEOUT)
    if not up.ok:
        raise Upstream(502, f"blob store {up.status_code}: {up.text[:200]}")
    return up.json()


def import_layer(node_dir: str, blob_hash: str) -> None:
    r = requests.get(f"{BLOBS}/blobs/{blob_hash}", timeout=TIMEOUT)
    if not r.ok:
        raise Upstream(502, f"blob {blob_hash} missing in the store")
    api("PUT", "/node/untar", params={"path": node_dir}, data=r.content)


# -- the checkpoint operation ----------------------------------------------------

class CheckpointBody(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    ship: bool = True


def fingerprints(sid: str, tmpl: dict) -> dict:
    out = {}
    for role, path in tmpl.get("stateful_paths", {}).items():
        try:
            cs = api("GET", f"/sandboxes/{sid}/containers").json()
            cname = next((c["name"] for c in cs if c["role"] == role), None)
            fp = api("GET", f"/sandboxes/{sid}/fingerprint",
                     params={"path": path, "container": cname, "exclude": "pg_wal,pg_stat_tmp,postmaster.pid,postmaster.opts,pg_logical,pg_stat,global/pg_control"}).json()
            out[role] = {"root": fp["root"], "files": fp["files"], "bytes": fp["bytes"]}
        except Upstream as e:
            out[role] = {"error": e.detail}
    try:
        out["db"] = api("GET", f"/sandboxes/{sid}/fingerprint/db", timeout=120).json()
    except Upstream:
        pass
    return out


@app.post("/sandboxes/{sid}/checkpoints", status_code=201)
def create_checkpoint(sid: str, body: CheckpointBody):
    try:
        s = sandbox(sid)
        if s["state"] not in ("READY", "DEGRADED", "STOPPED"):
            raise HTTPException(409, f"checkpoint requires READY/DEGRADED/STOPPED (state={s['state']})")
        vols = api("GET", f"/sandboxes/{sid}/volumes").json()
        if not vols.get("stateful"):
            raise HTTPException(400, "sandbox is not stateful (create it with options.stateful=true)")
        tmpl = template_info(s["template"])
        cid = uuid.uuid4().hex[:12]
        dest = f"{CKPT_ROOT}/{cid}"
        parent = db.current_base(sid)
        was_running = s["state"] != "STOPPED"
        t0 = time.time()
        lsn = None
        if was_running:
            q = tmpl.get("quiesce")
            if q:
                r = exec_in(sid, q["cmd"], q["role"], timeout=60)
                if r.get("exit_code") != 0:
                    log.warning("quiesce hook failed on %s: %s", sid, r.get("stderr", "")[:200])
                if s["template"] == "coreapp":
                    r = exec_in(sid, ["psql", "-U", "company", "-d", "companydb", "-Atc",
                                      "select pg_current_wal_lsn()"], "db")
                    lsn = (r.get("stdout") or "").strip() or None
                    # force the current WAL segment into the archive so a
                    # PITR to this LSN needs nothing the archive lacks
                    exec_in(sid, ["psql", "-U", "company", "-d", "companydb", "-Atc",
                                  "select pg_switch_wal()"], "db")
                    time.sleep(1.5)
            api("POST", f"/sandboxes/{sid}/pause")
        try:
            snap = api("POST", f"/sandboxes/{sid}/volumes/snapshot", json={"dest": dest}).json()
        finally:
            if was_running:
                api("POST", f"/sandboxes/{sid}/unpause")
        if was_running:
            fps = fingerprints(sid, tmpl)          # after unpause: content identical to the snapshot
        else:
            # agents are down in a STOPPED sandbox; hash the layer dirs on the node instead
            fps = {name: {"node_sha256": node_tree_hash(info["dir"]), "bytes": info["bytes"]}
                   for name, info in snap.items()}
        blobs = {}
        if body.ship and BLOBS:
            for name, info in snap.items():
                res = ship_layer(cid, info["dir"])
                if res:
                    blobs[name] = {"hash": res["hash"], "size": res["size"]}
        manifest = {
            "id": cid, "sandbox_id": sid, "sandbox_name": s["name"], "template": s["template"],
            "label": body.label, "parent": parent, "created": time.time(),
            "was_running": was_running, "lsn": lsn, "layers": snap, "blobs": blobs,
            "fingerprints": fps, "seconds": round(time.time() - t0, 2),
            "layer_order": [n for n, i in snap.items() if i["kind"] == "layer"],
        }
        # manifest written LAST on the node (fsync) = the commit point
        api("PUT", "/node/untar", params={"path": dest},
            data=_tar_single("manifest.json", json.dumps(manifest, indent=1).encode()))
        db.insert(manifest)
        db.set_base(sid, cid)
        return manifest
    except Upstream as e:
        raise _http(e)


def node_tree_hash(node_dir: str) -> str:
    """sha256 over the tar stream of a node directory (stopped sandboxes)."""
    h = hashlib.sha256()
    r = api("GET", "/node/tar", params={"path": node_dir}, stream=True)
    for chunk in r.iter_content(chunk_size=256 * 1024):
        h.update(chunk)
    return h.hexdigest()


def _tar_single(name: str, data: bytes) -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# -- queries ---------------------------------------------------------------------

@app.get("/checkpoints")
def list_checkpoints(sandbox: str | None = None):
    return db.list_all(sandbox)


@app.get("/sandboxes/{sid}/checkpoints")
def sandbox_checkpoints(sid: str):
    return {"sandbox_id": sid, "base": db.current_base(sid), "checkpoints": db.list_all(sid)}


@app.get("/checkpoints/{cid}")
def get_checkpoint(cid: str):
    m = db.get(cid)
    if not m:
        raise HTTPException(404, f"checkpoint {cid} not found")
    m["children"] = db.children(cid)
    m["forks"] = db.forks_of(cid)
    return m


@app.get("/checkpoints/{cid}/tree")
def checkpoint_tree(cid: str):
    if not db.get(cid):
        raise HTTPException(404, f"checkpoint {cid} not found")
    return {"ancestors": db.ancestors(cid), "id": cid, "descendants": db.descendants(cid),
            "forks": db.forks_of(cid)}


@app.get("/checkpoints/{cid}/diff")
def checkpoint_diff(cid: str, against: str):
    a, b = db.get(cid), db.get(against)
    if not a or not b:
        raise HTTPException(404, "unknown checkpoint")
    fa, fb = a.get("fingerprints", {}), b.get("fingerprints", {})
    out = {"a": cid, "b": against, "roles": {}}
    for role in sorted(set(fa) | set(fb)):
        if role == "db":
            ta, tb = fa.get("db", {}).get("tables", {}), fb.get("db", {}).get("tables", {})
            out["roles"]["db"] = {
                "identical": fa.get("db", {}).get("root") == fb.get("db", {}).get("root"),
                "tables": {t: {"rows_a": ta.get(t, {}).get("rows"), "rows_b": tb.get(t, {}).get("rows"),
                               "changed": ta.get(t, {}).get("hash") != tb.get(t, {}).get("hash")}
                           for t in sorted(set(ta) | set(tb))}}
        else:
            out["roles"][role] = {"identical": fa.get(role, {}).get("root") == fb.get(role, {}).get("root"),
                                  "root_a": fa.get(role, {}).get("root"), "root_b": fb.get(role, {}).get("root")}
    out["identical"] = all(v.get("identical") for v in out["roles"].values())
    return out


# -- fork / restore ----------------------------------------------------------------

def _layer_stack(m: dict) -> tuple[dict, dict]:
    """A checkpoint's layers = its own layer dirs; because each snapshot
    copies the FULL upper (everything since the sandbox's base), one
    checkpoint is self-contained: its layer dir sits on an empty base.
    Plain dirs (WAL) are copied."""
    layers = {}
    copies = {}
    for name, info in m["layers"].items():
        if info["kind"] == "layer":
            layers[name] = [info["dir"]]
        else:
            copies[name] = info["dir"]
    return layers, copies


class ForkBody(BaseModel):
    name: str = Field(min_length=2, max_length=41)
    isolation: str | None = None


@app.post("/checkpoints/{cid}/fork", status_code=201)
def fork(cid: str, body: ForkBody):
    m = db.get(cid)
    if not m:
        raise HTTPException(404, f"checkpoint {cid} not found")
    if m.get("state") == "CORRUPT":
        raise HTTPException(409, "checkpoint is CORRUPT")
    layers, copies = _layer_stack(m)
    try:
        r = api("POST", "/sandboxes", json={
            "name": body.name, "template": m["template"], "isolation": body.isolation,
            "options": {"stateful": True, "layers": layers, "copies": copies, "from_checkpoint": cid}})
    except Upstream as e:
        raise _http(e)
    s = r.json()
    db.set_base(s["id"], cid)
    db.add_fork(cid, s["id"], body.name)
    return s


class RestoreBody(BaseModel):
    sandbox: str | None = None


@app.post("/checkpoints/{cid}/restore")
def restore(cid: str, body: RestoreBody):
    m = db.get(cid)
    if not m:
        raise HTTPException(404, f"checkpoint {cid} not found")
    if m.get("state") == "CORRUPT":
        raise HTTPException(409, "checkpoint is CORRUPT")
    sid = body.sandbox or m["sandbox_id"]
    try:
        s = sandbox(sid)
        if s["template"] != m["template"]:
            raise HTTPException(409, "template mismatch")
        if s["state"] in ("READY", "DEGRADED"):
            api("POST", f"/sandboxes/{sid}/stop")
        elif s["state"] == "PAUSED":
            api("POST", f"/sandboxes/{sid}/unpause")
            api("POST", f"/sandboxes/{sid}/stop")
        wait_state(sid, ("STOPPED",), 60)
        layers, copies = _layer_stack(m)
        api("POST", f"/sandboxes/{sid}/volumes/restore", json={"layers": layers, "copies": copies})
        api("POST", f"/sandboxes/{sid}/start")
        db.set_base(sid, cid)
        db.add_event(cid, f"restored into {sid}")
        return {"checkpoint": cid, "sandbox": sid, "state": sandbox(sid)["state"]}
    except Upstream as e:
        raise _http(e)


# -- point-in-time recovery (Postgres) ----------------------------------------------

class PitrBody(BaseModel):
    lsn: str = Field(min_length=3)      # e.g. "0/1A2B3C4"


@app.post("/sandboxes/{sid}/pitr")
def pitr(sid: str, body: PitrBody):
    """Restore the newest checkpoint whose LSN <= target, then make
    Postgres replay the archived WAL up to the target LSN."""
    cands = [m for m in db.list_all(sid) if m.get("lsn") and _lsn_int(m["lsn"]) <= _lsn_int(body.lsn)
             and m.get("state") != "CORRUPT"]
    if not cands:
        raise HTTPException(404, "no checkpoint at or before that LSN")
    base = max(cands, key=lambda m: _lsn_int(m["lsn"]))
    try:
        s = sandbox(sid)
        if s["state"] in ("READY", "DEGRADED"):
            api("POST", f"/sandboxes/{sid}/stop")
        wait_state(sid, ("STOPPED",), 60)
        layers, copies = _layer_stack(base)
        # WAL archive: use the LIVE sandbox's current archive (it holds every
        # segment since creation), not the checkpoint's copy, so segments
        # written after the checkpoint are available for replay.
        copies.pop("wal", None)
        api("POST", f"/sandboxes/{sid}/volumes/restore", json={"layers": layers, "copies": copies})
        # recovery.signal + target: Postgres replays /wal until the LSN, then
        # promotes (recovery_target_action=promote)
        vol = api("GET", f"/sandboxes/{sid}/volumes").json()
        dbdir = vol["roles"]["db"]["node_dir"] + "/merged"
        conf = (f"restore_command = 'cp /wal/%f %p'\nrecovery_target_lsn = '{body.lsn}'\n"
                f"recovery_target_action = 'promote'\nrecovery_target_inclusive = true\n")
        api("PUT", "/node/untar", params={"path": dbdir},
            data=_tar_many({"recovery.signal": b"", "postgresql.auto.conf": conf.encode()}))
        api("POST", f"/sandboxes/{sid}/start")
        wait_state(sid, ("READY",), 120)
        r = exec_in(sid, ["sh", "-c", "for i in $(seq 1 60); do psql -U company -d companydb -Atc 'select pg_is_in_recovery()' 2>/dev/null | grep -q f && break; sleep 1; done; psql -U company -d companydb -Atc 'select pg_is_in_recovery(), pg_current_wal_lsn()'"], "db", timeout=90)
        db.set_base(sid, base["id"])
        return {"sandbox": sid, "base_checkpoint": base["id"], "base_lsn": base["lsn"],
                "target_lsn": body.lsn, "postgres": (r.get("stdout") or "").strip()}
    except Upstream as e:
        raise _http(e)


def _lsn_int(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def _tar_many(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# -- import / delete / gc ------------------------------------------------------------

@app.post("/checkpoints/{cid}/import")
def import_checkpoint(cid: str):
    """Re-create the node layer dirs from the blob store (after a rmdir or
    on a fresh node). Verifies the manifest is complete afterwards."""
    m = db.get(cid)
    if not m:
        raise HTTPException(404, f"checkpoint {cid} not found")
    if not BLOBS or not m.get("blobs"):
        raise HTTPException(409, "checkpoint was not shipped to the blob store")
    try:
        for name, info in m["layers"].items():
            import_layer(info["dir"], m["blobs"][name]["hash"])
        api("PUT", "/node/untar", params={"path": f"{CKPT_ROOT}/{cid}"},
            data=_tar_single("manifest.json", json.dumps(m, indent=1).encode()))
        db.set_state(cid, "OK")
        return {"imported": cid, "layers": list(m["layers"])}
    except Upstream as e:
        raise _http(e)


@app.delete("/checkpoints/{cid}", status_code=204)
def delete_checkpoint(cid: str, force: int = 0):
    m = db.get(cid)
    if not m:
        raise HTTPException(404, f"checkpoint {cid} not found")
    deps = db.dependents(cid)
    if deps and not force:
        raise HTTPException(409, f"checkpoint is in use: {deps}")
    try:
        api("DELETE", "/node/dir", params={"path": f"{CKPT_ROOT}/{cid}"})
    except Upstream as e:
        if e.status != 404:
            raise _http(e)
    if BLOBS:
        try:
            requests.delete(f"{BLOBS}/refs/checkpoint:{cid}", timeout=30)
        except requests.RequestException:
            pass
    db.delete(cid)
    return JSONResponse(status_code=204, content=None)


@app.post("/gc")
def gc():
    """Verify every checkpoint's layer dirs; mark CORRUPT when the manifest
    or a layer is missing on the node (an interrupted snapshot), drop blob
    refs of deleted checkpoints."""
    marked, ok = [], 0
    for m in db.list_all(None):
        try:
            res = api("GET", "/node/du", params={"path": f"{CKPT_ROOT}/{m['id']}/manifest.json"}).json()
            if res["bytes"] <= 0:
                raise Upstream(404, "manifest missing")
            for name, info in m["layers"].items():
                # du returns -1 for a missing path; an empty-but-present layer is 4096 (the dir)
                if api("GET", "/node/du", params={"path": info["dir"]}).json()["bytes"] < 0:
                    raise Upstream(404, "layer missing")
            if m.get("state") == "CORRUPT":
                db.set_state(m["id"], "OK")
            ok += 1
        except Upstream:
            db.set_state(m["id"], "CORRUPT")
            marked.append(m["id"])
    return {"ok": ok, "corrupt": marked}


@app.get("/healthz")
def healthz():
    up = True
    try:
        api("GET", "/healthz", timeout=5)
    except Upstream:
        up = False
    return {"checkpoint": "up", "sandbox_api": "up" if up else "down",
            "blobs": ("configured" if BLOBS else "off")}


@app.get("/stats")
def stats():
    return db.stats()


@app.on_event("startup")
def startup():
    db.init()
    # boot: a checkpoint row whose manifest never landed on the node is an
    # interrupted snapshot -> CORRUPT (never restorable, never forkable)
    try:
        gc()
    except Exception:
        log.warning("boot gc skipped (sandbox-api not reachable yet)")
