"""sandbox-blobs - a content-addressed blob store (CAS) for the platform.

One place for every large byte-stream the platform produces or consumes:
image build contexts, checkpoint layers, network cassettes, big exec
outputs, screenshots, dataset exports. Everything is addressed by its
sha256, deduplicated by construction, streamed (never buffered in RAM),
reference-counted, and garbage-collected only when nothing refers to it.

Endpoints
  PUT    /blobs                  body = bytes (streamed) -> {hash, size, existed}
                                 ?owner=<ref owner> registers a reference atomically
  GET    /blobs/{hash}           stream the bytes (Range supported)
  HEAD   /blobs/{hash}           size in Content-Length
  GET    /blobs/{hash}/meta      {hash, size, created, last_seen, refs, pins}
  GET    /blobs/{hash}/verify    re-hash the file; {ok, expected, actual}
  DELETE /blobs/{hash}           only if unreferenced and unpinned (409 otherwise)
  POST   /refs                   {owner, hashes[]} -> add references
  DELETE /refs/{owner}           drop every reference of an owner
  GET    /refs?hash=|owner=      list
  PUT    /pins/{name}            {hash} -> a named pin (survives GC)
  DELETE /pins/{name}
  GET    /pins
  POST   /gc                     ?grace=60 -> delete unreferenced, unpinned blobs older than grace
  GET    /stats                  counts, bytes, disk
  GET    /healthz

Design notes are in docs/ARCHITECTURE.md; the GC safety argument matters:
a blob can only be collected if (a) it has no refs, (b) no pins, and (c)
it was last written more than `grace` seconds ago. Uploaders register
refs right after PUT (or in the same request with ?owner=), well inside
the grace window, and both ref insertion and GC's scan+unlink take the
same lock, so a reference can never be added to a blob GC is deleting.
"""
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox-blobs")

ROOT = Path(os.environ.get("BLOB_ROOT", "/blobs"))
MAX_BLOB_BYTES = int(os.environ.get("MAX_BLOB_BYTES", str(8 << 30)))     # 8 GiB
DEFAULT_GRACE = int(os.environ.get("GC_GRACE_SECONDS", "60"))
CHUNK = 1 << 20

app = FastAPI(title="sandbox-blobs", version="1.0.0")
db = store.Store(os.environ.get("DB_PATH", str(ROOT / "index.db")))
_gc_lock = threading.Lock()         # serializes GC scan+unlink against ref/pin writes


def blob_path(h: str) -> Path:
    return ROOT / "sha256" / h[:2] / h


def _valid(h: str) -> str:
    h = h.lower()
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise HTTPException(400, "hash must be 64 hex chars (sha256)")
    return h


@app.on_event("startup")
def startup():
    (ROOT / "sha256").mkdir(parents=True, exist_ok=True)
    (ROOT / "tmp").mkdir(parents=True, exist_ok=True)
    db.init()
    # crash recovery: temp files from interrupted uploads are garbage
    for p in (ROOT / "tmp").iterdir():
        p.unlink(missing_ok=True)
    stats = db.reconcile(lambda h: blob_path(h).exists())
    log.info("index reconciled: %s", stats)


@app.get("/healthz")
def healthz():
    return {"blobs": "up", "root": str(ROOT)}


# -- upload (streamed, hashed on the fly) -------------------------------------

@app.put("/blobs")
async def put_blob(request: Request, owner: str | None = None):
    h = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(dir=ROOT / "tmp", prefix="up-")
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BLOB_BYTES:
                    raise HTTPException(413, f"blob larger than {MAX_BLOB_BYTES} bytes")
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        digest = h.hexdigest()
        dest = blob_path(digest)
        with _gc_lock:
            existed = dest.exists()
            if not existed:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, dest)           # atomic: a blob is complete or absent
            else:
                os.unlink(tmp)
                os.utime(dest, None)            # refresh last_seen for the grace window
            db.touch(digest, size)
            if owner:
                db.add_refs(owner, [digest])
    except HTTPException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return JSONResponse(status_code=200 if existed else 201,
                        content={"hash": digest, "size": size, "existed": existed,
                                 "owner": owner})


# -- download -----------------------------------------------------------------------

def _get_meta(h: str) -> dict:
    h = _valid(h)
    m = db.get(h)
    if not m or not blob_path(h).exists():
        raise HTTPException(404, f"blob {h} not found")
    return m


@app.head("/blobs/{h}")
def head_blob(h: str):
    m = _get_meta(h)
    return Response(headers={"Content-Length": str(m["size"]), "ETag": f'"{m["hash"]}"'})


@app.get("/blobs/{h}")
def get_blob(h: str, request: Request):
    m = _get_meta(h)
    path = blob_path(m["hash"])
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        start_s, _, end_s = rng[6:].partition("-")
        start = int(start_s or 0)
        end = int(end_s) if end_s else m["size"] - 1
        end = min(end, m["size"] - 1)
        if start > end:
            raise HTTPException(416, "bad range")

        def gen():
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk
        return StreamingResponse(gen(), status_code=206, media_type="application/octet-stream",
                                 headers={"Content-Range": f"bytes {start}-{end}/{m['size']}",
                                          "Content-Length": str(end - start + 1),
                                          "ETag": f'"{m["hash"]}"'})
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"ETag": f'"{m["hash"]}"'})


@app.get("/blobs/{h}/meta")
def blob_meta(h: str):
    m = _get_meta(h)
    m["refs"] = db.refs_of(m["hash"])
    m["pins"] = db.pins_of(m["hash"])
    return m


@app.get("/blobs/{h}/verify")
def blob_verify(h: str):
    m = _get_meta(h)
    sha = hashlib.sha256()
    with open(blob_path(m["hash"]), "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    return {"hash": m["hash"], "ok": actual == m["hash"], "actual": actual, "size": m["size"]}


@app.delete("/blobs/{h}", status_code=204)
def delete_blob(h: str):
    m = _get_meta(h)
    with _gc_lock:
        if db.refs_of(m["hash"]) or db.pins_of(m["hash"]):
            raise HTTPException(409, "blob is referenced or pinned")
        blob_path(m["hash"]).unlink(missing_ok=True)
        db.forget(m["hash"])
    return Response(status_code=204)


# -- references and pins ---------------------------------------------------------------

class RefsBody(BaseModel):
    owner: str = Field(min_length=1, max_length=200)
    hashes: list[str] = Field(min_length=1)


@app.post("/refs")
def add_refs(body: RefsBody):
    hashes = [_valid(x) for x in body.hashes]
    with _gc_lock:
        missing = [x for x in hashes if not db.get(x) or not blob_path(x).exists()]
        if missing:
            raise HTTPException(404, f"unknown blobs: {missing}")
        db.add_refs(body.owner, hashes)
    return {"owner": body.owner, "hashes": hashes}


@app.delete("/refs/{owner}")
def drop_refs(owner: str):
    with _gc_lock:
        n = db.drop_owner(owner)
    return {"owner": owner, "dropped": n}


@app.get("/refs")
def list_refs(hash: str | None = None, owner: str | None = None):
    if hash:
        return {"hash": _valid(hash), "owners": db.refs_of(_valid(hash))}
    if owner:
        return {"owner": owner, "hashes": db.hashes_of(owner)}
    return {"owners": db.all_owners()}


class PinBody(BaseModel):
    hash: str


@app.put("/pins/{name}")
def put_pin(name: str, body: PinBody):
    h = _valid(body.hash)
    with _gc_lock:
        if not db.get(h) or not blob_path(h).exists():
            raise HTTPException(404, f"blob {h} not found")
        db.set_pin(name, h)
    return {"name": name, "hash": h}


@app.delete("/pins/{name}")
def delete_pin(name: str):
    with _gc_lock:
        if not db.del_pin(name):
            raise HTTPException(404, "no such pin")
    return {"name": name, "deleted": True}


@app.get("/pins")
def list_pins():
    return db.all_pins()


# -- garbage collection ---------------------------------------------------------------

@app.post("/gc")
def gc(grace: int = DEFAULT_GRACE, dry_run: int = 0):
    now = time.time()
    deleted, freed, kept_young, candidates = [], 0, 0, 0
    with _gc_lock:
        for m in db.unreferenced():
            candidates += 1
            p = blob_path(m["hash"])
            if not p.exists():
                db.forget(m["hash"])
                continue
            if now - p.stat().st_mtime < grace:
                kept_young += 1
                continue
            if not dry_run:
                p.unlink()
                db.forget(m["hash"])
            deleted.append(m["hash"])
            freed += m["size"]
    return {"deleted": len(deleted), "freed_bytes": freed, "kept_young": kept_young,
            "candidates": candidates, "dry_run": bool(dry_run), "grace": grace,
            "hashes": deleted[:50]}


@app.get("/stats")
def stats():
    s = db.stats()
    usage = shutil.disk_usage(ROOT)
    s["disk"] = {"total": usage.total, "free": usage.free, "used": usage.used}
    s["root"] = str(ROOT)
    return s
