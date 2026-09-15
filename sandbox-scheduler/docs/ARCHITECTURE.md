# sandbox-scheduler - Architecture

## The one-sentence idea

A pure control plane: **the scheduler never touches docker** - it spends
abstract capacity budgets and drives the T01 sandbox-api over HTTP, adding
the things a raw sandbox API lacks: queueing, fairness, aging, admission
control, expiry, and crash-safe bookkeeping.

```
 clients
   |
   v
 +--------------------------------------------------------+
 |  scheduler (FastAPI, single process, threads)          |
 |                                                        |
 |   POST /jobs -> jobs table (SQLite, QUEUED)            |
 |        ^                                               |
 |        | placer loop (1s)                              |
 |        |   engine view -> capacity math -> select      |
 |        |   (WFQ across tenants, skip-and-fill, aging)  |
 |        |   -> ADMITTED -> provision thread             |
 |        v                                               |
 |   monitor loop (2s): ADMITTED -> RUNNING; task exec    |
 |   janitor loop (3s): max_runtime expiry, orphan sweep  |
 |   cancel loop  (1s): CANCELLING -> CANCELLED (retry)   |
 +--------------------------------------------------------+
            |  HTTP (requests, 8s timeout, UpstreamDown)
            v
   sandbox-api (T01)  ->  dind  ->  sandboxes
```

## Data model

`jobs` table: id, idem_key (UNIQUE), name, tenant, template, kind, cmd,
priority, count (gang size), cpu_units, state, sandbox_ids, result,
max_runtime, **queued_mono** (monotonic anchor for aging), **deadline_mono**
+ **deadline_wall** (expiry), timestamps, error.

`events` table: `(job_id, seq)` primary key - same ordered-audit-trail
pattern as T01.

## State machine

```
                 submit
                   |
                   v
   cancel ->    QUEUED ---------------+
     |           |                    |  (placer: fits capacity)
     |           v                    v
     |        ADMITTED -> provision -> RUNNING
     |           |                     | task: exec cmd
     |           |                     +-> SUCCEEDED (exit 0, output captured)
     |           |                     +-> FAILED    (exit != 0 / sandbox died)
     |           |                     +-> EXPIRED   (max_runtime, janitor)
     |           v                     |
     +------ CANCELLING <--------------+
                   |
                   v (cancel loop: deletes retried until engine says gone)
               CANCELLED
```

## The capacity model

Three budgets, all must fit:

| budget | source of truth | default |
|---|---|---|
| cpu units | scheduler reservations (sum of active jobs) minus engine truth for unmanaged sandboxes | 4 |
| sandbox slots | engine: total sandbox count (includes foreign ones) | 8 |
| app ports | sandbox-api `/status` pool.free | 10 |

`free_cpu = CLUSTER_CPU_UNITS - reserved - 1*unmanaged_sandboxes`
(unmanaged = sandboxes in the engine not created by the scheduler - each
counts as 1 unit, documented approximation).

## The selection policy (placer)

Every tick, under the single admission lock:

1. Group QUEUED jobs by tenant.
2. Order tenants by current sandbox count, least-served first (equal
   weights -> plain fair queueing; weights land in a later task).
3. Within a tenant: order by `(effective_priority desc, age asc)`;
   **skip-and-fill** - the first job that fits is admitted; jobs that
   don't fit are skipped without blocking smaller ones.
4. Repeat until nothing fits.

**effective_priority = priority + min(AGING_MAX_BONUS, floor(queued_seconds
/ AGING_INTERVAL))** - so a priority-5 job that waited 10 s with the default
config has effective 7 and beats a fresh priority-6 job. Starvation is
provably impossible: every job's effective priority reaches max in bounded
time, and oldest-effective-priority wins ties.

## Crash recovery - self-stabilizing by design

The scheduler can die at any point; on boot `reconcile()` plus the loops
converge it back:

- **QUEUED** jobs just resume being placed.
- **ADMITTED/RUNNING** jobs are re-derived from the engine by name
  correlation (`sched-<jobid>-<k>`): the monitor loop re-adopts live
  sandboxes, or marks the job FAILED if they vanished.
- **CANCELLING** jobs: the cancel loop keeps deleting until the engine
  confirms gone.
- **Clocks:** monotonic anchors are re-based from wall-clock timestamps
  (`queued_mono` keeps the job's age for aging; `deadline_wall` re-derives
  `deadline_mono`), so TTLs survive restarts honestly.
- **Orphans:** a `sched-*` sandbox with no live job row (e.g. created just
  before a crash) is reaped by the janitor's sweep.

The invariant: *engine state and DB state always converge, never diverge.*

## The four loops, and why separate

| loop | cadence | owns |
|---|---|---|
| placer | 1 s | admission decisions only (fast, lock-held briefly) |
| monitor | 2 s | state transitions + task execution |
| janitor | 3 s | expiry + orphan sweep |
| cancel | 1 s | teardown with retry |

Separation keeps each loop small enough to reason about, and means a slow
upstream only stalls the affected loop. Every loop catches UpstreamDown ->
`upstream_down=true` in metrics, jobs simply wait.

## Upstream failure behavior

All upstream calls go through `upstream.py` with an 8 s timeout; connection
errors and 503s raise `UpstreamDown`. Placer/monitor/janitor/cancel treat
it as "do nothing this tick" - no hot loop, no state corruption, automatic
resume. A provisioning thread that hits it mid-create **requeues** the job
(QUEUED again with an event), because admission promises are cheap to redo.
