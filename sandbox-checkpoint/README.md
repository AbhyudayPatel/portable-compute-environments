# sandbox-checkpoint - T05: Checkpoint, fork, restore, point-in-time

> **Git for execution state.** Freeze a running sandbox's filesystem *and*
> database into an immutable checkpoint in about a second, fork new
> sandboxes from it copy-on-write, roll a sandbox back in place, and - for
> CoreApp's Postgres - replay the archived WAL to any LSN between
> checkpoints. Layers are shipped to the blob store so a checkpoint can be
> re-materialised after its node copy is lost.

```
you --> checkpoint :9040 --> sandbox-api :9000 (pause / volumes snapshot+restore / node tar)
                         --> sandbox-blobs :9400 (layers as content-addressed blobs)

sandbox (stateful)                   checkpoint c1              fork
  db     /var/lib/postgresql/data    /var/lib/sbx/ckpt/c1/db    overlay: c1/db  <- fresh upper
  backend /workspace                 /var/lib/sbx/ckpt/c1/backend                 ...
  wal    /wal (plain dir)            /var/lib/sbx/ckpt/c1/wal   copied
                                     manifest.json (written LAST = the commit)
```

## Quick start

```powershell
# prerequisites, in this order:
cd ..\sandbox-api;   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-blobs; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-checkpoint; powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

```bash
# 1. a STATEFUL sandbox (overlay-backed data paths + WAL archive)
curl -s -X POST localhost:9000/sandboxes -H 'content-type: application/json' \
  -d '{"name":"s","template":"coreapp","options":{"stateful":true}}'
# 2. checkpoint it (while running; ~1-2 s pause)
curl -s -X POST localhost:9040/sandboxes/<id>/checkpoints -H 'content-type: application/json' -d '{"label":"v1"}'
# 3. fork it
curl -s -X POST localhost:9040/checkpoints/<cid>/fork -H 'content-type: application/json' -d '{"name":"s-fork"}'
# 4. break something, then roll back in place
curl -s -X POST localhost:9040/checkpoints/<cid>/restore -H 'content-type: application/json' -d '{}'

bash scripts/verify.sh      # 47 assertions, ~8 min
```

## What a checkpoint is

| Step | What happens | Why |
|---|---|---|
| quiesce | template hook (`psql -c CHECKPOINT`, then `pg_switch_wal`) | fewer WAL records to replay; the archive is complete up to the recorded LSN |
| pause | `POST /sandboxes/{id}/pause` (cgroup freezer) | every process stops between two instructions: a crash-consistent instant, like a power cut |
| snapshot | `POST /sandboxes/{id}/volumes/snapshot` copies each overlay **upper** (the delta since the sandbox started) and the WAL dir to `/var/lib/sbx/ckpt/<cid>/` | O(delta), not O(data) |
| unpause | the sandbox continues; downtime is the copy time (~1 s for a fresh CoreApp) | |
| fingerprint | merkle roots of the data paths + per-table DB hashes, recorded in the manifest | diffs between checkpoints without touching the layers |
| ship | each layer dir streamed as tar into sandbox-blobs under owner `checkpoint:<cid>` | survives node loss; dedup across checkpoints |
| manifest | `manifest.json` written to the node last, index row inserted, lineage updated | a dir without a manifest is not a checkpoint |

A **fork** creates a new sandbox whose overlays mount the checkpoint's layer
directories as read-only lowers under a fresh upper: it starts in seconds,
shares the bytes, and diverges privately. A **restore** stops the sandbox,
remounts its volumes the same way (dropping its current upper), copies the
WAL archive back and starts it. **PITR** restores the newest checkpoint at or
before a target LSN and lets Postgres replay the live WAL archive up to it.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/sandboxes/{sid}/checkpoints` | `{label?, ship?}` -> manifest (201) |
| GET | `/sandboxes/{sid}/checkpoints` | lineage + the checkpoint the sandbox currently sits on |
| GET | `/checkpoints[/{cid}]` | list / manifest with children and forks |
| GET | `/checkpoints/{cid}/tree` | ancestors, descendants, forks |
| GET | `/checkpoints/{cid}/diff?against=` | fingerprint diff (per role; table rows) |
| POST | `/checkpoints/{cid}/fork` | `{name, isolation?}` -> new sandbox |
| POST | `/checkpoints/{cid}/restore` | `{sandbox?}` in-place restore (default: origin) |
| POST | `/sandboxes/{sid}/pitr` | `{lsn}` point-in-time recovery |
| POST | `/checkpoints/{cid}/import` | re-create node layers from the blob store |
| DELETE | `/checkpoints/{cid}[?force=1]` | 409 while children or sandboxes depend on it |
| POST | `/gc` | verify layers, mark `CORRUPT`, drop stale blob refs |
| GET | `/healthz` `/stats` | |

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - overlay layers, the
  commit rule, lineage, PITR, what lives where.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - the T05 edge-case list from
  the AgentOS tasklist mapped to code and to verify.sh.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files, build story,
  bugs found (root-owned WAL dir, overlay-on-overlay).
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - branch-per-experiment and
  time-travel exercises.
- [`docs/SECURITY.md`](docs/SECURITY.md) - what root-on-node powers this
  service holds through sandbox-api, and why they are path-restricted.
