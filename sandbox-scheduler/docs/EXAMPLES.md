# sandbox-scheduler - Practice Exercises

Ordered beginner -> expert. Each exercise says what to do, what to
**observe**, and what it teaches. Prereqs: both stacks running
(`sandbox-api` then `sandbox-scheduler`), scheduler console open at
**http://localhost:9010**.

---

## Level 0 - Console tour (5 min)

0.1 **Read the "How scheduling works" strip.** The five boxes (you ->
queue -> admission -> sandbox-api -> sandboxes) are the whole system.

0.2 **Submit one task job** from the form (template blank, kind task,
cmd `echo hello; sleep 3`). **Observe:** it appears QUEUED, flashes
ADMITTED, goes RUNNING, then SUCCEEDED - click its row to see the captured
stdout in the result panel. The sandbox lived for seconds and was deleted
for you.

0.3 **Submit one service job** (kind service, template web). **Observe:**
it reaches RUNNING and *stays*; its sandbox chip shows a URL you can open.
Cancel it from the table; watch it go CANCELLING -> CANCELLED.

0.4 **Watch the capacity bar** while jobs run: cpu units fill as jobs
admit, drain as they finish. The queue table's **+aged** column climbs on
waiting jobs - aging, live.

---

## Level 1 - Submit & inspect (10 min)

1.1 **Task vs service.** Run the same command as both kinds
(`echo hi`). Task: SUCCEEDED with output; service: RUNNING forever.
**Teaches:** the two fundamental workload shapes.

1.2 **Gang jobs.** Submit `count: 3`, kind task, cmd `hostname`.
**Observe:** one job, three sandboxes (`sched-<id>-0/1/2`), result has
three outputs. Then submit `count: 5` (over capacity 4): it sits QUEUED
and **zero** sandboxes appear in the sandbox-api console - all-or-nothing.
Cancel it. **Teaches:** gang admission + rollback.

1.3 **Idempotent submit.** Submit twice with the same
`Idempotency-Key: my-key-1` header (use curl). **Observe:** same job id
back, HTTP 200 the second time. Change the body with the same key -> 409.
**Teaches:** safe client retries.

1.4 **Priority.** Fill the cluster (4x service jobs cpu 1), then submit a
prio-0 job and a prio-9 job. **Observe:** when a slot frees, prio-9 goes
first. **Teaches:** base priority ordering.

---

## Level 2 - Fairness & aging (15 min)

2.1 **Noisy neighbor.** As tenant `loud`, submit 6 service jobs
(max_runtime 300). **Observe:** only 3 run - `MAX_PER_TENANT=3` caps them;
the rest queue. As tenant `small`, submit 1 service job. **Observe:** it
runs *immediately*, ahead of loud's queue. **Teaches:** per-tenant caps +
fair queueing. (One click: the "fairness demo" button.)

2.2 **Aging beats priority.** Keep the cluster full. Submit a prio-1 job
named `old`. Watch the queue table: its **+aged** climbs +1 every 5 s.
After ~15 s submit a prio-4 job named `new`. **Observe:** when capacity
frees, `old` (effective 1+3=4, then 5...) wins over `new` once its bonus
passes 3. **Teaches:** bounded wait - starvation is impossible.

2.3 **Skip-and-fill.** Submit service job cpu=3 (runs). Submit cpu=2
(queues - only 1 free). Submit cpu=1. **Observe:** the cpu=1 job runs
while cpu=2 waits. **Teaches:** no head-of-line blocking.

2.4 **Cancel everywhere.** Cancel a QUEUED job (instant CANCELLED), a
RUNNING job (CANCELLING -> CANCELLED, sandbox disappears from the
sandbox-api console), and a SUCCEEDED job (409). **Teaches:** cancel
semantics per state.

---

## Level 3 - Failure drills (15 min)

3.1 **Expiry.** Service job with `max_runtime: 20`. **Observe:** RUNNING
-> EXPIRED at ~20 s; its sandbox is deleted by the janitor.
**Teaches:** bounded lifetimes.

3.2 **Upstream outage.** `docker stop sandbox-api-api-1`. Submit a job.
**Observe:** it stays QUEUED; `/metrics` shows `upstream_down: true`; the
console's upstream pill goes red - nothing errors, nothing spins. `docker
start sandbox-api-api-1`; the job admits within seconds.
**Teaches:** honest degradation + automatic recovery.

3.3 **Kill the scheduler mid-flight.** Start 3 service jobs, wait for
RUNNING, then `docker restart sandbox-scheduler-scheduler-1`. **Observe:**
jobs still RUNNING afterwards; click one - a `reconcile` event records the
restart; sandbox count in the sandbox-api console is unchanged (no
duplicates). **Teaches:** crash recovery via name correlation.

3.4 **Orphan sweep.** Create a fake scheduler sandbox by hand:
`curl -X POST localhost:9000/sandboxes -d '{"name":"sched-fake123-0","template":"blank"}' -H 'content-type: application/json'`
**Observe:** within ~2 janitor ticks it is deleted - no live job owns it.
**Teaches:** the engine is reconciled against the DB continuously.

3.5 **Queue bound.** With the cluster blocked, fire 55 submits
(herd button x5). **Observe:** 50 accept, 5 get 429 Retry-After.
**Teaches:** bounded memory, honest overload signal.

---

## Level 4 - Metrics & observability (5 min)

4.1 `curl localhost:9010/metrics` - find wait p50/p95/p99 growing as you
run herds. 4.2 `curl localhost:9010/metrics/prometheus` - the same for a
Prometheus scrape. 4.3 Compare `wait_seconds` across priorities after a
herd: high-priority jobs should show lower waits in their events.

---

## Level 5 - Design questions (free play)

- Where would preemption go? (janitor needs a safe pause - see T05
  checkpoints.)
- The cluster model is abstract units - what breaks when tenants learn the
  mapping? (T03 turns units into cgroup limits.)
- Two scheduler replicas would double-place: what lock would you need?
  (T12 answers with Postgres advisory locks + fencing.)
