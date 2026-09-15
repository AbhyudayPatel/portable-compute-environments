"""SQLite store for jobs + events + idempotency keys.

Same discipline as T01's store: one module-level write lock, short-lived
connections, WAL journal. The jobs table is the scheduler's memory; the
engine (via the upstream sandbox-api) is the truth about what RUNS.
"""
import json
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    idem_key      TEXT UNIQUE,              -- NULL allowed; client retry key
    name          TEXT NOT NULL,
    tenant        TEXT NOT NULL,
    template      TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- service | task
    cmd           TEXT,                     -- JSON array, task jobs only
    priority      INTEGER NOT NULL,         -- base priority 0..9
    count         INTEGER NOT NULL,         -- gang size (sandboxes per job)
    cpu_units     INTEGER NOT NULL,
    mem_units     INTEGER NOT NULL,
    state         TEXT NOT NULL,
    sandbox_ids   TEXT NOT NULL DEFAULT '[]',
    result        TEXT,                     -- JSON, task jobs
    max_runtime   INTEGER,                  -- seconds; NULL = no limit
    queued_mono   REAL NOT NULL,            -- monotonic when queued (aging)
    deadline_mono REAL,                     -- monotonic reap deadline
    deadline_wall REAL,                     -- wall deadline (survives restart)
    created_at    REAL NOT NULL,
    admitted_at   REAL,
    finished_at   REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS events (
    job_id   TEXT NOT NULL,
    seq      INTEGER NOT NULL,              -- monotonic per job
    ts       REAL NOT NULL,
    type     TEXT NOT NULL,
    message  TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["cmd"] = json.loads(d["cmd"]) if d["cmd"] else None
    d["sandbox_ids"] = json.loads(d["sandbox_ids"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def init() -> None:
    with _lock, _conn() as conn:
        conn.executescript(SCHEMA)


def create_job(j: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id,idem_key,name,tenant,template,kind,cmd,"
            "priority,count,cpu_units,mem_units,state,sandbox_ids,result,"
            "max_runtime,queued_mono,deadline_mono,deadline_wall,created_at,"
            "admitted_at,finished_at,error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (j["id"], j.get("idem_key"), j["name"], j["tenant"], j["template"],
             j["kind"], json.dumps(j.get("cmd")) if j.get("cmd") else None,
             j["priority"], j["count"], j["cpu_units"], j["mem_units"],
             j["state"], "[]", None, j.get("max_runtime"), j["queued_mono"],
             None, None, time.time(), None, None, None))


def get(jid: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _row(r) if r else None


def get_by_idem(key: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE idem_key=?", (key,)).fetchone()
    return _row(r) if r else None


def list_jobs(limit_terminal: int = 50) -> list[dict]:
    """All live jobs + the most recent terminal ones (for the console)."""
    with _conn() as conn:
        live = conn.execute(
            "SELECT * FROM jobs WHERE state NOT IN ('SUCCEEDED','FAILED',"
            "'EXPIRED','CANCELLED') ORDER BY created_at").fetchall()
        done = conn.execute(
            "SELECT * FROM jobs WHERE state IN ('SUCCEEDED','FAILED',"
            "'EXPIRED','CANCELLED') ORDER BY finished_at DESC LIMIT ?",
            (limit_terminal,)).fetchall()
    return [_row(r) for r in live] + [_row(r) for r in done]


def queued() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state='QUEUED' ORDER BY created_at"
        ).fetchall()
    return [_row(r) for r in rows]


def active() -> list[dict]:
    """Jobs holding cluster reservations: ADMITTED / RUNNING / CANCELLING."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state IN ('ADMITTED','RUNNING','CANCELLING')"
        ).fetchall()
    return [_row(r) for r in rows]


def by_state(*states: str) -> list[dict]:
    q = ",".join("?" for _ in states)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({q})", states).fetchall()
    return [_row(r) for r in rows]


def queue_depth() -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state='QUEUED'"
        ).fetchone()["n"]


def set_state(jid: str, state: str, error: str | None = None) -> None:
    with _lock, _conn() as conn:
        if state == "ADMITTED":
            conn.execute("UPDATE jobs SET state=?, error=?, admitted_at=? "
                         "WHERE id=?", (state, error, time.time(), jid))
        elif state in config.TERMINAL:
            conn.execute("UPDATE jobs SET state=?, error=?, finished_at=? "
                         "WHERE id=?", (state, error, time.time(), jid))
        else:
            conn.execute("UPDATE jobs SET state=?, error=? WHERE id=?",
                         (state, error, jid))


def set_sandboxes(jid: str, ids: list[str]) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE jobs SET sandbox_ids=? WHERE id=?",
                     (json.dumps(ids), jid))


def set_result(jid: str, result: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE jobs SET result=? WHERE id=?",
                     (json.dumps(result), jid))


def set_deadline(jid: str, deadline_mono: float | None,
                 deadline_wall: float | None) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE jobs SET deadline_mono=?, deadline_wall=? "
                     "WHERE id=?", (deadline_mono, deadline_wall, jid))


def reset_queued_mono(jid: str, mono: float) -> None:
    """After a restart, re-anchor aging to the new process's monotonic clock
    while preserving the job's wall-clock age (edge case #8)."""
    with _lock, _conn() as conn:
        conn.execute("UPDATE jobs SET queued_mono=? WHERE id=?", (mono, jid))


def add_event(jid: str, type_: str, message: str) -> int:
    with _lock, _conn() as conn:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE job_id=?",
            (jid,)).fetchone()["n"]
        conn.execute("INSERT INTO events (job_id,seq,ts,type,message) "
                     "VALUES (?,?,?,?,?)", (jid, seq, time.time(), type_, message))
        return seq


def events(jid: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT seq,ts,type,message FROM events WHERE job_id=? ORDER BY seq",
            (jid,)).fetchall()
    return [dict(r) for r in rows]


def wait_times() -> list[float]:
    """admitted_at - created_at for everything ever admitted (metrics)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT admitted_at - created_at AS w FROM jobs "
            "WHERE admitted_at IS NOT NULL").fetchall()
    return [r["w"] for r in rows]


def finished_since(since_wall: float) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE finished_at IS NOT NULL "
            "AND finished_at > ?", (since_wall,)).fetchone()["n"]


def admin_reset() -> dict:
    """Wipe all job rows. Live sandboxes are left to the janitor's orphan
    sweep (they are name-correlated), so nothing leaks. Demo/test only."""
    with _lock, _conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM events")
    return {"jobs_cleared": n}
