# sandbox-scheduler v2 - Implementation Notes

## Files

```
sandbox-scheduler/
+-- docker-compose.yml        one service; SANDBOX_API_URL, CHECKPOINT_URL, PREEMPT_*, MEMORY_* knobs
+-- .env.example              every knob documented
+-- scheduler/
|   +-- Dockerfile            python:3.12-slim + fastapi/uvicorn/requests
|   +-- app/
|       +-- clock.py          injectable clock (virtual in the simulator)
|       +-- config.py         capacity, aging, preemption, memory budget, cadences, TEMPLATE_COST
|       +-- store.py          SQLite (class-based): jobs with v2 columns, events, counters
|       +-- upstream.py       sandbox-api client + checkpoint client (create/fork), memory sampling
|       +-- core.py           THE policy: placer/monitor/janitor/cancel ticks, provision + preempt generators,
|                             submit, cancel, reconcile - no threads, no HTTP, no clock
|       +-- main.py           routes + loops + worker threads driving the core
|       +-- static/           console (queue with deadlines/states, jobs with v2 facts)
+-- scripts/  start/stop/reset.ps1, demo.ps1, verify.sh (v1 16 sections + v2 17-20)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

The scheduler is also imported by `../sandbox-sim/sim/sched.py` (as
`sched_app`) and fuzzed against the sandbox-api core.

## The v2 refactor

v1 was `scheduler.py`: four `while True` loops, module-level store and
upstream, `time.*` everywhere, threads spawned from inside the policy.
v2 splits it exactly like sandbox-api:

- `core.py` holds every decision and transition; each loop body is one
  method (`placer_tick`, `monitor_tick`, `janitor_tick`, `cancel_tick`);
  long operations are generators (`provision_steps`, `preempt_steps`).
- `main.py` owns threads and HTTP; it never decides anything.
- `clock.py` and `Store(db_path)` make time and storage injectable.
- `upstream.py` is a class with `Down`/`Conflict` exception attributes so
  the simulator can substitute an adapter with the same contract.

## New behaviour, where it lives

| Feature | Code |
|---|---|
| EDF | `select_next` sort key `(-effective_priority, deadline_at, created_at)`; `_finish` counts misses |
| preemption | `placer_tick` -> `select_next` returns the best job blocked on CPU -> `choose_victims` -> `PREEMPTING`; `preempt_steps` checkpoints via `upstream.checkpoint_create`, deletes, sets PREEMPTED (+resume bonus in `effective_priority`); `provision_steps` forks when `checkpoint_id` is set |
| memory budget | `monitor_tick` samples `upstream.sandbox_memory` (agent cgroup stats) into `mem_bytes`; `_fits` compares observed + estimate with `engine_view()["memory_budget"]` |
| crash recovery | `reconcile`: re-anchor clocks, PREEMPTING -> RUNNING, **ADMITTED -> re-queued** |

## Bugs found while building

1. **(simulator) ADMITTED forever.** A job whose provision thread died
   with the process stayed ADMITTED with no sandboxes; v1's "the monitor
   re-derives truth by name" only helps when something was created.
   `reconcile()` now re-queues ADMITTED jobs (sandbox-api's idempotent
   create adopts any sandbox that did get created under the same name).
2. **(simulator) Preemption never triggered.** The candidate was the
   first non-fitting job of the *least-served tenant*, so a priority-9 job
   elsewhere never counted. Now the candidate is the highest effective
   priority job blocked specifically on CPU across all under-cap tenants.
3. **(simulator harness) Cancel stuck on an orphaned CREATING sandbox.**
   An API crash while the engine was down left a sandbox CREATING;
   sandbox-api's periodic resync (which rolls it back) was not being run
   inside the scheduler simulation's quiescence loop, so `cancel_tick`
   retried a 409 forever. The harness now runs the api resync in the
   loop, mirroring production.
4. **(simulator harness) Queue bound after a crash.** Re-queued ADMITTED
   jobs can push the depth past `MAX_QUEUE`; the invariant now allows
   the in-flight admission count on top of the bound (and the behaviour
   is documented as intended: dropping work is worse than a transient
   overshoot of the submit bound).

## Verified

- [x] simulator: `python -m sim.run_sched --seeds 150 --steps 300` ->
      0 failing seeds (4430 submits, 827 scheduler crashes); `pytest
      tests/test_sched.py` covers preemption, non-preemption of
      non-stateful jobs, EDF, deadline misses, checkpoint-service outage,
      crash during preemption.
- [x] `bash scripts/verify.sh` - see the RESULT line of the latest run
      (v1 sections + EDF ordering, deadline-miss metric, live preemption
      with resume by fork, memory-budget refusal).
