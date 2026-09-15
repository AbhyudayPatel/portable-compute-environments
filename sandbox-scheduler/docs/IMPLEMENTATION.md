# sandbox-scheduler - Implementation Notes

## Files

```
sandbox-scheduler/
+-- docker-compose.yml        one service (scheduler), host-gateway mapping
+-- .env.example              every knob: capacity, caps, aging, cadences
+-- scheduler/
|   +-- Dockerfile            python:3.12-slim + fastapi/uvicorn/requests
|   +-- requirements.txt
|   +-- app/
|       +-- config.py         env-driven config + TEMPLATE_COST + state sets
|       +-- store.py          SQLite jobs/events, idempotency keys, metrics queries
|       +-- upstream.py       sandbox-api client; UpstreamDown / UpstreamConflict
|       +-- scheduler.py      the core: 4 loops, admission, aging, reconcile
|       +-- main.py           FastAPI routes + console mount
|       +-- static/           console SPA (index.html, style.css, app.js)
+-- scripts/  start/stop/reset.ps1, demo.ps1, verify.sh
+-- docs/     ARCHITECTURE, EDGE-CASES, EXAMPLES, SECURITY, this file
```

### scheduler.py - the core, piece by piece

- **`_admission` lock** - every admission decision happens inside it. The
  herd test proves N concurrent submits cannot oversubscribe.
- **`_engine_view()`** - one upstream `/status` + `/sandboxes` call per
  placer tick gives engine truth: total sandboxes (slots), mine vs
  unmanaged, free ports. Scheduler-owned sandboxes are name-correlated
  (`sched-*`).
- **`_fits()`** - all three budgets (cpu units, slots, ports for
  port-needing templates) plus the per-tenant cap.
- **`_select_next()`** - WFQ across tenants (least-served first), then
  within the tenant: effective-priority order with skip-and-fill.
- **`effective_priority()`** - base + `min(cap, age // interval)`, computed
  from a monotonic anchor so wall-clock jumps can't game it.
- **`_admit()` / `_provision()`** - admission writes state first (the
  reservation), provisioning happens on a thread. Upstream 429 mid-gang ->
  rollback all created sandboxes, FAILED. UpstreamDown mid-create ->
  **requeue** to QUEUED (admission was cheap; redo it later).
- **`monitor_loop`** - ADMITTED -> RUNNING when all gang members are READY;
  spawns the task executor; notices vanished sandboxes under RUNNING
  service jobs.
- **`_run_task()`** - execs the command in each sandbox, stores per-member
  results, releases sandboxes, SUCCEEDED iff all exit 0.
- **`janitor_loop`** - expiry via monotonic deadlines; orphan sweep for any
  `sched-*` sandbox without a live job.
- **`cancel_loop`** - the only safe teardown: delete with retry until the
  engine confirms absence, then CANCELLED.
- **`reconcile()`** - on boot, re-anchor monotonic clocks from wall-clock
  timestamps for every live job. The loops do the rest (self-stabilizing).

## Bugs found while building (all fixed, all covered by verify.sh)

1. **Test-side: invalid names.** `aged-B`/`newer-C` failed the name
   validator (uppercase) and `count: 9` exceeded `le: 8` - the tests saw
   empty job ids. Fixed the tests (lowercase names, count 5). The API
   validators did exactly their job.
2. **Queue-bound test was racy.** A fast-draining queue rarely hits 50
   deep, so 429s were nondeterministic. Fix: fill the cluster with blocker
   jobs first, then submit 55 - now exactly 5 get 429 every run.
3. **`grep -c` counts lines, not matches** - the single-line JSON from
   `/jobs` made the fairness assertion always 1. Fixed with `grep -o | wc -l`.
4. **`set -u` vs uninitialized counter** in verify.sh (`N429`). Initialize
   before the loop.
5. **Cancel loop's terminal check was convoluted** (double condition).
   Rewrote as: attempt deletes, re-list, CANCELLED only when truly gone -
   simpler and correct.

## Design decisions worth knowing

- **Why the scheduler doesn't talk to docker directly.** Keeping the docker
  boundary inside sandbox-api means exactly one component owns engine
  semantics (labels, reconciliation, exec quirks). The scheduler is pure
  policy - and replaceable.
- **Why requeue on upstream outage instead of failing.** Admission is a
  promise about the future; a transient outage shouldn't kill queued work.
  Jobs that DID partially create roll back first, so the requeue is clean.
- **Why monotonic + wall dual timestamps.** Monotonic is right for intervals
  but per-process; wall survives restarts. Store both; re-derive on boot.
  (This closes the "TTL slides on restart" limitation documented in T01.)
- **Why name correlation (`sched-<jobid>-<k>`).** After any crash, the
  engine alone tells the truth about what exists. Names are the join key -
  no extra shared state to keep consistent.

## Verified end to end

- [x] `bash scripts/verify.sh` - **41/41**: health, task success/failure +
  output capture, idempotency (replay + conflict), queue bound (5x 429),
  50-job herd drain with concurrency never > 4 and zero leaks, tenant
  fairness, skip-and-fill, aging (aged job beat newer higher-priority job),
  cancel from every state + 409 on terminal, 30x cancel race with zero
  leaks, gang all-or-nothing, max_runtime expiry with teardown, downstream
  outage + recovery, scheduler restart with re-adoption + orphan reaping,
  metrics + prometheus exposition.
- [x] Console: submit / herd / fairness buttons, live queue aging, drawer
  with sandboxes + result + events - all live-polled.
