# sandbox-sim - Deterministic Simulation Testing of the control plane

> Run the sandbox-api lifecycle core **thousands of times per minute**
> against a fake engine and a virtual clock, with faults injected from a
> seed - engine outages, container deaths, out-of-band removals, partial
> creates, process crashes, clock skew - and check invariants after every
> step. A failure is a seed; a seed replays identically; the harness
> shrinks it to the shortest failing prefix. No Docker engine is touched.

```
   seed  --> random.Random --> step kinds: create / delete / stop / start /
                               provision-step / reaper / informer-event /
                               resync / time / engine_down / engine_up /
                               kill / remove / fail_next / crash / skew
                                       |
                                       v
                    +-------- Core (sandbox-api/api/app/core.py) --------+
                    |  the SAME code the real API runs                  |
                    |  store = SQLite in memory   engine = FakeEngine     |
                    |  clock = VirtualClock       ids = seeded counter   |
                    +----------------------------------------------------+
                                       |
                      after EVERY step: invariants (I1..I8), and at
                      quiescence: engine == DB (Q1..Q4)
```

This is the technique TigerBeetle, FoundationDB and Antithesis use. It is
why the core was rewritten without threads, sleeps, or direct clock reads
(see `sandbox-api/docs/ARCHITECTURE.md`): anything deterministic can be
simulated; anything simulated can be replayed.

## Two cores, one seed

The harness now drives **both** control planes together:

```
scriptsun.ps1                                  # sandbox-api core alone (sim/run.py)
PYTHONPATH=../sandbox-api/api python -m sim.run_sched --seeds 200   # scheduler core ON TOP of the api core (sim/sched.py)
```

`sim/sched.py` wires sandbox-scheduler's `Core` to the sandbox-api `Core`
through an in-process adapter (`SimUpstream`) plus a fake checkpoint
service, so admission, fairness, aging, EDF, preemption (checkpoint ->
delete -> resume by fork), memory budget, cancellation and crash recovery
are fuzzed against a sandbox-api that is itself being fuzzed (engine
outages, container deaths, out-of-band removals, API crashes).

## Quick start

```powershell
scripts\run.ps1                    # 300 seeds x 300 steps (~2 min)
scripts\run.ps1 -Seeds 2000        # more
scripts\run.ps1 -Seed 42 -Verbose  # replay one seed step by step
scripts\run.ps1 -Pytest            # 76 tests: 60 seeds, 12 scenarios, 4 mutation tests
bash scripts\verify.sh             # the whole battery (build, pytest, 300-seed fuzz, determinism)
```

Without Docker (needs python 3.12+):

```bash
cd sandbox-sim
PYTHONPATH=../sandbox-api/api python -m sim.run --seeds 500 --steps 300
```

Output of a run:

```
500 seeds x 300 steps in 215.4s (696 ops/s): 8062 creates, 4059 deletes, 3068 crashes, 0 failing seeds
```

and when something breaks:

```
FAIL seed=20: seed=20 step=82: DELETED row still has engine resources
  seed 20 fails within 83 steps; replay: python -m sim.run --seed 20 --steps 83 --verbose
```

## What it found (real bugs in sandbox-api, before any bash test did)

| # | Bug | Symptom in the sim | Fix |
|---|---|---|---|
| 1 | API boots while the engine is down -> boot reconcile skipped -> rows left CREATING by a crash are never cleaned (periodic resync skips CREATING on purpose) | `engine/DB divergence at quiescence`, 5 of 30 seeds | `boot_pending`: the first resync that reaches the engine runs in boot mode |
| 2 | A boot-mode reconcile rolls back a create that is legitimately in flight; the orphaned generator keeps creating containers for a FAILED/DELETED row | `DELETED row still has engine resources`, seed 20 | `core.in_flight` ownership + the generator re-checks its row between steps |
| 3 | FAILED rows keep engine resources if the engine was down during rollback | Q1 "FAILED owns nothing" | reconcile removes leftovers of FAILED rows |
| 4 | Capacity cap counted engine sandboxes only, so N creates passing the check together overshoot `MAX_SANDBOXES` | `engine over capacity`, seed 199 of 500 | count CREATING rows too |

Found in the scheduler core by `sim/sched.py`:

| # | Bug | Symptom | Fix |
|---|---|---|---|
| 5 | ADMITTED jobs whose provision thread died with the process were never re-provisioned (v1 "self-stabilizing" claim had a hole) | `non-quiescent job state at quiescence` | `reconcile()` re-queues ADMITTED jobs |
| 6 | Preemption candidate was the head of the least-served tenant, so a priority-9 job in another tenant never triggered preemption | 0 preemptions in 40 seeds | candidate = best job blocked on CPU across tenants |

And one the *live* battery found that the sim did not model (a 20 ms gap
between row insert and thread start) - see `sandbox-api/docs/IMPLEMENTATION.md`
bug 8. The two styles are complementary; keep both.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the harness: ports, fake
  engine, scenario driver, invariants, shrinking, determinism.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - every fault and every
  invariant, and the mutation tests that prove the harness has teeth.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files, the refactor
  that made the core simulable, bugs in the harness itself.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - exercises: replay, shrink, add a
  fault, add an invariant, inject a bug and watch it get caught.
- [`docs/SECURITY.md`](docs/SECURITY.md) - what the simulator does and does
  not prove.
