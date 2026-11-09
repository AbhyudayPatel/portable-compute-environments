# sandbox-scheduler v2 - Practice Exercises

Prereqs: sandbox-api up, scheduler up (and sandbox-blobs + sandbox-checkpoint
for the preemption exercise). Console: http://localhost:9010.

## Level 0 - Console tour (5 min)

Submit one task job (`echo hello; sleep 3`): QUEUED -> ADMITTED -> RUNNING
-> SUCCEEDED, output in the drawer, sandbox gone. Submit a service job
(template web): RUNNING with a URL chip; cancel it. Watch the capacity
bars and the queue's **+aged** column.

## Level 1 - Fairness and aging (10 min)

Press **herd x12** and **fairness demo**. Read
`docs/ARCHITECTURE.md` "the placement tick" while the queue drains and
map each rule to what you see.

## Level 2 - Deadlines (10 min)

Fill the cluster with four services (`max_runtime: 300`). Submit two
priority-5 tasks: one with `deadline_seconds: 600`, one with `30`.
`GET /queue`: the 30-second one is first even though it arrived later.
Cancel one blocker: it runs first. Then submit a task with
`deadline_seconds: 2` and `cmd: sleep 8`: SUCCEEDED, but
`deadline_missed: true` and `deadline_missed_total` in `/metrics` grew.

## Level 3 - Preemption (15 min)

```bash
for i in 1 2 3; do curl -s -X POST localhost:9010/jobs -H 'content-type: application/json' \
  -d "{\"tenant\":\"low\",\"template\":\"blank\",\"kind\":\"service\",\"name\":\"low-$i\",\"priority\":1,\"stateful\":true,\"preemptible\":true,\"max_runtime\":600}"; done
curl -s -X POST localhost:9010/jobs -H 'content-type: application/json' \
  -d '{"tenant":"low2","template":"blank","kind":"service","name":"low-4","priority":1,"stateful":true,"preemptible":true,"max_runtime":600}'
```
Four units reserved. Now a VIP:
```bash
curl -s -X POST localhost:9010/jobs -H 'content-type: application/json' \
  -d '{"tenant":"vip","template":"blank","kind":"task","cmd":["sh","-c","echo vip; sleep 5"],"priority":9}'
```
**Observe:** one low job goes PREEMPTING -> PREEMPTED (events: checkpoint
taken), the VIP runs, and after it finishes the victim comes back RUNNING
with "forked from checkpoint" in its events, on a new sandbox. Write a
file into a victim's `/workspace` before the VIP arrives and read it
after the resume. **Teaches:** preemption without data loss is a
checkpoint plus a fork.

## Level 4 - When preemption refuses (5 min)

Repeat with `stateful: false` services: the VIP waits, `preemptions_total`
stays 0. Stop sandbox-checkpoint and try again: the victim is chosen,
the checkpoint fails, the job returns to RUNNING with "preemption aborted"
and `checkpoint_down: true` appears in `/status`.

## Level 5 - Memory budget (5 min)

Set `MEMORY_BUDGET_BYTES=1` in `.env`, restart: every submission waits
and `memory_refusals_total` climbs. Remove it: the default is 75% of the
engine's RAM; `/status` shows `memory_budget` and `memory_observed`
(sampled from each running sandbox's cgroup through the agent).

## Level 6 - Expert: simulate before you ship (15 min)

```
cd ../sandbox-sim
PYTHONPATH=../sandbox-api/api python -m sim.run_sched --seeds 300 --steps 300
PYTHONPATH=../sandbox-api/api python -m sim.run_sched --seed 7 --steps 200 --verbose
```
Change a policy knob in `scheduler/app/config.py` (say `PREEMPT_PRIORITY=5`)
and re-run: do invariants still hold? Add an invariant of your own
(e.g. "a preempted job is resumed within N placer ticks of free capacity")
and see what the fuzzer says.
