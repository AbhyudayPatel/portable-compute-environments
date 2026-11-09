# sandbox-scheduler v2 - Edge Cases

**VERIFIED** = `scripts/verify.sh` (v1 sections 1-16 re-run on v2, plus
17-20) or the simulator (`../sandbox-sim/sim/sched.py`, `tests/test_sched.py`).

## v1 cases (unchanged contract, re-verified)

| # | Edge case | Handling | Test |
|---|---|---|---|
| 1 | Thundering herd | single admission lock; capacity from reservations + engine truth | verify 5-6 (50 jobs, never > 4 active) |
| 2 | Priority inversion | aging (+1 per 5 s, cap +6) | verify 9 |
| 3 | Starvation | aging + least-served-tenant-first | verify 7 |
| 4 | Crash mid-admission | `reconcile()`: re-anchor clocks; **ADMITTED jobs re-queued** (new); monitor re-derives RUNNING by name; janitor reaps orphans | verify 15; sim `test_random_scheduler_scenarios` (scheduler crashes every run) |
| 5 | Duplicate submission | Idempotency-Key UNIQUE; same body 200, different 409 | verify 4 |
| 6 | Cancel races | QUEUED/PREEMPTED -> CANCELLED at once; ADMITTED/RUNNING/PREEMPTING -> CANCELLING with retried deletes | verify 10-11 |
| 7 | Downstream outage | `UpstreamDown` on every loop; jobs wait; provisioning requeues | verify 14 |
| 8 | Clock skew / TTL | monotonic deadlines + wall re-anchor on boot | verify 13, 15; sim skew fault |
| 9 | Queue bound | 429 past `MAX_QUEUE` | verify 5; sim S6 (depth may transiently exceed by in-flight admissions after a crash re-queue - documented) |
| 10 | Head-of-line blocking | skip-and-fill | verify 8 |

## v2 cases

| # | Edge case | Handling | Test |
|---|---|---|---|
| 11 | **Deadline ordering** | among equal effective priority, earliest `deadline_at` first | verify 17; sim `test_edf_orders_equal_priority_by_deadline` |
| 12 | **Deadline missed** | `_finish` compares `finished_at` with `deadline_at`; flag + counter | verify 18; sim `test_deadline_miss_is_counted` |
| 13 | **Preemption** | head blocked on CPU with effective priority >= 8 -> lowest-priority preemptible+stateful single services checkpointed, deleted, PREEMPTED (+3 bonus); resumed by fork | verify 19; sim `test_preemption_evicts_...` |
| 14 | **Preemption candidate across tenants** | the head is the best job blocked on CPU across all under-cap tenants, not the first tenant's head | sim (found: 0 preemptions in 40 seeds before the fix) |
| 15 | **Non-stateful or task jobs are never victims** | `choose_victims` filters on kind/preemptible/stateful/count | sim `test_non_stateful_jobs_are_never_preempted` |
| 16 | **Cannot free enough** | no victims taken at all (never a partial eviction) | `choose_victims` returns [] |
| 17 | **Checkpoint service down** | preemption aborts, victim back to RUNNING, `checkpoint_down` metric, head keeps waiting | sim `test_preemption_aborts_cleanly_when_checkpoint_service_is_down` |
| 18 | **Crash during preemption** | PREEMPTING -> RUNNING on boot; a later tick preempts again cleanly | sim `test_scheduler_crash_during_preemption_recovers` |
| 19 | **Resume when the engine is down** | provision generator requeues to PREEMPTED (not QUEUED) so the checkpoint is kept | `provision_steps` |
| 20 | **Preempted job cancelled** | PREEMPTED -> CANCELLED directly (no sandbox to tear down) | `cancel()` |
| 21 | **Memory budget** | observed cgroup memory + estimate for the new job > budget -> no admission, `memory_refusals_total` | verify 20 (1-byte budget) |
| 22 | **Budget derived from the engine** | `MEMORY_BUDGET_BYTES` empty -> 75% of engine RAM from `/status` | `engine_view()` |
| 23 | **Gang jobs and preemption** | gangs are never preempted and never preempt partially (count must be 1) | `submit()` forces preemptible=false for count>1 |

## Known limits

- Preemption frees CPU units only; a job blocked by slots/ports/tenant cap
  does not trigger it.
- The memory estimate for unmeasured jobs is a constant per cpu unit;
  real per-template profiles would be better (T03).
- Gang preemption and preemption of non-stateful services (would lose
  state) are deliberately unsupported.
