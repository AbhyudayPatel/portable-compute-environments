# sandbox-scheduler v2 - T02: Concurrent Sandbox Scheduler with preemption

> A queue + placement engine on top of the T01 sandbox-api. Submit many
> sandbox jobs at once; the scheduler **admits what fits, queues the rest
> fairly, ages them so nothing starves, meets deadlines first, preempts
> low-priority stateful services for urgent work (checkpoint, evict,
> resume by fork), and refuses admission when memory would run out** -
> all under hard capacity limits, all recoverable after a crash, and all
> of it **simulation-tested** against the sandbox-api core without Docker.

```
you --> scheduler :9010  (queue, fair admission, aging, EDF, preemption, janitor)
            |  HTTP                 |  HTTP (preemption only)
            v                       v
        sandbox-api :9000       sandbox-checkpoint :9040
            |                       (checkpoint victim, fork to resume)
            v
          dind --> the actual sandboxes
```

The scheduler owns **no docker engine**: it is pure policy. That is what
made it simulable - the same `core.py` runs in `../sandbox-sim` against the
sandbox-api core with a virtual clock (`sim/sched.py`).

## Quick start (Windows)

```powershell
cd ..\sandbox-api;        powershell -ExecutionPolicy Bypass -File scripts\start.ps1
cd ..\sandbox-blobs;      powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # for checkpoints
cd ..\sandbox-checkpoint; powershell -ExecutionPolicy Bypass -File scripts\start.ps1   # for preemption
cd ..\sandbox-scheduler;  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Console: http://localhost:9010 (capacity bars, the queue with live aging,
jobs table, submit form, herd/fairness buttons). API docs at `/docs`.

```
bash scripts/verify.sh        # v1 battery (16 sections) + v2 sections 17-20
```

## Job fields

```json
{
  "tenant": "alice",            // fairness unit + noisy-neighbour cap (default 3)
  "template": "blank",          // blank | web | coreapp
  "kind": "task",               // task (runs cmd, then releases) | service (lives until cancel/max_runtime)
  "cmd": ["sh","-c","..."],     // required for kind=task
  "priority": 3,                // 0..9; effective = base + aging (+ resume bonus)
  "count": 1,                   // gang size, all-or-nothing
  "cpu_units": 1,               // abstract capacity units (template default)
  "max_runtime": 60,            // seconds; null = until cancelled
  "deadline_seconds": 30,       // v2: EDF ordering among equal priority; misses counted
  "stateful": true,             // v2: overlay-backed data paths (sandbox-api options.stateful)
  "preemptible": true           // v2: default true for services, false for tasks; needs stateful
}
```

## Scheduling policy (what "best" means here)

1. **Capacity** - cpu units AND sandbox slots AND app ports AND tenant cap
   AND (v2) memory budget, all under one admission lock.
2. **Fair queueing** - tenants take turns, least-served first.
3. **Aging** - +1 effective priority every 5 s queued (cap +6).
4. **EDF** (v2) - among equal effective priority, earliest deadline first.
5. **Skip-and-fill** - a big job that does not fit is skipped; smaller ones admit.
6. **Gang admission** - `count: N` all-or-nothing with rollback.
7. **Preemption** (v2) - a queued job with effective priority >=
   `PREEMPT_PRIORITY` (8) that is blocked on CPU evicts the lowest-priority
   RUNNING services that are `preemptible` AND `stateful` AND single:
   checkpoint (sandbox-checkpoint) -> delete sandbox -> `PREEMPTED`
   (queued with +3 resume bonus) -> later admitted by **forking** the
   checkpoint, so the victim resumes with its data intact.
8. **Memory budget** (v2) - the monitor samples each running sandbox's
   cgroup memory through the agent; admission refuses when observed +
   256 MiB per new cpu unit would exceed the budget (default 75% of the
   engine's RAM). Refusals are counted.
9. **Janitor** - max-runtime expiry on monotonic deadlines, orphan sweep.

## States

```
QUEUED -> ADMITTED -> RUNNING -> SUCCEEDED | FAILED | EXPIRED
RUNNING -> PREEMPTING -> PREEMPTED -> ADMITTED (resume = fork from checkpoint)
any non-terminal -> CANCELLING -> CANCELLED
```

## API additions in v2

| Method | Path | New |
|---|---|---|
| POST | `/jobs` | `deadline_seconds`, `stateful`, `preemptible` |
| GET | `/jobs[/{id}]` | `deadline_at`, `deadline_missed`, `checkpoint_id`, `preempt_count`, `mem_bytes` |
| GET | `/queue` | includes `PREEMPTED` jobs, `deadline_in`, `checkpoint_id` |
| GET | `/status` | `capacity.memory_budget/memory_observed`, `preemption` counters |
| GET | `/metrics` | `preemptions_total`, `resumes_total`, `deadline_missed_total`, `memory_refusals_total`, `checkpoint_down` |
| GET | `/healthz` | `checkpoint` reachability |

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the core/driver split, loops, policy, preemption protocol, memory budget, crash recovery.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - v1 cases re-verified + v2 cases, each mapped to verify.sh or the simulator.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files, the v2 refactor, bugs found (two by the simulator).
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - exercises incl. preemption and deadlines.
- [`docs/SECURITY.md`](docs/SECURITY.md).
