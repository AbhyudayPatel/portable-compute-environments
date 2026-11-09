# sandbox-scheduler v2 - Architecture

## The one-sentence idea

A pure control plane: the scheduler never touches docker. It spends
abstract capacity budgets and drives sandbox-api (and, for preemption,
sandbox-checkpoint) over HTTP, adding queueing, fairness, aging,
deadlines, preemption, memory-aware admission, expiry and crash-safe
bookkeeping - as a **core with no threads, no HTTP and no clock** that
the simulator can drive deterministically.

```
 clients
   |
   v
 +----------------------------------------------------------------------+
 |  main.py (FastAPI + 4 loops + worker threads)                         |
 |     placer loop   -> core.placer_tick()  -> provision_steps() gens    |
 |                                           -> preempt_steps() gens     |
 |     monitor loop  -> core.monitor_tick() -> run_task() threads        |
 |     janitor loop  -> core.janitor_tick()                              |
 |     cancel loop   -> core.cancel_tick()                               |
 |                                                                       |
 |  core.py (policy + state machine)   store.py (SQLite)  clock.py       |
 +----------------------------------------------------------------------+
            |  upstream.py (requests, 8 s timeout, Down/Conflict)
            v                           v
   sandbox-api (T01)         sandbox-checkpoint (T05): checkpoint_create, fork
```

In `../sandbox-sim`, `sim/sched.py` replaces `upstream.py` with an adapter
that calls the sandbox-api core in-process (same virtual clock, same
seed) and a fake checkpoint service; the loops become scenario steps.

## Data model (v2 columns in bold)

`jobs`: id, idem_key, name, tenant, template, kind, cmd, priority, count,
cpu_units, mem_units, state, sandbox_ids, result, max_runtime,
queued_mono, deadline_mono, deadline_wall, created/admitted/finished_at,
error, **deadline_at, preemptible, stateful, checkpoint_id, preempt_count,
mem_bytes, deadline_missed**. `events`: `(job_id, seq)`.

## State machine

```
   submit ->  QUEUED --(fits)--> ADMITTED --(all READY)--> RUNNING --task--> SUCCEEDED/FAILED
                 ^                  |                         |  \-- max_runtime --> EXPIRED
                 |                  | upstream down           |
                 +------------------+                         | chosen as victim
                                                              v
   PREEMPTED <--(checkpoint ok, sandbox deleted)-- PREEMPTING
       |  (queued with resume bonus; admitted again -> ADMITTED, provision = FORK)
   any non-terminal --cancel--> CANCELLING --(sandboxes gone)--> CANCELLED
```

## The placement tick

1. `engine_view()`: one `/status` + `/sandboxes` call -> slots, ports,
   unmanaged sandboxes, memory budget (75% of engine RAM unless set).
2. Loop: `select_next()` ranks QUEUED+PREEMPTED jobs per tenant by
   `(effective priority desc, deadline asc, age)`, tenants least-served
   first, and returns the first job that fits (`_fits`: tenant cap, cpu,
   slots, ports, memory). Admit it, update the view, repeat.
3. When nothing fits, `select_next` also returns the **head**: the
   highest-effective-priority job blocked specifically on CPU. If its
   effective priority >= `PREEMPT_PRIORITY`, `choose_victims` picks the
   lowest-priority RUNNING, preemptible, stateful, single-sandbox services
   until enough cpu units would be freed (or nobody, if it cannot free
   enough). Victims go to `PREEMPTING`; the head is admitted on a later
   tick once the units are free.

Why "blocked on CPU" only: preemption frees cpu units; a job blocked by its
tenant cap, by slots or by ports would not benefit, so no victim is taken
for it.

## The preemption protocol

```
preempt_steps(jid):
   1. checkpoint_create(sandbox)    (sandbox-checkpoint: quiesce, pause, snapshot, unpause)
   2. delete_sandbox(sandbox)        (units freed here)
   3. job: sandbox_ids=[], checkpoint_id=<cid>, state PREEMPTED, queued_mono reset, preempt_count+1
on any error: job back to RUNNING, event "preemption aborted" (the head keeps waiting)

resume = provision_steps(jid) with checkpoint_id set:
   fork_checkpoint(cid, name sched-<jid>-0)  instead of create_sandbox
```

The victim's data (overlay volumes, WAL) is exactly what the checkpoint
captured; it resumes as a new sandbox on a new port. Requirements:
`stateful` (so there is something to checkpoint) and `count == 1` (gang
preemption is not supported: documented, EXPECTED).

## EDF and deadline accounting

`deadline_seconds` at submit becomes `deadline_at` (wall). It only
influences order among equal effective priority (priority and aging stay
the primary signal - a deadline is not a way to jump the queue). When a
job finishes after `deadline_at`, `deadline_missed` is set and
`deadline_missed_total` increments.

## Memory budget

The monitor asks sandbox-api `/sandboxes/{id}/stats` for each RUNNING
job's containers (cgroup `memory.current` via the agent) and stores the
sum as `mem_bytes`. Admission estimates `observed + MEM_RESERVE_PER_UNIT x
cpu_units` and refuses when it exceeds `MEMORY_BUDGET_BYTES`
(`memory_refusals_total`). This is the seed of T03: real signals instead
of abstract units.

## Crash recovery (self-stabilizing)

| State at restart | Action |
|---|---|
| QUEUED / PREEMPTED | re-anchor monotonic clocks, resume being placed |
| ADMITTED | **re-queued** (the provision thread died; sandbox-api's idempotent create adopts anything already made) - found by the simulator |
| RUNNING | kept; monitor re-derives truth from sandbox names |
| PREEMPTING | back to RUNNING (checkpoint may exist unused; a later preemption makes a new one) |
| CANCELLING | cancel loop keeps deleting until gone |
| orphan `sched-*` sandboxes | janitor sweep |

## Why loops are thin

Every loop is `sleep; core.<tick>()` with exception handling; generators
(`provision_steps`, `preempt_steps`) are run to completion on worker
threads. The simulator calls the same ticks and advances the same
generators one step at a time, interleaving them with faults - which is
how the ADMITTED-forever and PREEMPTING-after-crash cases were found before
the live battery existed.
