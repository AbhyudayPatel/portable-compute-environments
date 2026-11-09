"""sandbox-egress - the control service for network policy, cassettes and
zero-secret sandboxes.

The enforcement lives in sandbox-api (per-sandbox proxy container +
iptables fence) and in the proxy addon (modes, allowlist, record/replay,
placeholder secrets). This service adds what spans sandboxes:

  * cassettes as portable artefacts: ship a sandbox's recording to the
    blob store, load any cassette into any sandbox for replay;
  * a catalogue of cassettes (hash, size, exchanges, label);
  * convenience passthroughs for policy and stats.

Endpoints
  GET    /sandboxes/{sid}/egress                policy + stats + fence (passthrough)
  PUT    /sandboxes/{sid}/egress                change the live policy (passthrough)
  POST   /sandboxes/{sid}/cassettes/ship        {label} -> cassette record (blob hash)
  POST   /sandboxes/{sid}/cassettes/{id}/load   put the cassette into the sandbox, switch to replay
  GET    /cassettes[/{id}]                      catalogue
  DELETE /cassettes/{id}                        drop the blob reference
  GET    /healthz
"""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-egress")

API = os.environ.get("SANDBOX_API_URL", "http://api:8000").rstrip("/")
BLOBS = os.environ.get("BLOBS_URL", "http://blobs:8000").rstrip("/")
DB = os.environ.get("DB_PATH", "/data/egress.db")

app = FastAPI(title="sandbox-egress", version="1.0.0")
_lock = threading.Lock()


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@app.on_event("startup")
def startup():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS cassettes (id TEXT PRIMARY KEY, sandbox_id TEXT, label TEXT, "
                  "hash TEXT NOT NULL, size INTEGER NOT NULL, exchanges INTEGER NOT NULL, created REAL NOT NULL)")


class Upstream(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def api(method, path, **kw):
    kw.setdefault("timeout", 60)
    try:
        r = requests.request(method, API + path, **kw)
    except requests.RequestException as e:
        raise Upstream(503, f"sandbox-api unreachable: {e}")
    if r.status_code >= 400:
        try:
            d = r.json().get("detail")
        except ValueError:
            d = r.text[:300]
        raise Upstream(r.status_code if r.status_code in (400, 404, 409, 422) else 502, f"sandbox-api {r.status_code}: {d}")
    return r


def _http(e: Upstream):
    return HTTPException(e.status, e.detail)


@app.get("/healthz")
def healthz():
    ok_api = ok_blobs = True
    try:
        api("GET", "/healthz", timeout=5)
    except Upstream:
        ok_api = False
    try:
        ok_blobs = requests.get(BLOBS + "/healthz", timeout=5).ok
    except requests.RequestException:
        ok_blobs = False
    return {"egress": "up", "sandbox_api": "up" if ok_api else "down", "blobs": "up" if ok_blobs else "down"}


@app.get("/sandboxes/{sid}/egress")
def egress_get(sid: str):
    try:
        return api("GET", f"/sandboxes/{sid}/egress").json()
    except Upstream as e:
        raise _http(e)


class PolicyBody(BaseModel):
    mode: str
    allow: list[str] = []
    secrets: dict[str, dict] = {}
    strict_replay: bool = True


@app.put("/sandboxes/{sid}/egress")
def egress_put(sid: str, body: PolicyBody):
    try:
        return api("PUT", f"/sandboxes/{sid}/egress", json=body.model_dump()).json()
    except Upstream as e:
        raise _http(e)


class ShipBody(BaseModel):
    label: str | None = None


@app.post("/sandboxes/{sid}/cassettes/ship", status_code=201)
def ship(sid: str, body: ShipBody):
    try:
        data = api("GET", f"/sandboxes/{sid}/egress/cassette").content
    except Upstream as e:
        raise _http(e)
    if not data.strip():
        raise HTTPException(409, "cassette is empty (record something first)")
    exchanges = sum(1 for l in data.splitlines() if l.strip())
    cid = uuid.uuid4().hex[:12]
    r = requests.put(f"{BLOBS}/blobs", params={"owner": f"cassette:{cid}"}, data=data, timeout=300)
    if not r.ok:
        raise HTTPException(502, f"blob store {r.status_code}: {r.text[:200]}")
    h = r.json()["hash"]
    rec = {"id": cid, "sandbox_id": sid, "label": body.label, "hash": h, "size": len(data),
           "exchanges": exchanges, "created": time.time()}
    with _lock, conn() as c:
        c.execute("INSERT INTO cassettes VALUES (?,?,?,?,?,?,?)",
                  (cid, sid, body.label, h, len(data), exchanges, rec["created"]))
    return rec


@app.get("/cassettes")
def list_cassettes():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cassettes ORDER BY created")]


@app.get("/cassettes/{cid}")
def get_cassette(cid: str):
    with conn() as c:
        r = c.execute("SELECT * FROM cassettes WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such cassette")
    return dict(r)


@app.delete("/cassettes/{cid}", status_code=204)
def delete_cassette(cid: str):
    with _lock, conn() as c:
        n = c.execute("DELETE FROM cassettes WHERE id=?", (cid,)).rowcount
    if not n:
        raise HTTPException(404, "no such cassette")
    try:
        requests.delete(f"{BLOBS}/refs/cassette:{cid}", timeout=30)
    except requests.RequestException:
        pass
    return None


@app.post("/sandboxes/{sid}/cassettes/{cid}/load")
def load(sid: str, cid: str):
    """Blob -> sandbox proxy, then switch the sandbox to replay mode."""
    rec = get_cassette(cid)
    r = requests.get(f"{BLOBS}/blobs/{rec['hash']}", timeout=300)
    if not r.ok:
        raise HTTPException(502, "cassette blob missing from the store")
    try:
        api("PUT", f"/sandboxes/{sid}/egress/cassette", data=r.content)
        pol = api("GET", f"/sandboxes/{sid}/egress").json()["policy"]
        pol["mode"] = "replay"
        api("PUT", f"/sandboxes/{sid}/egress", json={k: pol[k] for k in ("mode", "allow", "secrets", "strict_replay")})
    except Upstream as e:
        raise _http(e)
    return {"sandbox": sid, "cassette": cid, "hash": rec["hash"], "exchanges": rec["exchanges"], "mode": "replay"}
