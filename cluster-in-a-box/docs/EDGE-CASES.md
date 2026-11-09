# cluster-in-a-box - Edge Cases

The T12 list from `docs/AGENTOS-TASKLIST.md`, mapped to this implementation
and to `scripts/verify.sh` (31 assertions). **VERIFIED** unless marked
**EXPECTED**.

| # | Edge case (tasklist) | Handling | Test |
|---|---|---|---|
| 1 | Split brain (two control planes) | single control plane here; **epochs** fence stale worker copies (a returning DEAD worker gets epoch+1 and its stale placements are purged). Leader election between control planes: EXPECTED (Postgres advisory lock design in the tasklist) | 8 (epoch, purge) |
| 2 | Worker flapping | hysteresis: 3 misses -> SUSPECT, 5 -> DEAD; a single missed heartbeat changes nothing | 7 |
| 3 | Heartbeat delay != death | placements are only re-placed on an explicit `reschedule` (or drain), never automatically on SUSPECT; a slow worker is not duplicated | 7 (UNKNOWN until reschedule) |
| 4 | Migration losing in-flight work | drain copies `data_path` with the agent tar API before deleting the source; exec sessions are not migrated (EXPECTED: sessions live in the agent of the old sandbox) | 6 |
| 5 | Clock skew across workers | the control plane uses its own clock for misses; workers never exchange timestamps | by construction |
| 6 | Partial migration | the new copy is created and READY before the old one is deleted; a failure leaves the old copy and the placement unchanged | `migrate()` order; EXPECTED (not injected) |
| 7 | Control-plane DB down | SQLite file; if the control plane dies, workers and placements keep running; on restart heartbeats resume (EXAMPLES level 5) | EXPECTED |
| 8 | Version skew | workers run the same `sandbox-api` package the outer node runs (same image content); a mismatch would surface in `/healthz` shape - EXPECTED |
| 9 | Ghost sandboxes after a worker restart | `fence()` re-adopts placements still present, marks LOST those that vanished, purges stale copies | 8 |
| 10 | Network partition of a worker | identical to "stopped" from the control plane's view: SUSPECT -> DEAD, placements UNKNOWN; the worker keeps running locally; on return, fencing | 7-8 (stop/start models it) |

## Beyond the list

| Edge case | Handling | Test |
|---|---|---|
| Three-engine port identity | worker `extra_ports` + the outer dind's `CLUSTER_PORTS` publish; each placement's URL works from Windows | 3 |
| Placement when nothing fits | `pick_worker` -> 503 (no ALIVE worker with capacity / no label match) | 5 |
| Worker capacity | `WORKER_MAX_SANDBOXES` per worker counted from placements | 4 (binpack fills one worker) |
| Worker image build | built inside the outer dind from a context the API assembles (app + coreapp + agent + entrypoint); content-hashed tag | 2 (first run) |
| Worker is also a sandbox | outer `exec` into it shows its own `docker ps`; outer events/stats apply | 2 |
| Drained worker reuse | state returns to ALIVE with zero placements | 6 |
| Delete worker with placements | placements LOST, outer sandbox deleted | 9 |

## Known limits

- One control plane instance (no election).
- Stateful placement migration via checkpoints is EXPECTED (needs a
  checkpoint service per worker).
- Memory: ~200 MB per idle worker; three workers plus the outer stack fit
  the 3.4 GB VM used for verification.
