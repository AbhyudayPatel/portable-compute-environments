# sandbox-api - Implementation Notes

The files, what each does, and the bugs found while building (the most
useful part for learning).

## Files

```
sandbox-api/
+-- docker-compose.yml        dind + api services, identity port range
+-- .env.example              API_PORT, POOL_START/END, MAX_SANDBOXES, REAPER_INTERVAL
+-- api/
|   +-- Dockerfile            python:3.12-slim + fastapi/uvicorn/docker SDK
|   +-- requirements.txt
|   +-- app/
|       +-- config.py         every tunable, env-driven, documented
|       +-- store.py          SQLite: sandboxes + events, port allocator
|       +-- engine.py         all dockerd interaction + reconcile() + exec
|       +-- templates.py      web / blank / coreapp specs + buildable images
|       +-- main.py           FastAPI routes, per-sandbox locks, TTL reaper
+-- scripts/
|   +-- start.ps1 / stop.ps1 / reset.ps1
|   +-- demo.ps1              interactive guided tour
|   +-- verify.sh             22-assertion edge-case battery
+-- docs/  ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

### store.py - the metadata store

- `sandboxes` table: id, name (UNIQUE), template, spec (JSON, the
  idempotency key), state, port, ttl, timestamps, error.
- `events` table: `(sandbox_id, seq)` primary key - `seq` is computed as
  `MAX(seq)+1` under the write lock, so ordering is exact per sandbox.
- `alloc_port()`: lowest-free in `[POOL_START, POOL_END]`, atomic under the
  same lock as all writes; `None` when full -> caller turns that into 429.
- Tombstoning: when a row becomes `DELETED` its `name` is rewritten to
  `name::deleted::<id>`, freeing the human name for reuse while keeping the
  row + events as an audit trail. (`free_name()` covers rows deleted by an
  older version.)

### engine.py - the only file that talks to dockerd

- Every SDK call wrapped; connection/daemon errors -> `EngineDown` -> API
  maps to `503 + Retry-After`. Client timeout is 8 s so nothing hangs.
- `create_sandbox_resources()`: network -> containers (with `wait_for`
  dependency ordering and network aliases) -> rollback closure that
  force-removes anything it created on failure.
- `reconcile()`: the crash-recovery pass (see ARCHITECTURE.md table).
- `exec_in_sandbox()`: `exec_create` + `exec_start(socket=True)` then reads
  the **raw multiplexed stream**, parsing 8-byte demux frames manually,
  enforcing deadline (timed_out) and 64 KiB/stream caps (truncated).
- `ensure_image()`: `images.get` -> if missing and buildable, tar a build
  context in memory and `images.build(fileobj=...)` **inside dind**; else
  `images.pull`. Cached afterwards by the inner engine itself.

### templates.py - what a sandbox IS

- `web`: busybox httpd; page + `/health` are base64-encoded by Python and
  decoded by the container's entrypoint (see bug #3 below).
- `blank`: alpine `sleep infinity` - a pure exec target, no port.
- `coreapp`: Postgres + FastAPI + nginx task board. Backend/frontend images
  are string-constant build contexts uploaded to dind; `postgres` is pulled.
  The frontend is a real working app (add/toggle/delete tasks).

### main.py - the API contract

- Per-sandbox locks (`_lock_for`) serialize manual ops vs the reaper.
- Provision runs in a daemon thread per create so image builds never block
  the event loop; state machine visible via `GET /sandboxes/{id}`.
- TTL reaper thread sweeps every `REAPER_INTERVAL`s using monotonic
  deadlines captured at admission.
- `/events` deliberately readable after deletion (audit trail); 404 only
  for sandboxes that never existed.

## Bugs found while building (all fixed, all in the verify battery)

1. **`SocketIO` poisons itself after a read timeout.**
   `exec_start(socket=True)` returns a `socket.SocketIO` wrapper; after one
   timeout its internal `_timeout_occurred` flag makes *every subsequent
   read* raise `OSError` - so the first `exec` with a quiet command broke
   the stream. Fix: read from the **raw socket** (`sock._sock.recv`), which
   recovers from timeouts normally. (verify 5)

2. **UNIQUE constraint on `sandboxes.name` vs deleted rows.**
   Soft-deleted rows kept their names, so recreating a deleted name (or
   re-running verify) crashed with `sqlite3.IntegrityError` -> 500. Fix:
   tombstone the name on delete (`name::deleted::<id>`). (verify 9 + rerun)

3. **Shell-quoting hell in the web entrypoint.**
   Generating HTML+JSON via nested `printf` inside `sh -c` inside a Python
   string produced `create failed: '\"status\"'` - escaping was wrong at
   one of three layers. Fix: Python base64-encodes both files; the
   container runs `echo <b64> | base64 -d > file`. Zero quoting, any content.

4. **Backend couldn't resolve `db`.**
   The coreapp db container joined the sandbox network under its container
   name only; `DATABASE_URL` points at host `db`. Fix: `network_aliases:
   ["db"]` (disconnect+reconnect with alias at create). Same for `backend`.

5. **Image tags are the cache key.**
   After improving the coreapp images, the inner engine kept serving the
   stale `:1.0` build. Fix: bump to `:1.1` - `ensure_image()`'s
   get-then-build is tag-exact, so a tag bump forces one rebuild, then it's
   cached again.

## Character encoding decision

All files in this project are **pure ASCII**. Early versions used UTF-8
em-dashes/arrows/box-drawing, which Windows tools (and browsers served
pages without a charset header) mangle into mojibake like `"a€"`.
Decision: rather than fight per-tool charset negotiation, every file -
code, comments, docs, UI strings, served HTML - is ASCII-only. Box diagrams
use `+ - |`, arrows are `->`, dashes are `-`. Enforced by inspection; if you
add content, keep it ASCII.

## Verified end to end

- [x] `bash scripts/verify.sh` - **22/22** (healthz, create->READY->page,
      idempotency, 409 conflict, exec exit/stdout/stderr/timeout/truncation,
      pool-exhaustion 429 + no port leak, TTL reap, delete idempotency,
      validation 422s, event-log order, restart reconciliation)
- [x] coreapp full CRUD round-trip through `localhost:9200`
      (add -> toggle -> list -> delete) - **VERIFIED**
- [x] web sandbox `/health` probe + introspection page - **VERIFIED**
- [x] dind pause -> 503s + `dind:down`; unpause -> recovery - **VERIFIED**
