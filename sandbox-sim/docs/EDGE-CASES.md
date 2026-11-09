# sandbox-sim - Edge Cases (faults, invariants, and proof the harness bites)

The simulator's job is to generate edge cases, not enumerate them. This
file lists the *kinds* of situation it produces, the invariant that would
catch a wrong reaction, and how we know each invariant is live.

## Faults the scenario injects

| Fault | Models | Caught by |
|---|---|---|
| `engine_down` / `engine_up` | dind paused, restarting, unreachable | every op must 503 (`create:503` counts in stats), nothing hangs, no state corruption (Q1 after recovery) |
| `fail_next` (container k of the next create fails) | image missing, start failure mid-template | I1 (FAILED has no port), Q1 (FAILED owns nothing), rollback events present |
| `kill` (optionally OOM) | process crash, OOM killer | DEGRADED transitions; Q2 state vs containers |
| `remove` | `docker rm -f` behind the API's back | DELETED + port freed (Q3) |
| `crash` | `kill -9` of the API process; boot reconcile; boot skipped if engine down | Q1 (orphan CREATING rows), I6, TTL re-anchor (Q4/I8) |
| `skew` | NTP step of the wall clock | reaper unaffected (monotonic); wall-based I8 disabled for the run |
| `time` | 1-30 s passing | TTL expiry paths |
| interleaving | provision steps mixed with every other op | CREATING refuses delete (409), generator aborts if rolled back |

## Scenario tests (deterministic, one situation each)

| Test | Situation | Expectation |
|---|---|---|
| idempotent create | same name + spec twice | second returns the existing row |
| conflict | same name, different spec | 409 |
| port exhaustion | pool of 2, third create | 429, FAILED row has no port |
| partial create failure | container 2 of coreapp fails | FAILED, port freed, no engine resources, error event |
| crash mid-create | resources exist, API dies, boot | rolled back to FAILED, resources gone |
| TTL across restart | 20 s elapsed, crash, new monotonic clock | not reaped early, reaped at 30 s |
| informer degrade + heal | kill backend, deliver `die`, start | DEGRADED with detail, then READY |
| stop then die events | stop, deliver the stop's own `die` | stays STOPPED |
| out-of-band removal | engine loses the sandbox, deliver `destroy` | DELETED, port free |
| reaper vs delete | expired TTL, manual delete first | reaper finds nothing torn; second delete 404 |
| engine down create | engine unreachable | 503 with Retry-After |
| determinism | same seed twice | identical logs |

## Mutation tests (the harness must FAIL when the core is broken)

| Injected bug | Expected detector | Status |
|---|---|---|
| FAILED create keeps its port | I1 / Q3 | VERIFIED (fails within 40 seeds) |
| reconcile ignores vanished rows | Q1 | VERIFIED |
| no TTL re-anchor after restart (v1 behaviour) | I8 (wall-based) | VERIFIED |
| informer flips STOPPED to DEGRADED on stop's `die` | I7 | VERIFIED (after the fake engine learned to emit `die` on stop) |

## Known blind spots (deliberate)

- Thread-level races inside one engine call (covered by the live battery).
- Agent/proxy behaviour (covered by the standalone agent tests and
  verify.sh sections 5-8).
- Real dockerd event ordering/dropping (the fake queues events in order;
  reality may reorder - the periodic resync is the backstop in both).
- SQLite file durability (in-memory in the sim).
