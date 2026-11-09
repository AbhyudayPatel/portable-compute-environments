# sandbox-api v2 - Edge Cases

Legend: **VERIFIED** = executed and observed by `scripts/verify.sh`
(89 assertions, 17 sections) or by the simulator (`../sandbox-sim`,
76 tests + 500 seeded fuzz runs). **EXPECTED** = reasoned, not executed.

## Lifecycle (v1 contract, re-verified on v2)

| # | Edge case | Handling (code) | Test |
|---|---|---|---|
| 1 | Idempotent create | same name+spec -> 200 existing; different spec -> 409; `name UNIQUE` + tombstoning on delete | verify 4; sim `test_idempotent_create_same_spec_returns_existing` |
| 2 | Port-pool exhaustion | `alloc_port` None -> row FAILED (no port), 429 + Retry-After, nothing created | verify 13; sim `test_port_exhaustion_...` |
| 3 | Partial creation failure | `create_sandbox_resources` rollback closure removes everything it made; provision marks FAILED and frees the port | sim `test_partial_create_failure_rolls_back_and_frees_port` (fault: container 2 of 3 fails) |
| 4 | API crash / restart | boot `reconcile()`: adopt orphans, finish interrupted deletes, roll back orphaned CREATING rows, sync states, re-anchor TTLs | verify 16; sim `test_crash_mid_create_is_rolled_back_on_boot` + random crashes in every fuzz seed |
| 5 | Foreign-container safety | every engine call filters on `sandbox.managed=true` | by construction (unchanged) |
| 6 | Exec edge cases | 409 unless READY/DEGRADED; timeout **kills** the process group (exit 143) - no leftover process; 64 KiB caps with `truncated:true` and the true `bytes_total`; non-zero exit in-band | verify 5 |
| 7 | Delete idempotency | missing/DELETED -> 404; CREATING -> 409; reaper vs delete serialized by the per-sandbox lock | verify 15; sim `test_reaper_vs_delete_race_is_idempotent` |
| 8 | Engine unavailable | `ping()` gate -> 503 + Retry-After; 8 s client timeout; boot survives dind down | sim `test_engine_down_gives_503_never_hangs`; fuzz `engine_down` fault |
| 9 | TTL race | reaper and manual delete both lock + re-read; loser sees DELETED | sim (every fuzz seed mixes reaper sweeps and deletes) |
| 10 | Capacity cap | `MAX_SANDBOXES` vs engine-derived count -> 429 | sim invariant I3 (engine never over capacity) |
| 11 | Name validation | `^[a-z0-9][a-z0-9-]{1,40}$`; unknown template; unsupported isolation -> 422 | verify 4 |
| 12 | Event-log integrity | `MAX(seq)+1` under the write lock; contiguous; readable after deletion | verify 3, 15; sim invariant I4 |

## New in v2

| # | Edge case | Handling | Test |
|---|---|---|---|
| 13 | **TTL across restarts** | `deadline_wall` stored; boot reconcile re-derives `deadline_mono`; the reaper never compares wall clocks | verify 14 (restart mid-TTL: deadline unchanged, fires on time); sim `test_ttl_survives_restart_via_wall_deadline` + mutation test proving the sim catches the v1 bug |
| 14 | **Container dies under a READY sandbox** | informer `die`/`oom` -> DEGRADED with `detail.dead`; `start` restarts dead containers -> READY | verify 11; sim `test_informer_marks_degraded_and_start_heals` |
| 15 | **stop() emits die events too** | handler re-reads state under the lock; STOPPED explains the events -> ignored | sim `test_stop_then_die_events_do_not_degrade` + mutation `test_catches_informer_ignoring_stop_lock` |
| 16 | **Containers removed outside the API** (`docker rm -f` inside dind) | informer `destroy` -> if nothing left in engine: DELETED, port freed | verify 12; sim `test_out_of_band_removal_marks_deleted_and_frees_port` |
| 17 | **Resync vs in-flight create** | `core.in_flight` registered in `create()` (not when the thread starts): reconcile never rolls back an owned CREATING row | verify 13 was the detector (rollback 20 ms after create); sim fuzz |
| 18 | **API boots while dind is down** | `boot_pending` stays true; the first periodic resync that reaches the engine runs in boot mode | sim fuzz (`CRASH` while `ENGINE DOWN`) - this was the first bug the simulator found |
| 19 | **FAILED row with leftovers** (engine went down mid-rollback) | reconcile destroys engine resources of FAILED rows | sim invariant Q1 "FAILED owns nothing" |
| 20 | **Provision rolled back under it** | generator re-checks `CREATING` between steps; aborts and destroys what it created | sim fuzz (seed 20 found it) |
| 21 | Session resume | `?since=` replays from the ring; eviction reported as `{"gap":true}` | verify 6 (resume returns only new frames); agent test (2 KiB ring, 20 KiB output -> gap + dropped_frames) |
| 22 | Session kill / delete | signal to process group; delete kills if running; 404 after delete | verify 6 |
| 23 | Pty resize | `TIOCSWINSZ` on the pty; `stty size` reflects it | verify 6 |
| 24 | Tar path escape on upload | entry names cleaned relative to the destination; `../../etc/evil` lands under the destination | verify 7 |
| 25 | File jail | `SBX_AGENT_FS_ROOT` + symlink resolution of the deepest existing ancestor | agent code; templates use `/` today (EXPECTED for a jailed template) |
| 26 | Fingerprint determinism | sorted walk, mtimes ignored; same tree -> same root; one byte -> new root | verify 8 |
| 27 | Diff of unrelated sandboxes | per-entry comparison; self-diff identical | verify 8 |
| 28 | DB fingerprint | rows hashed in canonical order; insert changes the table hash and the root | verify 9 |
| 29 | gVisor unavailable | create with `isolation:gvisor` -> 400 if the engine has no `runsc` | code path; EXPECTED (engine always has it here) |
| 30 | gVisor + published port + Postgres | verified: web under runsc serves through the identity port; Postgres starts under runsc (init-restart window: connect via TCP or retry) | verify 10; standalone probe in IMPLEMENTATION.md |
| 31 | Desktop readiness | agent started via `docker exec` (s6 stays PID 1); readiness = agent + KasmVNC HTTP | verified manually (IMPLEMENTATION.md); not in verify.sh because the first build pulls a 3.9 GB base |
| 32 | Init hooks from a Windows build context | linuxserver init refuses non-executable hooks; Dockerfile `chmod +x` | verified (the first build silently skipped the hook) |
| 33 | API restart mid-`exec`/session | sessions live in the agent: the API restart loses nothing; resume with `since=` | verify 16 is the restart; session continuity EXPECTED (agent-side verified by the standalone agent test) |
| 34 | Engine (dind) restart | all inner containers stop; reconcile marks sandboxes STOPPED; `start` resumes them; exec-mode agents are relaunched by start | observed during the build (IMPLEMENTATION.md) |
| 35 | Wall-clock skew | reaper uses monotonic deadlines only; wall is used once at boot | sim fuzz `skew` fault |
| 36 | SSE resume | per-sandbox `since=` and global `cursor=` replay from the store, then live; 15 s heartbeats; bounded lifetime | verify 17 |

## Stateful volumes and pause (used by ../sandbox-checkpoint)

| # | Edge case | Handling | Test |
|---|---|---|---|
| 37 | Overlay on the wrong filesystem | overlay refuses `upperdir` on overlayfs; volumes live on the ext4 `sbx-state` volume | probed during the build (IMPLEMENTATION.md) |
| 38 | Root-owned archive dir | Postgres archives as uid 70; template declares `chown` for plain binds | checkpoint verify 2 (WAL segments appear) |
| 39 | Stop while PAUSED | 409 "unpause before stopping" (a frozen process cannot handle SIGTERM) | code path; sim fuzz `pause`/`stop` steps |
| 40 | Reconcile sees paused containers | engine state `PAUSED` when every container is running-or-paused and at least one is paused | sim invariant Q2 |
| 41 | Delete of a stateful sandbox | `destroy_sandbox_resources` unmounts and removes `/var/lib/sbx/vol/<sid>` (idempotent, no-op for stateless) | checkpoint verify cleanup; node dir gone |
| 42 | Create rollback with volumes | rollback closure also tears volumes down | code path (EXPECTED) |
| 43 | Node ops path restriction | `/node/*` only accept `/var/lib/sbx/...`; `DELETE /node/dir` only `/var/lib/sbx/ckpt/...` | checkpoint verify 10, 12 |

## Known limits (deliberate, documented)

- **Single API process.** Locks, `in_flight`, the bus live in memory.
  Horizontal scaling is a T12 concern.
- **Agent injection requires a writable root** (put_archive to `/sbx`).
  Read-only root images need a volume mount - EXPECTED, not implemented.
- **`desktop` is container-only** and heavy (3.9 GB base image, ~500 MB
  RAM live). One desktop at a time on a 3.4 GB Docker Desktop VM.
- **No auth on the API** (unchanged; see SECURITY.md). Agent tokens
  protect sandbox-to-sandbox, not client-to-API.
- **Upload endpoints buffer the body in the API** before relaying (tar
  upload of hundreds of MB would need a streaming relay).
