"""SQLite metadata store.

The engine (container labels) is the source of truth for *what exists*;
this store adds what the engine cannot express: desired state, idempotent-
create specs, TTL deadlines, and the per-sandbox event log.

Threading model: FastAPI runs sync endpoints in a threadpool, so every
public function takes the module-level write lock and opens a short-lived
connection. SQLite serializes writers; we never hold a connection open.
"""
import json
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    id            TEXT PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    template      TEXT NOT NULL,
    spec          TEXT NOT NULL,          -- JSON of the create request (idempotency)
    state         TEXT NOT NULL,          -- CREATING READY FAILED STOPPED DELETED
    port          INTEGER,                -- allocated pool port, NULL if template has none
    ttl_seconds   INTEGER,                -- NULL = live forever
    created_at    REAL NOT NULL,          -- wall clock, for display
    deadline_mono REAL,                   -- monotonic reap deadline, NULL = none
    error         TEXT                    -- last failure reason, if any
);
CREATE TABLE IF NOT EXISTS events (
    sandbox_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,          -- monotonic per sandbox
    ts         REAL NOT NULL,
    type       TEXT NOT NULL,             -- state | action | reconcile | error
    message    TEXT NOT NULL,
    PRIMARY KEY (sandbox_id, seq)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["spec"] = json.loads(d["spec"])
    return d


def create(sandbox: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO sandboxes (id,name,template,spec,state,port,ttl_seconds,"
            "created_at,deadline_mono,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sandbox["id"], sandbox["name"], sandbox["template"],
                json.dumps(sandbox["spec"]), sandbox["state"], sandbox["port"],
                sandbox["ttl_seconds"], time.time(), sandbox.get("deadline_mono"),
                sandbox.get("error"),
            ),
        )


def get(sid: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE id=?", (sid,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_name(name: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    return _row_to_dict(row) if row else None


def list_all(include_deleted: bool = False) -> list[dict]:
    q = "SELECT * FROM sandboxes"
    if not include_deleted:
        q += " WHERE state != 'DELETED'"
    q += " ORDER BY created_at"
    with _conn() as conn:
        return [_row_to_dict(r) for r in conn.execute(q).fetchall()]


def set_state(sid: str, state: str, error: str | None = None) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE sandboxes SET state=?, error=? WHERE id=?",
                     (state, error, sid))
        if state == "DELETED":
            # Free the human name so it can be reused by a future sandbox.
            # The row (and its event log, keyed by id) is preserved as an
            # audit trail under a tombstoned name.
            conn.execute(
                "UPDATE sandboxes SET name = name || '::deleted::' || id "
                "WHERE id=? AND state='DELETED'", (sid,))


def free_name(name: str) -> None:
    """Retire any DELETED rows holding `name` so a create can reuse it."""
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE sandboxes SET name = name || '::deleted::' || id "
            "WHERE name=? AND state='DELETED'", (name,))


def set_port(sid: str, port: int | None) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE sandboxes SET port=? WHERE id=?", (port, sid))


def used_ports() -> set[int]:
    """Ports currently owned by non-deleted sandboxes (DB view)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT port FROM sandboxes WHERE port IS NOT NULL AND state != 'DELETED'"
        ).fetchall()
    return {r["port"] for r in rows}


def alloc_port(sid: str) -> int | None:
    """Lowest-free allocation, atomic under the write lock.

    Returns None when the pool is exhausted (caller maps this to 429 and
    must roll back whatever it already created).
    """
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT port FROM sandboxes WHERE port IS NOT NULL AND state != 'DELETED'"
        ).fetchall()
        used = {r["port"] for r in rows}
        for p in range(config.POOL_START, config.POOL_END + 1):
            if p not in used:
                conn.execute("UPDATE sandboxes SET port=? WHERE id=?", (p, sid))
                return p
    return None


def add_event(sid: str, type_: str, message: str) -> int:
    """Append an event with the next per-sandbox sequence number."""
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE sandbox_id=?", (sid,)
        ).fetchone()
        seq = row["n"]
        conn.execute(
            "INSERT INTO events (sandbox_id,seq,ts,type,message) VALUES (?,?,?,?,?)",
            (sid, seq, time.time(), type_, message),
        )
        return seq


def events(sid: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT seq,ts,type,message FROM events WHERE sandbox_id=? ORDER BY seq", (sid,)
        ).fetchall()
    return [dict(r) for r in rows]


def expired(now_mono: float) -> list[dict]:
    """Sandboxes whose monotonic TTL deadline has passed (non-terminal)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sandboxes WHERE deadline_mono IS NOT NULL "
            "AND deadline_mono <= ? AND state IN ('READY','STOPPED','FAILED')",
            (now_mono,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
