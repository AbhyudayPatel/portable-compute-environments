# sandbox-scheduler - T02: Concurrent Sandbox Scheduler

> A queue + placement engine on top of the T01 sandbox-api. Submit many
> sandbox jobs at once; the scheduler **admits what fits, queues the rest
> fairly, ages them so nothing starves, and reaps what misbehaves** -
> all under hard capacity limits, all recoverable after a crash.

```
you --> scheduler :9010  (queue, fair admission, aging, janitor)
            |
            | HTTP
            v
        sandbox-api :9000 (T01 - owns the docker engine)
            |
            v
          dind --> the actual sandboxes
```

The scheduler owns **no docker engine**. It is a pure control plane: it
decides *which* sandbox requests get created *when*, and the sandbox-api
decides *how*. That separation is the point of the exercise.

## Quick start (Windows)

```powershell
# 1. PREREQUISITE - the sandbox-api stack must be running:
cd ..\sandbox-api
powershell -ExecutionPolicy Bypass -File scripts\start.ps1

# 2. start the scheduler:
cd ..\sandbox-scheduler
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Then open the console:

### http://localhost:9010

The console shows, live (2 s refresh):
- **capacity bars** - cpu units / sandbox slots / free app ports
- **the queue** - queued jobs with their *effective priority* and the
  **aging bonus ticking up in front of you**
- **the jobs table** - every job's state, wait time, sandboxes
- **submit form** - tenant, template, kind, priority, gang count, runtime
- **herd x12 / fairness demo buttons** - one click load tests
- **metrics** - wait p50/p95/p99, throughput, upstream health

## The two kinds of jobs

| kind | lifecycle | use |
|---|---|---|
| `task` | sandbox created, `cmd` runs inside, result captured, sandbox deleted | batch work, CI-style runs |
| `service` | sandbox created and kept until cancel/max_runtime | preview envs, long-running apps |

## All functions (API)

| Method | Path | What it does |
|---|---|---|
| GET | `/healthz` | scheduler + upstream liveness |
| GET | `/status` | capacity, queue depth, per-tenant usage |
| POST | `/jobs` | submit (202; `Idempotency-Key` header for safe retries) |
| GET | `/jobs` | live jobs + recent terminal ones (sandboxes joined) |
| GET | `/jobs/{id}` | one job incl. task result |
| POST | `/jobs/{id}/cancel` | QUEUED -> CANCELLED; RUNNING -> torn down; terminal -> 409 |
| GET | `/jobs/{id}/events` | ordered per-job event log |
| GET | `/queue` | live queue sorted by effective priority |
| GET | `/metrics` | JSON: wait p50/p95/p99, throughput, state counts |
| GET | `/metrics/prometheus` | Prometheus text exposition |
| POST | `/admin/reset` | wipe job DB (demo/testing) |

### Job fields

```json
{
  "tenant": "alice",        // fairness unit + noisy-neighbor cap (default 3)
  "template": "blank",      // blank | web | coreapp (sandbox-api templates)
  "kind": "task",           // task | service
  "cmd": ["sh","-c","..."], // required for kind=task
  "priority": 3,            // 0..9, effective = base + aging bonus
  "count": 1,               // gang size - all-or-nothing admission
  "cpu_units": 1,           // abstract capacity units (template default)
  "max_runtime": 60         // seconds; null = live until cancelled
}
```

## Scheduling policy (what "best" means here)

1. **Capacity** - a job admits only if it fits free cpu units AND free
   sandbox slots AND free app ports (port-needing templates) AND its
   tenant is under the per-tenant cap. All under one admission lock, so
   herds cannot oversubscribe.
2. **Fair queueing** - tenants take turns, least-served first.
3. **Aging** - +1 effective priority every 5 s queued (cap +6): bounded
   wait, starvation impossible.
4. **Skip-and-fill** - a big job that doesn't fit is skipped; smaller jobs
   behind it still admit.
5. **Gang admission** - `count: N` is all-or-nothing; partial failure rolls
   back every created sandbox.
6. **Janitor** - max-runtime expiry on monotonic deadlines, plus an orphan
   sweeper that reaps any `sched-*` sandbox with no live job.

## Learn it

- **Guided demo:** `powershell -ExecutionPolicy Bypass -File scripts\demo.ps1`
- **Edge-case battery:** `bash scripts/verify.sh` (41 assertions)
- **Practice exercises:** [docs/EXAMPLES.md](docs/EXAMPLES.md)
- **How it works:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Edge cases:** [docs/EDGE-CASES.md](docs/EDGE-CASES.md)
- **Build notes + bugs found:** [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)
- **Security posture:** [docs/SECURITY.md](docs/SECURITY.md)

## Reset

```powershell
scripts\stop.ps1     # keep job DB
scripts\reset.ps1    # wipe job DB (orphaned sandboxes get reaped on boot)
```
