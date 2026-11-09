"""State fingerprints and semantic diffs.

A sandbox's state has two halves we can hash deterministically:

  filesystem  - the agent's merkle walk (see agent/fingerprint.go):
                root hash + per-entry hashes, mtimes ignored
  database    - for templates that declare a `db` role: every table in
                the public schema hashed over its rows in canonical order
                (md5 of the row texts sorted), so inserts/updates/deletes
                change the hash and autoincrement noise does not matter
                beyond the rows that actually differ

`diff()` turns two fingerprints into a human-readable change list.
"""
import hashlib
import json

from .agent import AgentClient

TABLE_LIST_SQL = ("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1")


def db_fingerprint(agent: AgentClient, user: str, database: str,
                   with_rows: bool = False, row_cap: int = 2000) -> dict:
    """Run psql inside the db container; returns {root, tables:{name:{hash,rows[,data]}}}."""
    def psql(sql: str) -> str:
        r = agent.exec(["psql", "-U", user, "-d", database, "-Atc", sql], timeout=60,
                       max_bytes=4 << 20)
        if r.get("exit_code") != 0:
            raise RuntimeError(f"psql failed: {r.get('stderr', '')[:300]}")
        return r.get("stdout", "")

    tables = [t for t in psql(TABLE_LIST_SQL).split("\n") if t]
    out = {}
    h = hashlib.sha256()
    for t in tables:
        line = psql(f"SELECT count(*), coalesce(md5(string_agg(t::text, E'\\n' "
                    f"ORDER BY t::text)), 'empty') FROM \"{t}\" t")
        count, digest = (line.strip().split("|") + ["", ""])[:2]
        info = {"rows": int(count or 0), "hash": digest}
        if with_rows:
            raw = psql(f"SELECT coalesce(json_agg(t ORDER BY t::text), '[]') "
                       f"FROM (SELECT * FROM \"{t}\" LIMIT {row_cap}) t")
            try:
                info["data"] = json.loads(raw or "[]")
            except json.JSONDecodeError:
                info["data"] = []
        out[t] = info
        h.update(f"{t}:{digest}:{count}\n".encode())
    return {"root": h.hexdigest(), "tables": out}


def diff_fs(a: dict, b: dict) -> dict:
    """Diff two agent fingerprints taken with detail=1."""
    ea = {e["path"]: e for e in a.get("entries", [])}
    eb = {e["path"]: e for e in b.get("entries", [])}
    added = sorted(p for p in eb if p not in ea)
    removed = sorted(p for p in ea if p not in eb)
    modified, mode_changed = [], []
    for p in sorted(set(ea) & set(eb)):
        x, y = ea[p], eb[p]
        if x["type"] == "d" and y["type"] == "d":
            continue                       # dir hashes change when children do
        if x["hash"] != y["hash"] or x["type"] != y["type"]:
            modified.append({"path": p, "size_a": x["size"], "size_b": y["size"]})
        elif x["mode"] != y["mode"]:
            mode_changed.append({"path": p, "mode_a": x["mode"], "mode_b": y["mode"]})
    return {
        "identical": a.get("root") == b.get("root"),
        "root_a": a.get("root"), "root_b": b.get("root"),
        "added": added, "removed": removed, "modified": modified,
        "mode_changed": mode_changed,
        "summary": f"+{len(added)} -{len(removed)} ~{len(modified)}",
    }


def _row_key(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def diff_db(a: dict, b: dict) -> dict:
    ta, tb = a.get("tables", {}), b.get("tables", {})
    out = {"identical": a.get("root") == b.get("root"), "tables": {}}
    for t in sorted(set(ta) | set(tb)):
        x, y = ta.get(t), tb.get(t)
        if x is None or y is None:
            out["tables"][t] = {"status": "added" if x is None else "removed"}
            continue
        if x["hash"] == y["hash"]:
            continue
        entry = {"status": "changed", "rows_a": x["rows"], "rows_b": y["rows"]}
        if "data" in x and "data" in y:
            sa = {_row_key(r): r for r in x["data"]}
            sb = {_row_key(r): r for r in y["data"]}
            entry["rows_added"] = [sb[k] for k in sb if k not in sa][:50]
            entry["rows_removed"] = [sa[k] for k in sa if k not in sb][:50]
        out["tables"][t] = entry
    return out
