"""SQLite metadata store.

The engine (container labels) is the source of truth for *what exists*;
this store adds what the engine cannot express: desired state, idempotent-
create specs, TTL deadlines, agent tokens, and the per-sandbox event log.

v2 changes:
  * class-based (`Store(db_path)`) so the simulator can own an isolated
    in-memory database per run;
  * dual deadlines (monotonic + wall) so TTLs survive an API restart;
  * every event is also published on the in-process bus (live streams);
  * a global cursor (rowid) over the events table for the /events stream.

Threading model: FastAPI runs sync endpoints in a threadpool, so every
write takes the instance write lock and opens a short-lived connection.
SQLite serializes writers; we never hold a connection open - except the
"keeper" connection for shared-cache in-memory databases, which would
otherwise vanish the moment the last connection closes.
"""
import json
import sqlite3
import threading

from . import clock
from .bus import bus

SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    id            TEXT PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    template      TEXT NOT NULL,
    spec          TEXT NOT NULL,          -- JSON of the create request (idempotency)
    state         TEXT NOT NULL,          -- CREATING READY DEGRADED STOPPED FAILED DELETED
    port          INTEGER,                -- allocated pool port, NULL if template has none
    ttl_seconds   INTEGER,                -- NULL = live forever
    created_at    REAL NOT NULL,          -- wall clock
    deadline_mono REAL,                   -- monotonic reap deadline (this process)
    deadline_wall REAL,                   -- wall reap deadline (survives restarts)
    error         TEXT,                   -- last failure reason, if any
    isolation     TEXT NOT NULL DEFAULT 'container',
    agent_token   TEXT,                   -- bearer token of this sandbox's agents
    detail        TEXT                    -- JSON: free-form (degraded containers, ...)
);
CREATE TABLE IF NOT EXISTS events (
    sandbox_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,          -- monotonic per sandbox
    ts         REAL NOT NULL,
    type       TEXT NOT NULL,             -- state | action | reconcile | error | ...
    message    TEXT NOT NULL,
    PRIMARY KEY (sandbox_id, seq)
);
"""

# columns added in v2; applied idempotently to v1 databases
MIGRATIONS = [
    "ALTER TABLE sandboxes ADD COLUMN deadline_wall REAL",
    "ALTER TABLE sandboxes ADD COLUMN isolation TEXT NOT NULL DEFAULT 'container'",
    "ALTER TABLE sandboxes ADD COLUMN agent_token TEXT",
    "ALTER TABLE sandboxes ADD COLUMN detail TEXT",
]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._uri = db_path.startswith("file:")
        self._keeper = None
        if self._uri:
            self._keeper = self._open()

    # -- connections ---------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, uri=self._uri,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self._uri:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _conn(self) -> sqlite3.Connection:
        return self._open()

    def init(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)
            for m in MIGRATIONS:
                try:
                    conn.execute(m)
                except sqlite3.OperationalError:
                    pass  # column already there

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["spec"] = json.loads(d["spec"]) if d["spec"] else {}
        d["detail"] = json.loads(d["detail"]) if d.get("detail") else {}
        return d

    # -- sandboxes -----------------------------------------------------------

    def create(self, sandbox: dict) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO sandboxes (id,name,template,spec,state,port,ttl_seconds,"
                "created_at,deadline_mono,deadline_wall,error,isolation,agent_token,detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sandbox["id"], sandbox["name"], sandbox["template"],
                    json.dumps(sandbox.get("spec") or {}), sandbox["state"],
                    sandbox.get("port"), sandbox.get("ttl_seconds"),
                    clock.now_wall(), sandbox.get("deadline_mono"),
                    sandbox.get("deadline_wall"), sandbox.get("error"),
                    sandbox.get("isolation", "container"),
                    sandbox.get("agent_token"),
                    json.dumps(sandbox.get("detail") or {}),
                ),
            )

    def get(self, sid: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sandboxes WHERE id=?", (sid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_name(self, name: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_all(self, include_deleted: bool = False) -> list[dict]:
        q = "SELECT * FROM sandboxes"
        if not include_deleted:
            q += " WHERE state != 'DELETED'"
        q += " ORDER BY created_at, id"
        with self._conn() as conn:
            return [self._row_to_dict(r) for r in conn.execute(q).fetchall()]

    def set_state(self, sid: str, state: str, error: str | None = None,
                  detail: dict | None = None) -> None:
        with self._lock, self._conn() as conn:
            if detail is None:
                conn.execute("UPDATE sandboxes SET state=?, error=? WHERE id=?",
                             (state, error, sid))
            else:
                conn.execute("UPDATE sandboxes SET state=?, error=?, detail=? WHERE id=?",
                             (state, error, json.dumps(detail), sid))
            if state == "DELETED":
                # Free the human name so it can be reused by a future sandbox.
                # The row (and its event log, keyed by id) is preserved as an
                # audit trail under a tombstoned name.
                conn.execute(
                    "UPDATE sandboxes SET name = name || '::deleted::' || id "
                    "WHERE id=? AND state='DELETED' AND name NOT LIKE '%::deleted::%'",
                    (sid,))

    def set_state_if(self, sid: str, expected: tuple, state: str,
                     error: str | None = None, detail: dict | None = None) -> bool:
        """Compare-and-set: transition only if the current state is in
        `expected`. Returns True when the update happened."""
        with self._lock, self._conn() as conn:
            q = ",".join("?" for _ in expected)
            cur = conn.execute(
                f"UPDATE sandboxes SET state=?, error=?, detail=COALESCE(?, detail) "
                f"WHERE id=? AND state IN ({q})",
                (state, error, json.dumps(detail) if detail is not None else None,
                 sid, *expected))
            if cur.rowcount and state == "DELETED":
                conn.execute(
                    "UPDATE sandboxes SET name = name || '::deleted::' || id "
                    "WHERE id=? AND name NOT LIKE '%::deleted::%'", (sid,))
            return bool(cur.rowcount)

    def free_name(self, name: str) -> None:
        """Retire any DELETED rows holding `name` so a create can reuse it."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE sandboxes SET name = name || '::deleted::' || id "
                "WHERE name=? AND state='DELETED'", (name,))

    def set_port(self, sid: str, port: int | None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sandboxes SET port=? WHERE id=?", (port, sid))

    def set_deadline(self, sid: str, deadline_mono: float | None,
                     deadline_wall: float | None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sandboxes SET deadline_mono=?, deadline_wall=? WHERE id=?",
                         (deadline_mono, deadline_wall, sid))

    def set_detail(self, sid: str, detail: dict) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE sandboxes SET detail=? WHERE id=?",
                         (json.dumps(detail), sid))

    def used_ports(self) -> set[int]:
        """Ports currently owned by non-deleted sandboxes (DB view)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT port FROM sandboxes WHERE port IS NOT NULL AND state != 'DELETED'"
            ).fetchall()
        return {r["port"] for r in rows}

    def alloc_port(self, sid: str, pool_start: int, pool_end: int) -> int | None:
        """Lowest-free allocation, atomic under the write lock.

        Returns None when the pool is exhausted (caller maps this to 429).
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT port FROM sandboxes WHERE port IS NOT NULL AND state != 'DELETED'"
            ).fetchall()
            used = {r["port"] for r in rows}
            for p in range(pool_start, pool_end + 1):
                if p not in used:
                    conn.execute("UPDATE sandboxes SET port=? WHERE id=?", (p, sid))
                    return p
        return None

    # -- events ---------------------------------------------------------------

    def add_event(self, sid: str, type_: str, message: str) -> int:
        """Append an event with the next per-sandbox sequence number, then
        publish it on the bus for live subscribers."""
        ts = clock.now_wall()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE sandbox_id=?", (sid,)
            ).fetchone()
            seq = row["n"]
            cur = conn.execute(
                "INSERT INTO events (sandbox_id,seq,ts,type,message) VALUES (?,?,?,?,?)",
                (sid, seq, ts, type_, message),
            )
            cursor = cur.lastrowid
        bus.publish({"cursor": cursor, "sandbox_id": sid, "seq": seq, "ts": ts,
                     "type": type_, "message": message})
        return seq

    def events(self, sid: str, since_seq: int = 0) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT rowid AS cursor, seq, ts, type, message FROM events "
                "WHERE sandbox_id=? AND seq > ? ORDER BY seq", (sid, since_seq)
            ).fetchall()
        return [dict(r) for r in rows]

    def events_after(self, cursor: int, limit: int = 500) -> list[dict]:
        """Global stream, ordered by insertion (rowid)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT rowid AS cursor, sandbox_id, seq, ts, type, message FROM events "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?", (cursor, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def last_cursor(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(rowid),0) AS c FROM events").fetchone()
        return row["c"]

    # -- TTL ----------------------------------------------------------------

    def expired(self, now_mono: float) -> list[dict]:
        """Sandboxes whose monotonic TTL deadline has passed (non-terminal)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE deadline_mono IS NOT NULL "
                "AND deadline_mono <= ? AND state IN ('READY','DEGRADED','STOPPED','FAILED')",
                (now_mono,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def with_deadlines(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE deadline_wall IS NOT NULL "
                "AND state != 'DELETED'").fetchall()
        return [self._row_to_dict(r) for r in rows]
