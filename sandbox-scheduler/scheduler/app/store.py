"""SQLite store for jobs + events + idempotency keys (v2).

Same discipline as sandbox-api's store: class-based, one write lock,
short-lived connections (or a keeper for shared-cache in-memory DBs in the
simulator), WAL journal, time through the injectable clock.

v2 columns: deadline_at (EDF), preemptible, stateful, checkpoint_id,
preempt_count, mem_bytes, deadline_missed.
"""
import json
import sqlite3
import threading

from . import clock, config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    idem_key      TEXT UNIQUE,
    name          TEXT NOT NULL,
    tenant        TEXT NOT NULL,
    template      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    cmd           TEXT,
    priority      INTEGER NOT NULL,
    count         INTEGER NOT NULL,
    cpu_units     INTEGER NOT NULL,
    mem_units     INTEGER NOT NULL,
    state         TEXT NOT NULL,
    sandbox_ids   TEXT NOT NULL DEFAULT '[]',
    result        TEXT,
    max_runtime   INTEGER,
    queued_mono   REAL NOT NULL,
    deadline_mono REAL,
    deadline_wall REAL,
    created_at    REAL NOT NULL,
    admitted_at   REAL,
    finished_at   REAL,
    error         TEXT,
    deadline_at   REAL,
    preemptible   INTEGER NOT NULL DEFAULT 0,
    stateful      INTEGER NOT NULL DEFAULT 0,
    checkpoint_id TEXT,
    preempt_count INTEGER NOT NULL DEFAULT 0,
    mem_bytes     INTEGER,
    deadline_missed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS events (
    job_id   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    ts       REAL NOT NULL,
    type     TEXT NOT NULL,
    message  TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""
MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN deadline_at REAL",
    "ALTER TABLE jobs ADD COLUMN preemptible INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN stateful INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN checkpoint_id TEXT",
    "ALTER TABLE jobs ADD COLUMN preempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN mem_bytes INTEGER",
    "ALTER TABLE jobs ADD COLUMN deadline_missed INTEGER NOT NULL DEFAULT 0",
]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._uri = db_path.startswith("file:")
        self._keeper = self._open() if self._uri else None

    def _open(self):
        c = sqlite3.connect(self.db_path, timeout=10, uri=self._uri, check_same_thread=False)
        c.row_factory = sqlite3.Row
        if not self._uri:
            c.execute("PRAGMA journal_mode=WAL")
        return c

    def _conn(self):
        return self._open()

    def init(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)
            for m in MIGRATIONS:
                try:
                    conn.execute(m)
                except sqlite3.OperationalError:
                    pass

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["cmd"] = json.loads(d["cmd"]) if d["cmd"] else None
        d["sandbox_ids"] = json.loads(d["sandbox_ids"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        d["preemptible"] = bool(d.get("preemptible"))
        d["stateful"] = bool(d.get("stateful"))
        return d

    def create_job(self, j: dict) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id,idem_key,name,tenant,template,kind,cmd,priority,count,cpu_units,"
                "mem_units,state,sandbox_ids,result,max_runtime,queued_mono,deadline_mono,deadline_wall,"
                "created_at,admitted_at,finished_at,error,deadline_at,preemptible,stateful,checkpoint_id,"
                "preempt_count,mem_bytes,deadline_missed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (j["id"], j.get("idem_key"), j["name"], j["tenant"], j["template"], j["kind"],
                 json.dumps(j.get("cmd")) if j.get("cmd") else None, j["priority"], j["count"],
                 j["cpu_units"], j["mem_units"], j["state"], "[]", None, j.get("max_runtime"),
                 j["queued_mono"], None, None, clock.now_wall(), None, None, None,
                 j.get("deadline_at"), int(bool(j.get("preemptible"))), int(bool(j.get("stateful"))),
                 None, 0, None, 0))

    def get(self, jid: str) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return self._row(r) if r else None

    def get_by_idem(self, key: str) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE idem_key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def list_jobs(self, limit_terminal: int = 50) -> list[dict]:
        with self._conn() as conn:
            live = conn.execute(
                "SELECT * FROM jobs WHERE state NOT IN ('SUCCEEDED','FAILED','EXPIRED','CANCELLED') "
                "ORDER BY created_at, id").fetchall()
            done = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('SUCCEEDED','FAILED','EXPIRED','CANCELLED') "
                "ORDER BY finished_at DESC, id LIMIT ?", (limit_terminal,)).fetchall()
        return [self._row(r) for r in live] + [self._row(r) for r in done]

    def by_state(self, *states: str) -> list[dict]:
        q = ",".join("?" for _ in states)
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM jobs WHERE state IN ({q}) ORDER BY created_at, id", states).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> list[dict]:
        return self.by_state(*config.ACTIVE)

    def queue_depth(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('QUEUED','PREEMPTED')").fetchone()["n"]

    def set_state(self, jid: str, state: str, error: str | None = None) -> None:
        with self._lock, self._conn() as conn:
            if state == "ADMITTED":
                conn.execute("UPDATE jobs SET state=?, error=?, admitted_at=COALESCE(admitted_at, ?) WHERE id=?",
                             (state, error, clock.now_wall(), jid))
            elif state in config.TERMINAL:
                conn.execute("UPDATE jobs SET state=?, error=?, finished_at=? WHERE id=?",
                             (state, error, clock.now_wall(), jid))
            else:
                conn.execute("UPDATE jobs SET state=?, error=? WHERE id=?", (state, error, jid))

    def set_sandboxes(self, jid: str, ids: list[str]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET sandbox_ids=? WHERE id=?", (json.dumps(ids), jid))

    def set_result(self, jid: str, result: dict) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(result), jid))

    def set_deadline(self, jid: str, deadline_mono, deadline_wall) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET deadline_mono=?, deadline_wall=? WHERE id=?",
                         (deadline_mono, deadline_wall, jid))

    def reset_queued_mono(self, jid: str, mono: float) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET queued_mono=? WHERE id=?", (mono, jid))

    def set_checkpoint(self, jid: str, cid: str | None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET checkpoint_id=? WHERE id=?", (cid, jid))

    def set_preempted(self, jid: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET state='PREEMPTED', queued_mono=?, preempt_count=preempt_count+1, "
                         "mem_bytes=NULL WHERE id=?", (clock.now_mono(), jid))

    def set_mem(self, jid: str, mem: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET mem_bytes=? WHERE id=?", (mem, jid))

    def set_deadline_missed(self, jid: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET deadline_missed=1 WHERE id=?", (jid,))

    def add_event(self, jid: str, type_: str, message: str) -> int:
        with self._lock, self._conn() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE job_id=?",
                               (jid,)).fetchone()["n"]
            conn.execute("INSERT INTO events (job_id,seq,ts,type,message) VALUES (?,?,?,?,?)",
                         (jid, seq, clock.now_wall(), type_, message))
            return seq

    def events(self, jid: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT seq,ts,type,message FROM events WHERE job_id=? ORDER BY seq",
                                (jid,)).fetchall()
        return [dict(r) for r in rows]

    def wait_times(self) -> list[float]:
        with self._conn() as conn:
            rows = conn.execute("SELECT admitted_at - created_at AS w FROM jobs WHERE admitted_at IS NOT NULL").fetchall()
        return [r["w"] for r in rows]

    def finished_since(self, since_wall: float) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE finished_at IS NOT NULL AND finished_at > ?",
                                (since_wall,)).fetchone()["n"]

    def counters(self) -> dict:
        with self._conn() as conn:
            r = conn.execute("SELECT COALESCE(SUM(preempt_count),0) AS preempts, "
                             "COALESCE(SUM(deadline_missed),0) AS missed, "
                             "COALESCE(SUM(CASE WHEN deadline_at IS NOT NULL THEN 1 ELSE 0 END),0) AS with_deadline "
                             "FROM jobs").fetchone()
        return dict(r)

    def admin_reset(self) -> dict:
        with self._lock, self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM events")
        return {"jobs_cleared": n}
