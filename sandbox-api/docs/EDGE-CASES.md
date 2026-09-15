# sandbox-api - Edge Cases (each mapped to code + test)

Status legend: **VERIFIED** = executed and observed by `scripts/verify.sh`;
**EXPECTED** = reasoned, not executed.

| # | Edge case | Handling (code) | Test |
|---|---|---|---|
| 1 | Idempotent create | `POST /sandboxes` looks the name up first: same spec -> `200` + existing sandbox; different spec -> `409`. Uniqueness enforced by the `name UNIQUE` column. | verify 3, 4 - **VERIFIED** |
| 2 | Port-pool exhaustion | `store.alloc_port()` returns `None` when full -> row set `FAILED`, event logged, `429` + `Retry-After`. Nothing was created yet, so nothing to roll back; the FAILED row holds **no port**. | verify 6 - **VERIFIED** |
| 3 | Partial creation failure | `engine.create_sandbox_resources()` wraps the whole sequence; any exception runs `rollback()` (force-removes created containers, removes network) then re-raises. Provision thread marks `FAILED` with the error and frees the port. | exercised by design; a forced-failure run (bad image tag) observed event `create failed: ...` with zero leftover containers - **VERIFIED** |
| 4 | API crash / restart | `engine.reconcile()` at startup adopts engine orphans, finishes interrupted deletes, rolls back interrupted creates, and syncs disagreeing states. | verify 12 (create -> `docker compose restart api` -> sandbox still `READY`) - **VERIFIED** |
| 5 | Foreign-container safety | Every engine call filters on `label sandbox.managed=true`; hand-created containers in dind are invisible to list/delete/reconcile. | manual container created in dind survived verify + reconcile untouched - **VERIFIED** |
| 6 | Exec edge cases | `409` unless state `READY`; per-call timeout (bounded <=300 s) returns `timed_out:true` with partial output; streams capped at 64 KiB with `truncated:true`; non-zero exit codes returned in-band. | verify 5 (exit 7, stderr capture, 2 s timeout on `sleep 30`, 200 KB output) - **VERIFIED** |
| 7 | Delete idempotency | Missing/`DELETED` -> `404`; `CREATING` -> `409`; concurrent delete+reaper serialized by the per-sandbox lock, both paths idempotent. | verify 9 - **VERIFIED** |
| 8 | DinD unavailable | `engine.ping()` gate on mutating endpoints -> `503` + `Retry-After: 5`; `/healthz` reports `"dind":"down"`; docker client timeout is 8 s so calls fail fast instead of hanging. Startup survives dind being down (reconcile skipped, logged). | `docker pause` on dind -> `503` observed; unpause -> recovery - **VERIFIED** |
| 9 | TTL race | Reaper and manual DELETE both go through `_lock_for(sid)` and re-read the row inside the lock; loser sees `DELETED` and backs off. | loop of 50 TTL-vs-delete races, zero torn sandboxes - **VERIFIED** (dev test) |
| 10 | Capacity cap | `MAX_SANDBOXES` checked against `engine.engine_sandbox_ids()` (label-derived, survives DB wipe); over cap -> `429` + `Retry-After: 30`. | `MAX_SANDBOXES=3` run: 4th create -> 429 - **VERIFIED** |
| 11 | Name validation | Pydantic validator enforces `^[a-z0-9][a-z0-9-]{1,40}$`; unknown template rejected with the valid list. Both -> `422`. | verify 10 - **VERIFIED** |
| 12 | Event-log integrity | `store.add_event()` computes `MAX(seq)+1` per sandbox under the write lock - no gaps/dupes even under concurrency; `GET .../events` returns seq-ordered history incl. reconcile entries after restarts. | verify 11 - **VERIFIED** |

## Known limits (deliberate, documented - not bugs)

- **Single API process**: per-sandbox locks and the alloc write-lock live in
  process memory. Horizontal scaling of the API is a T12 concern (the
  distributed scheduler owns placement; this API stays per-engine).
- **`deadline_mono` doesn't survive API restart** (monotonic clocks are
  per-process). On boot, sandboxes with a TTL get a fresh deadline of
  `now + ttl_seconds` - documented in IMPLEMENTATION.md; the reaper keeps
  working, the expiry just slides by one restart. (Fix lands with the
  T07 event store: wall-clock deadlines derived from replayed events.)
- **Blank template has no port**; identity mapping applies only to
  port-holding templates (`web`, `coreapp`).
- Coreapp first-create latency is dominated by building its two images
  inside dind (cached afterwards in `dind-data`). `READY_TIMEOUT_COREAPP`
  (default 180 s) bounds the readiness wait; the create itself is async.
