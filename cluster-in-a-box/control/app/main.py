"""cluster-in-a-box - a distributed control plane over NESTED workers.

Every worker is a complete sandbox-api node (dockerd + agent proxy + API)
running as ONE sandbox of the outer sandbox-api - three engines deep:

    Docker Desktop -> outer dind -> worker container -> worker's dockerd -> sandboxes

This service (T12-lite) registers workers, heartbeats them, places
sandboxes across them by policy, migrates workspaces on drain, detects
dead workers, re-places their sandboxes elsewhere, and fences stale copies
when a worker returns. Because workers are sandboxes, chaos is one API
call away (stop/kill a worker through the outer API).

Endpoints
  POST   /workers {count, labels}        create N workers (outer sandbox-api, template `worker`)
  GET    /workers[/{id}]                 state (STARTING|ALIVE|SUSPECT|DEAD|DRAINING|GONE), epoch, placements
  POST   /workers/{id}/drain             migrate every placement away (workspace tar), then empty
  DELETE /workers/{id}                   delete the outer sandbox
  POST   /sandboxes {name, template, policy, labels, data_path}
                                         place: least-loaded | binpack | affinity
  GET    /sandboxes[/{id}]               merged view (worker, remote id, url, state)
  POST   /sandboxes/{id}/reschedule      re-place a sandbox whose worker is SUSPECT/DEAD
  DELETE /sandboxes/{id}
  GET    /status  /healthz
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cluster")

OUTER = os.environ.get("SANDBOX_API_URL", "http://api:8000").rstrip("/")
WORKER_HOST = os.environ.get("WORKER_HOST", "dind")          # where worker ports are bound
API_BASE = int(os.environ.get("WORKER_API_BASE", "9340"))
POOL_BASE = int(os.environ.get("WORKER_POOL_BASE", "9300"))
POOL_SIZE = int(os.environ.get("WORKER_POOL_SIZE", "10"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
WORKER_CAP = int(os.environ.get("WORKER_MAX_SANDBOXES", "6"))
HB = float(os.environ.get("HEARTBEAT_INTERVAL", "2"))
SUSPECT_MISSES = int(os.environ.get("SUSPECT_MISSES", "3"))
DEAD_MISSES = int(os.environ.get("DEAD_MISSES", "5"))
DB = os.environ.get("DB_PATH", "/data/cluster.db")

app = FastAPI(title="cluster-in-a-box", version="1.0.0")
_lock = threading.Lock()


def conn():
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, idx INTEGER NOT NULL, name TEXT NOT NULL, outer_sid TEXT,
  api_port INTEGER NOT NULL, pool_start INTEGER NOT NULL, pool_end INTEGER NOT NULL,
  labels TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, misses INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 1, last_seen REAL, created REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS placements (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, template TEXT NOT NULL, worker_id TEXT,
  remote_id TEXT, port INTEGER, state TEXT NOT NULL, labels TEXT NOT NULL DEFAULT '{}',
  data_path TEXT, policy TEXT, created REAL NOT NULL, moves INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS stale (          -- remote copies to purge when a worker returns
  worker_id TEXT NOT NULL, remote_id TEXT NOT NULL, placement_id TEXT NOT NULL, PRIMARY KEY (worker_id, remote_id));
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, subject TEXT, message TEXT);
"""


def event(kind, subject, message):
    with _lock, conn() as c:
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (time.time(), kind, subject, message))
    log.info("%s %s: %s", kind, subject, message)


class Down(Exception):
    pass


def outer(method, path, **kw):
    kw.setdefault("timeout", 60)
    try:
        r = requests.request(method, OUTER + path, **kw)
    except requests.RequestException as e:
        raise HTTPException(503, f"outer sandbox-api unreachable: {e}")
    if r.status_code >= 400:
        try:
            d = r.json().get("detail")
        except ValueError:
            d = r.text[:200]
        raise HTTPException(502 if r.status_code >= 500 else r.status_code, f"outer {r.status_code}: {d}")
    return r


def wapi(w: dict, method: str, path: str, **kw) -> requests.Response:
    """Call a worker's own sandbox-api."""
    kw.setdefault("timeout", 30)
    try:
        r = requests.request(method, f"http://{WORKER_HOST}:{w['api_port']}{path}", **kw)
    except requests.RequestException as e:
        raise Down(str(e))
    if r.status_code == 503:
        raise Down("worker engine down")
    return r


def wrow(wid: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
    if not r:
        raise HTTPException(404, f"worker {wid} not found")
    d = dict(r)
    d["labels"] = json.loads(d["labels"])
    return d


def workers_all() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM workers ORDER BY idx").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["labels"] = json.loads(d["labels"])
        out.append(d)
    return out


def wset(wid: str, **fields):
    with _lock, conn() as c:
        cols = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE workers SET {cols} WHERE id=?", (*fields.values(), wid))


def prow(pid: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM placements WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, f"placement {pid} not found")
    d = dict(r)
    d["labels"] = json.loads(d["labels"])
    return d


def placements_all(worker_id: str | None = None) -> list[dict]:
    with conn() as c:
        if worker_id:
            rows = c.execute("SELECT * FROM placements WHERE worker_id=? AND state!='DELETED' ORDER BY created", (worker_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM placements WHERE state!='DELETED' ORDER BY created").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["labels"] = json.loads(d["labels"])
        out.append(d)
    return out


def pset(pid: str, **fields):
    with _lock, conn() as c:
        cols = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE placements SET {cols} WHERE id=?", (*fields.values(), pid))


# -- workers ---------------------------------------------------------------------

class WorkersBody(BaseModel):
    count: int = Field(default=1, ge=1, le=4)
    labels: dict[str, str] = {}


@app.post("/workers", status_code=201)
def create_workers(body: WorkersBody):
    existing = workers_all()
    used = {w["idx"] for w in existing if w["state"] != "GONE"}
    if len(used) + body.count > MAX_WORKERS:
        raise HTTPException(429, f"max {MAX_WORKERS} workers")
    made = []
    for _ in range(body.count):
        idx = next(i for i in range(MAX_WORKERS) if i not in used)
        used.add(idx)
        wid = f"w{idx}"
        api_port = API_BASE + idx
        pool_start, pool_end = POOL_BASE + POOL_SIZE * idx, POOL_BASE + POOL_SIZE * idx + POOL_SIZE - 1
        extra_ports = {"8000/tcp": api_port, **{f"{p}/tcp": p for p in range(pool_start, pool_end + 1)}}
        r = outer("POST", "/sandboxes", json={
            "name": f"worker-{idx}", "template": "worker",
            "options": {"extra_ports": extra_ports,
                        "env": {"POOL_START": str(pool_start), "POOL_END": str(pool_end),
                                "MAX_SANDBOXES": str(WORKER_CAP), "SBX_NODE_TOKEN": "worker-node-token"}}}).json()
        with _lock, conn() as c:
            c.execute("INSERT OR REPLACE INTO workers(id,idx,name,outer_sid,api_port,pool_start,pool_end,labels,state,misses,epoch,last_seen,created,error)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (wid, idx, f"worker-{idx}", r["id"], api_port, pool_start, pool_end, json.dumps(body.labels),
                       "STARTING", 0, 1, None, time.time(), None))
        event("worker", wid, f"created as outer sandbox {r['id']} (api :{api_port}, pool {pool_start}-{pool_end})")
        made.append(wrow(wid))
    return made


@app.get("/workers")
def list_workers():
    out = []
    for w in workers_all():
        w["placements"] = len(placements_all(w["id"]))
        out.append(w)
    return out


@app.get("/workers/{wid}")
def get_worker(wid: str):
    w = wrow(wid)
    w["placements"] = placements_all(wid)
    try:
        w["remote_sandboxes"] = wapi(w, "GET", "/sandboxes", timeout=5).json()
    except Down:
        w["remote_sandboxes"] = None
    return w


def pick_worker(policy: str, labels: dict, exclude: set | None = None) -> dict:
    cands = [w for w in workers_all() if w["state"] == "ALIVE" and w["id"] not in (exclude or set())]
    if labels:
        cands = [w for w in cands if all(w["labels"].get(k) == v for k, v in labels.items())]
    loads = {w["id"]: len(placements_all(w["id"])) for w in cands}
    cands = [w for w in cands if loads[w["id"]] < WORKER_CAP]
    if not cands:
        raise HTTPException(503, "no ALIVE worker with capacity matches the request")
    if policy == "binpack":
        return max(cands, key=lambda w: (loads[w["id"]], -w["idx"]))
    return min(cands, key=lambda w: (loads[w["id"]], w["idx"]))        # least-loaded / affinity


# -- placements ------------------------------------------------------------------

class PlaceBody(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    template: str = "web"
    policy: str = Field(default="least-loaded", pattern="^(least-loaded|binpack|affinity)$")
    labels: dict[str, str] = {}
    data_path: str | None = None        # migrated on drain (e.g. /www)


def remote_create(w: dict, p: dict) -> dict:
    r = wapi(w, "POST", "/sandboxes", json={"name": f"c-{p['id']}", "template": p["template"]})
    if r.status_code >= 400:
        raise HTTPException(502, f"worker {w['id']} create failed: {r.text[:200]}")
    return r.json()


@app.post("/sandboxes", status_code=201)
def place(body: PlaceBody):
    w = pick_worker(body.policy, body.labels if body.policy == "affinity" else {})
    pid = uuid.uuid4().hex[:10]
    with _lock, conn() as c:
        c.execute("INSERT INTO placements(id,name,template,worker_id,remote_id,port,state,labels,data_path,policy,created,moves,error)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (pid, body.name, body.template, w["id"], None, None, "PLACING", json.dumps(body.labels),
                   body.data_path, body.policy, time.time(), 0, None))
    try:
        s = remote_create(w, {"id": pid, "template": body.template})
    except Down as e:
        pset(pid, state="FAILED", error=str(e))
        raise HTTPException(503, f"worker {w['id']} down: {e}")
    pset(pid, remote_id=s["id"], port=s.get("port"), state=s["state"])
    event("place", pid, f"{body.name} -> {w['id']} (policy {body.policy}) remote {s['id']} port {s.get('port')}")
    return view(prow(pid))


def view(p: dict) -> dict:
    p["url"] = f"http://localhost:{p['port']}" if p.get("port") else None
    return p


@app.get("/sandboxes")
def list_placements():
    return [view(p) for p in placements_all()]


@app.get("/sandboxes/{pid}")
def get_placement(pid: str):
    return view(prow(pid))


@app.delete("/sandboxes/{pid}", status_code=204)
def delete_placement(pid: str):
    p = prow(pid)
    if p["worker_id"] and p["remote_id"]:
        try:
            wapi(wrow(p["worker_id"]), "DELETE", f"/sandboxes/{p['remote_id']}")
        except (Down, HTTPException):
            pass
    pset(pid, state="DELETED")
    event("place", pid, "deleted")
    return None


def migrate(p: dict, src: dict | None, dst: dict, reason: str) -> dict:
    """Create the placement on dst; copy data_path from src if reachable;
    delete on src if reachable; remember a stale copy otherwise."""
    s = remote_create(dst, p)
    for _ in range(90):
        st = wapi(dst, "GET", f"/sandboxes/{s['id']}", timeout=10).json()
        if st["state"] in ("READY", "FAILED"):
            break
        time.sleep(2)
    copied = False
    if src and p.get("data_path") and p.get("remote_id"):
        try:
            tar = wapi(src, "GET", f"/sandboxes/{p['remote_id']}/fs/tar", params={"path": p["data_path"]}, timeout=120).content
            wapi(dst, "PUT", f"/sandboxes/{s['id']}/fs/tar", params={"path": p["data_path"]}, data=tar, timeout=120)
            copied = True
        except Down:
            pass
    old_rid, old_wid = p.get("remote_id"), p.get("worker_id")
    if src and old_rid:
        try:
            wapi(src, "DELETE", f"/sandboxes/{old_rid}")
        except Down:
            with _lock, conn() as c:
                c.execute("INSERT OR REPLACE INTO stale VALUES (?,?,?)", (old_wid, old_rid, p["id"]))
    elif old_rid and old_wid:
        with _lock, conn() as c:
            c.execute("INSERT OR REPLACE INTO stale VALUES (?,?,?)", (old_wid, old_rid, p["id"]))
    pset(p["id"], worker_id=dst["id"], remote_id=s["id"], port=s.get("port"), state=st["state"],
         moves=p.get("moves", 0) + 1, error=None)
    event("migrate", p["id"], f"{reason}: {old_wid} -> {dst['id']} remote {s['id']} (data copied: {copied})")
    return view(prow(p["id"]))


@app.post("/workers/{wid}/drain")
def drain(wid: str):
    w = wrow(wid)
    if w["state"] not in ("ALIVE", "SUSPECT"):
        raise HTTPException(409, f"worker is {w['state']}")
    wset(wid, state="DRAINING")
    moved = []
    for p in placements_all(wid):
        dst = pick_worker("least-loaded", {}, exclude={wid})
        moved.append(migrate(p, w, dst, "drain")["id"])
    wset(wid, state="ALIVE")
    event("worker", wid, f"drained {len(moved)} placement(s)")
    return {"worker": wid, "moved": moved}


@app.post("/sandboxes/{pid}/reschedule")
def reschedule(pid: str):
    p = prow(pid)
    w = wrow(p["worker_id"]) if p["worker_id"] else None
    if w and w["state"] == "ALIVE":
        raise HTTPException(409, "worker is ALIVE; use drain to move it")
    dst = pick_worker("least-loaded", {}, exclude={p["worker_id"]} if p["worker_id"] else None)
    return migrate(p, None, dst, "reschedule (worker " + (w["state"] if w else "?") + ")")


@app.delete("/workers/{wid}", status_code=204)
def delete_worker(wid: str):
    w = wrow(wid)
    for p in placements_all(wid):
        pset(p["id"], state="LOST", error="worker deleted")
    if w["outer_sid"]:
        try:
            outer("DELETE", f"/sandboxes/{w['outer_sid']}")
        except HTTPException:
            pass
    wset(wid, state="GONE")
    event("worker", wid, "deleted")
    return None


# -- loops --------------------------------------------------------------------------

def heartbeat_loop():
    while True:
        time.sleep(HB)
        for w in workers_all():
            if w["state"] == "GONE":
                continue
            try:
                h = wapi(w, "GET", "/healthz", timeout=3).json()
                ok = h.get("api") == "up" and h.get("dind") == "up"
            except (Down, ValueError):
                ok = False
            if ok:
                if w["state"] in ("STARTING", "SUSPECT", "DEAD"):
                    epoch = w["epoch"] + (1 if w["state"] == "DEAD" else 0)
                    wset(w["id"], state="ALIVE", misses=0, last_seen=time.time(), epoch=epoch)
                    event("worker", w["id"], f"ALIVE (epoch {epoch})")
                    if w["state"] == "DEAD":
                        fence(wrow(w["id"]))
                elif w["state"] == "ALIVE":
                    wset(w["id"], misses=0, last_seen=time.time())
            else:
                if w["state"] == "STARTING":
                    continue
                misses = w["misses"] + 1
                st = w["state"]
                if misses >= DEAD_MISSES and st != "DEAD":
                    st = "DEAD"
                    event("worker", w["id"], f"DEAD after {misses} missed heartbeats")
                    for p in placements_all(w["id"]):
                        if p["state"] not in ("DELETED", "LOST"):
                            pset(p["id"], state="UNKNOWN", error="worker dead")
                elif misses >= SUSPECT_MISSES and st == "ALIVE":
                    st = "SUSPECT"
                    event("worker", w["id"], f"SUSPECT after {misses} missed heartbeats")
                wset(w["id"], misses=misses, state=st)


def fence(w: dict):
    """A worker came back from DEAD: delete copies of placements that were
    re-placed elsewhere while it was gone (fencing: the old copy must not
    keep running), and re-adopt placements that were not moved."""
    try:
        remote = {s["id"]: s for s in wapi(w, "GET", "/sandboxes", timeout=10).json()}
    except Down:
        return
    with conn() as c:
        stale = [dict(r) for r in c.execute("SELECT * FROM stale WHERE worker_id=?", (w["id"],))]
    for s in stale:
        if s["remote_id"] in remote:
            try:
                wapi(w, "DELETE", f"/sandboxes/{s['remote_id']}")
                event("fence", s["placement_id"], f"purged stale copy {s['remote_id']} on {w['id']} (epoch {w['epoch']})")
            except Down:
                continue
        with _lock, conn() as c:
            c.execute("DELETE FROM stale WHERE worker_id=? AND remote_id=?", (w["id"], s["remote_id"]))
    for p in placements_all(w["id"]):
        if p["state"] == "UNKNOWN":
            if p["remote_id"] in remote:
                pset(p["id"], state=remote[p["remote_id"]]["state"], error=None)
                event("fence", p["id"], f"re-adopted on returning worker {w['id']}")
            else:
                pset(p["id"], state="LOST", error="not present on returning worker")


def monitor_loop():
    while True:
        time.sleep(3)
        for p in placements_all():
            if p["state"] in ("UNKNOWN", "LOST", "FAILED", "DELETED") or not p["remote_id"]:
                continue
            try:
                w = wrow(p["worker_id"])
            except HTTPException:
                continue
            if w["state"] != "ALIVE":
                continue
            try:
                r = wapi(w, "GET", f"/sandboxes/{p['remote_id']}", timeout=5)
                if r.status_code == 404:
                    pset(p["id"], state="LOST", error="gone on worker")
                elif r.ok:
                    pset(p["id"], state=r.json()["state"], port=r.json().get("port"))
            except Down:
                pass


@app.on_event("startup")
def startup():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    for fn in (heartbeat_loop, monitor_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


@app.get("/healthz")
def healthz():
    up = True
    try:
        outer("GET", "/healthz", timeout=5)
    except HTTPException:
        up = False
    return {"cluster": "up", "outer_sandbox_api": "up" if up else "down"}


@app.get("/status")
def status():
    ws = workers_all()
    return {"workers": {s: sum(1 for w in ws if w["state"] == s) for s in ("STARTING", "ALIVE", "SUSPECT", "DEAD", "DRAINING", "GONE")},
            "placements": {s: sum(1 for p in placements_all() if p["state"] == s)
                           for s in ("PLACING", "CREATING", "READY", "UNKNOWN", "LOST", "FAILED")},
            "policies": {"least-loaded": "fewest placements first", "binpack": "fullest worker with room first",
                         "affinity": "label match, then least-loaded"}}


@app.get("/events")
def events(limit: int = 100):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]
