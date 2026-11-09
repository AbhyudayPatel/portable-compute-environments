"""SQLite index for the blob store: blobs, references, pins.

The FILES are the truth about which bytes exist (a blob is present iff
its path exists - uploads rename atomically). The index adds what files
cannot express: who references what, pins, sizes and timestamps for GC.
`reconcile()` at boot drops index rows whose file vanished and adopts
files the index does not know (e.g. copied in by hand).
"""
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    hash       TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    created    REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    owner  TEXT NOT NULL,
    hash   TEXT NOT NULL,
    PRIMARY KEY (owner, hash)
);
CREATE INDEX IF NOT EXISTS idx_refs_hash ON refs(hash);
CREATE TABLE IF NOT EXISTS pins (
    name   TEXT PRIMARY KEY,
    hash   TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def init(self):
        with self._lock, self._conn() as c:
            c.executescript(SCHEMA)

    def touch(self, h: str, size: int):
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO blobs(hash,size,created,last_seen) VALUES (?,?,?,?) "
                      "ON CONFLICT(hash) DO UPDATE SET last_seen=excluded.last_seen",
                      (h, size, now, now))

    def get(self, h: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM blobs WHERE hash=?", (h,)).fetchone()
        return dict(r) if r else None

    def forget(self, h: str):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM blobs WHERE hash=?", (h,))
            c.execute("DELETE FROM refs WHERE hash=?", (h,))
            c.execute("DELETE FROM pins WHERE hash=?", (h,))

    def add_refs(self, owner: str, hashes: list[str]):
        with self._lock, self._conn() as c:
            c.executemany("INSERT OR IGNORE INTO refs(owner,hash) VALUES (?,?)",
                          [(owner, h) for h in hashes])

    def drop_owner(self, owner: str) -> int:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM refs WHERE owner=?", (owner,)).rowcount

    def refs_of(self, h: str) -> list[str]:
        with self._conn() as c:
            return [r["owner"] for r in c.execute("SELECT owner FROM refs WHERE hash=? ORDER BY owner", (h,))]

    def hashes_of(self, owner: str) -> list[str]:
        with self._conn() as c:
            return [r["hash"] for r in c.execute("SELECT hash FROM refs WHERE owner=? ORDER BY hash", (owner,))]

    def all_owners(self) -> list[str]:
        with self._conn() as c:
            return [r["owner"] for r in c.execute("SELECT DISTINCT owner FROM refs ORDER BY owner")]

    def pins_of(self, h: str) -> list[str]:
        with self._conn() as c:
            return [r["name"] for r in c.execute("SELECT name FROM pins WHERE hash=? ORDER BY name", (h,))]

    def set_pin(self, name: str, h: str):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO pins(name,hash) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET hash=excluded.hash",
                      (name, h))

    def del_pin(self, name: str) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM pins WHERE name=?", (name,)).rowcount > 0

    def all_pins(self) -> dict:
        with self._conn() as c:
            return {r["name"]: r["hash"] for r in c.execute("SELECT name,hash FROM pins ORDER BY name")}

    def unreferenced(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT b.* FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.hash=b.hash) "
                "AND NOT EXISTS (SELECT 1 FROM pins p WHERE p.hash=b.hash)").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._conn() as c:
            b = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM blobs").fetchone()
            r = c.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT owner) AS owners FROM refs").fetchone()
            p = c.execute("SELECT COUNT(*) AS n FROM pins").fetchone()
            u = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM blobs b "
                "WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.hash=b.hash) "
                "AND NOT EXISTS (SELECT 1 FROM pins p WHERE p.hash=b.hash)").fetchone()
        return {"blobs": b["n"], "bytes": b["bytes"], "refs": r["n"], "owners": r["owners"],
                "pins": p["n"], "unreferenced": {"blobs": u["n"], "bytes": u["bytes"]}}

    def reconcile(self, exists) -> dict:
        """Drop rows whose file is gone (crash between unlink and forget)."""
        dropped = 0
        with self._lock, self._conn() as c:
            for r in c.execute("SELECT hash FROM blobs").fetchall():
                if not exists(r["hash"]):
                    c.execute("DELETE FROM blobs WHERE hash=?", (r["hash"],))
                    c.execute("DELETE FROM refs WHERE hash=?", (r["hash"],))
                    c.execute("DELETE FROM pins WHERE hash=?", (r["hash"],))
                    dropped += 1
        return {"dropped_missing": dropped}
