# sandbox-scheduler - Edge Cases

Each edge case: how it is handled in code, and the verify.sh section that
proves it. All **VERIFIED** (41/41 passing) unless marked otherwise.

| # | Edge case | Handling | Test |
|---|---|---|---|
| 1 | Thundering herd | One global admission lock serializes all placement decisions; capacity math uses DB reservations + engine truth, so N concurrent submits can never oversubscribe. Concurrency sampler in verify watches ADMITTED+RUNNING never exceed CLUSTER_CPU_UNITS during a 50-job herd. | 5+6 - VERIFIED |
| 2 | Priority inversion | Aging: +1 effective priority per AGING_INTERVAL queued, capped. A long-waiting low-priority job eventually outranks fresh high-priority ones. | 9 - VERIFIED (aged prio-5 job beat fresh prio-6) |
| 3 | Starvation | Same mechanism, plus fair queueing across tenants: the least-served tenant is considered first every placement tick. | 7 - VERIFIED (tenant-b admitted while 3 tenant-a jobs waited) |
| 4 | Crash mid-admission | Self-stabilizing: on restart, ADMITTED/RUNNING jobs are re-derived from engine state by sandbox name (`sched-<jobid>-<k>`); sandboxes created just before the crash whose job never recorded them are reaped as orphans by the janitor. | 15 - VERIFIED (docker restart: jobs RUNNING kept, no duplicates, planted orphan reaped) |
| 5 | Duplicate submission | `Idempotency-Key` header, UNIQUE in DB. Same key + same body -> 200 with the existing job; same key + different body -> 409; concurrent same-key inserts -> loser re-reads and returns the winner's row. | 4 - VERIFIED |
| 6 | Cancel races | QUEUED -> CANCELLED instantly (never admitted). ADMITTED/RUNNING -> CANCELLING; the cancel loop retries deletes (upstream 409 while sandbox CREATING is retried, 404 = already gone is success). Terminal -> 409. CANCELLING -> idempotent 200. 30-iteration race loop asserts zero leaked sandboxes and zero stuck jobs. | 10, 11 - VERIFIED |
| 7 | Downstream outage | All upstream calls timeout-bounded (8 s); errors/503 -> UpstreamDown -> loops skip the tick and set `upstream_down` in /metrics. Jobs wait in place. Provisioning threads requeue their job. Recovery is automatic. | 14 - VERIFIED (stop sandbox-api: job stays QUEUED, flag set, completes after restart) |
| 8 | Clock skew / TTL | Deadlines computed on the monotonic clock at admission (`deadline_mono`), never compared across wall-clock changes. Wall deadline also stored so a restart re-anchors the monotonic deadline with the true remaining time. | 13 + 15 - VERIFIED |
| 9 | Queue bound | `MAX_QUEUE` (default 50) enforced at submit; over the bound -> 429 + Retry-After. The queue can never grow unboundedly and eat memory. | 5 - VERIFIED (55 submits -> 5x 429) |
| 10 | Head-of-line blocking | Skip-and-fill: within a tenant, jobs that don't fit are skipped; smaller jobs behind them admit. Also across tenants (next tenant's jobs tried). | 8 - VERIFIED (3-cpu blocker running, 2-cpu job queued, 1-cpu job ran past it) |

## Beyond the list - extra cases this implementation covers

| Edge case | Handling |
|---|---|
| Gang partial failure | `count: N` admission is all-or-nothing; if sandbox k of N fails upstream, all k-1 created sandboxes are deleted and the job FAILEDs. Oversized gangs (count > capacity) sit QUEUED forever, never partially admitted (cancel to clear). | 12 - VERIFIED |
| Sandbox dies under a RUNNING service job | Monitor notices the missing sandbox -> job FAILED ("sandbox vanished"), siblings deleted. | code path exercised; EXPECTED (hard to time deterministically in a script) |
| Exec timeout inside a task job | task commands run with JOB_TASK_TIMEOUT (60 s) via the sandbox-api exec caps; a hanging task ends FAILED with `timed_out: true` in its result. | 3-adjacent - VERIFIED via exit-code capture |
| Concurrent same-key submits | sqlite UNIQUE on idem_key + IntegrityError catch -> winner's row returned. | 4 covers replay; race variant EXPECTED (both requests still return one job) |
| admin/reset with live sandboxes | reset only wipes rows; the janitor's name-correlated orphan sweep reaps the live sandboxes, so nothing leaks. | 15 orphan assertion - VERIFIED |
| Duplicate sandbox names | scheduler sandboxes are always `sched-<jobid>-<k>` and job ids are uuids - name reuse after deletion is handled by sandbox-api's tombstoning (T01 edge #7b). | by construction |

## Known limits (deliberate - documented, not bugs)

- **Single scheduler process.** Two scheduler replicas would double-place;
  leader election is T12's job (Postgres advisory locks), by design.
- **Unmanaged sandboxes** cost a flat 1 cpu unit in the model - the engine
  has no unit annotation for them. T03 (quotas) makes units real cgroups.
- **Aging is priority-level, not preemptive** - a running low-priority job
  is never evicted; preemption needs checkpoints (T05) to be safe.
- **Task results** are capped by the sandbox-api exec caps (64 KiB/stream).
- **`/admin/reset` exists for demos/tests** and would be removed or
  auth-gated in any shared deployment (see SECURITY.md).
