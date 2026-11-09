"""SQLite: checkpoint manifests, lineage (parent), sandbox -> base checkpoint,
forks, events. The manifest on the node (manifest.json, written last) is
the commit record; this index makes it queryable."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id          TEXT PRIMARY KEY,
    sandbox_id  TEXT NOT NULL,
    parent      TEXT,
    template    TEXT NOT NULL,
    label       TEXT,
    created     REAL NOT NULL,
    lsn         TEXT,
    state       TEXT NOT NULL DEFAULT 'OK',     -- OK | CORRUPT
    manifest    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_sandbox ON checkpoints(sandbox_id);
CREATE TABLE IF NOT EXISTS bases (          -- which checkpoint a sandbox currently sits on
    sandbox_id TEXT PRIMARY KEY,
    checkpoint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forks (
    checkpoint TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    created    REAL NOT NULL,
    PRIMARY KEY (checkpoint, sandbox_id)
);
CREATE TABLE IF NOT EXISTS events (
    checkpoint TEXT NOT NULL,
    ts         REAL NOT NULL,
    message    TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init(self):
        with self._lock, self._conn() as c:
            c.executescript(SCHEMA)

    def _row(self, r) -> dict:
        m = json.loads(r["manifest"])
        m["state"] = r["state"]
        return m

    def insert(self, m: dict):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO checkpoints(id,sandbox_id,parent,template,label,created,lsn,state,manifest)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (m["id"], m["sandbox_id"], m.get("parent"), m["template"], m.get("label"),
                       m["created"], m.get("lsn"), "OK", json.dumps(m)))

    def get(self, cid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM checkpoints WHERE id=?", (cid,)).fetchone()
        return self._row(r) if r else None

    def list_all(self, sandbox: str | None) -> list[dict]:
        with self._conn() as c:
            if sandbox:
                rows = c.execute("SELECT * FROM checkpoints WHERE sandbox_id=? ORDER BY created", (sandbox,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM checkpoints ORDER BY created").fetchall()
        return [self._row(r) for r in rows]

    def set_state(self, cid: str, state: str):
        with self._lock, self._conn() as c:
            c.execute("UPDATE checkpoints SET state=? WHERE id=?", (state, cid))

    def delete(self, cid: str):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM checkpoints WHERE id=?", (cid,))
            c.execute("DELETE FROM forks WHERE checkpoint=?", (cid,))
            c.execute("DELETE FROM bases WHERE checkpoint=?", (cid,))

    def current_base(self, sid: str) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT checkpoint FROM bases WHERE sandbox_id=?", (sid,)).fetchone()
        return r["checkpoint"] if r else None

    def set_base(self, sid: str, cid: str):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO bases(sandbox_id,checkpoint) VALUES (?,?) "
                      "ON CONFLICT(sandbox_id) DO UPDATE SET checkpoint=excluded.checkpoint", (sid, cid))

    def add_fork(self, cid: str, sid: str, name: str):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO forks(checkpoint,sandbox_id,name,created) VALUES (?,?,?,?)",
                      (cid, sid, name, time.time()))

    def forks_of(self, cid: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT sandbox_id,name,created FROM forks WHERE checkpoint=?", (cid,))]

    def children(self, cid: str) -> list[str]:
        with self._conn() as c:
            return [r["id"] for r in c.execute("SELECT id FROM checkpoints WHERE parent=? ORDER BY created", (cid,))]

    def ancestors(self, cid: str) -> list[str]:
        out, cur = [], self.get(cid)
        while cur and cur.get("parent"):
            out.append(cur["parent"])
            cur = self.get(cur["parent"])
        return out

    def descendants(self, cid: str) -> list[str]:
        out, stack = [], [cid]
        while stack:
            x = stack.pop()
            for ch in self.children(x):
                out.append(ch)
                stack.append(ch)
        return out

    def dependents(self, cid: str) -> dict:
        """What stops a delete: child checkpoints, sandboxes based on it."""
        with self._conn() as c:
            based = [r["sandbox_id"] for r in c.execute("SELECT sandbox_id FROM bases WHERE checkpoint=?", (cid,))]
        ch = self.children(cid)
        deps = {}
        if ch:
            deps["children"] = ch
        if based:
            deps["sandboxes_on_it"] = based
        return deps

    def add_event(self, cid: str, message: str):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO events(checkpoint,ts,message) VALUES (?,?,?)", (cid, time.time(), message))

    def stats(self) -> dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) AS n, SUM(state='CORRUPT') AS corrupt FROM checkpoints").fetchone()
            f = c.execute("SELECT COUNT(*) AS n FROM forks").fetchone()
        return {"checkpoints": n["n"], "corrupt": n["corrupt"] or 0, "forks": f["n"]}
