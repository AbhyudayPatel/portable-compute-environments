# sandbox-checkpoint - Edge Cases

The T05 list from `docs/AGENTOS-TASKLIST.md`, each mapped to how it is
handled and to its assertion in `scripts/verify.sh` (47 assertions).
**VERIFIED** unless marked **EXPECTED**.

| # | Edge case (tasklist) | Handling | Test |
|---|---|---|---|
| 1 | Checkpoint during active writes - restore must be transactionally consistent | quiesce hook (`CHECKPOINT`) then **pause** (cgroup freezer) before copying: the copy is a crash-consistent instant; Postgres replays its own `pg_wal` on restore, so only committed state comes back. The DB fingerprint after restore equals the one recorded in the manifest. | verify 6 |
| 2 | Restore onto a different port | state holds no absolute URLs; a fork gets its own port and its app answers there | verify 5 |
| 3 | Checkpoint of a crashed/stopped container | STOPPED sandboxes are snapshotted without pause; `was_running=false` in the manifest; the sandbox stays STOPPED | verify 7 |
| 4 | Corrupt / incomplete snapshot (service killed mid-copy) | manifest is written **last** (commit point). A dir without manifest is invisible; `gc` marks checkpoints whose layer dirs or manifest vanished `CORRUPT`; fork/restore refuse CORRUPT (409) | verify 10, 12 |
| 5 | Restore version skew (image tag missing) | forks reuse the manifest's template; images are content-hashed tags in sandbox-api so a changed app yields a new tag, never a silent `latest` | by construction (EXPECTED: a deliberately removed inner image makes the fork FAIL with the build/pull error in its events) |
| 6 | Fork while parent is mutating | forks mount the checkpoint's immutable layer dir, never the live upper; verify writes to the parent before forking and asserts the fork sees checkpoint state | verify 5 |
| 7 | Storage growth / GC never deleting a referenced layer | layers shipped to the blob store under owner `checkpoint:<cid>`; refs dropped only on checkpoint delete; blob GC honours refs; node dirs removed only by delete; delete refused while children or sandboxes depend on it | verify 9 (409, then 204 + refs gone) |
| 8 | DB vs file divergence (a row and a file written together both reappear) | both live in the same frozen instant (pause) and both are restored from the same checkpoint | verify 6 (row `before-c1` + file `v1`) |
| 9 | Rollback while a client is connected | restore stops the sandbox first; connections drop; clients see the app come back on the same port | verify 6 (app answers after restore); gateway-level draining is T04 (EXPECTED) |
| 10 | Restore-failure rollback | the previous upper is discarded only after the new overlay mounted; if `start` fails the sandbox is FAILED/DEGRADED with its events, and the checkpoint is intact for a retry | EXPECTED (not injected) |

## Beyond the list

| Edge case | Handling | Test |
|---|---|---|
| PITR target between checkpoints | newest checkpoint at or before the LSN + live WAL archive replay to the LSN, `recovery_target_action=promote` | verify 8 (row before LSN present, row after absent, out of recovery) |
| WAL archive dir ownership | Postgres archives as uid 70; template `chown` on the plain bind; without it `archive_command` fails forever (found during the build) | verify 2 |
| Non-stateful sandbox | 400 - checkpoints need overlay-backed data paths | verify 11 |
| Unknown checkpoint | 404 | verify 11 |
| Blob store down while shipping | `ship=false` still creates a node-only checkpoint; with shipping on, a store error aborts the checkpoint before the manifest is written (nothing half-committed) | EXPECTED |
| Checkpoint of a PAUSED sandbox | refused (409) - the caller owns the pause | code path |
| Fingerprint of Postgres data dir | volatile files excluded (`pg_wal`, `pg_stat_tmp`, `postmaster.pid`...) so two checkpoints of unchanged data compare equal | verify 4 (diff reports only real changes) |

## Known limits

- Each checkpoint copies the full delta since the sandbox's base (no
  incremental rotation; see ARCHITECTURE.md).
- PITR is implemented for the `coreapp` template's Postgres; other data
  paths restore to the checkpoint only.
- Import rebuilds node layers from blobs but not the index row; the index
  is authoritative for *which* checkpoints exist (manifests on the node are
  the backup, adoption is EXPECTED).
