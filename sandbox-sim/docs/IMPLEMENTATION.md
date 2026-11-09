# sandbox-sim - Implementation Notes

## Files

```
sandbox-sim/
+-- docker-compose.yml     one service `sim`; additional build context `app` = ../sandbox-api/api/app
+-- Dockerfile             python:3.12-slim + pytest; copies the real core package + sim/ + tests/
+-- sim/
|   +-- vclock.py          VirtualClock: advance(), skew_wall(), sleep() records and advances
|   +-- fake_engine.py     FakeEngine: the DockerEngine surface + fault knobs + event queue
|   +-- scenario.py        Scenario (seeded step driver), SimApi (REST-shaped facade), run_seed, shrink
|   +-- invariants.py      I1..I8 always, Q1..Q4 at quiescence; mutating TTL checks last
|   +-- run.py             CLI: --seeds/--steps/--start, --seed --verbose replay, --json, shrinking
+-- tests/
|   +-- test_invariants.py     60 seeds + 12 scenario tests + determinism
|   +-- test_bug_injection.py  4 mutation tests (the harness must catch injected bugs)
+-- scripts/  run.ps1, verify.ps1, verify.sh
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

## What had to change in sandbox-api to make this possible

The v1 control plane could not be simulated: routes did engine calls
inline, loops were `while True: time.sleep()` threads, every timestamp
came straight from `time`, ids from `uuid4`, and the store was a module
with a hard-coded path. The v2 refactor (all in `sandbox-api/api/app`):

1. **`core.py`** - every state transition in one class with injected
   `store` and `engine`. Routes became one-line adapters.
2. **`clock.py`** - one module-level `impl` the simulator replaces.
   `store.py` and `engine.py` read time through it too.
3. **Provisioning as a generator** (`provision_steps`) - the only place
   v1 needed a thread. Production still runs it in a thread; the thread
   just iterates the generator.
4. **`Store(db_path)`** class - a shared-cache in-memory SQLite per
   scenario; a "keeper" connection keeps it alive.
5. **Injectable ids/tokens** - `Core(id_factory=, token_factory=)`.
6. **`in_flight` / `boot_pending`** - state that used to be implicit in
   "which threads exist" became explicit, which is what let the simulator
   reason about crashes (and find the bugs listed in the README).

The informer, reaper and resync loops in `informer.py` are thin: each
sleeps through the clock and calls one core method - so the scenario can
call the same methods directly as steps.

## Bugs found in the harness itself (worth knowing when you extend it)

1. **Non-determinism from ids.** The first failing seed did not reproduce:
   `uuid4` ids and `ORDER BY created_at, id` with virtual-time ties made
   `random.choice` pick different rows per run. Fix: seeded id factory.
2. **Mutating invariants.** A TTL check that called `reap_expired()`
   while a later check still used the pre-reap row snapshot produced a
   false "engine/DB divergence". Mutating checks now run last on a fresh
   snapshot (`_ttl_checks`).
3. **Fake engine fidelity.** `stop` did not emit `die` events, so the
   mutation test for the stop/informer race could not fail. The fake now
   mirrors dockerd. Lesson: a mutation test that stops failing is a
   coverage regression alarm.
4. **Over-strict invariant.** "DEGRADED must have a running container"
   was wrong for single-container sandboxes whose only container died
   (DEGRADED is correct there). Replaced by the user-intent invariant I7.
5. **Test fixtures vs validation.** Scenario tests used names like `"a"`;
   the real validator wants at least two characters. The validator was
   right.
6. **Logging noise.** The core logs engine-down retries at WARNING; the
   fuzzer silences the root logger to ERROR.

## Performance

About 600-700 steps per second on one core (SQLite in memory). 500 seeds x
300 steps = 150,000 steps in ~215 s. Thousands of seeds are a coffee
break; a nightly run of 20,000 seeds is feasible.

## Verified

- [x] `pytest -q` inside the image: **76 passed**
- [x] `python -m sim.run --seeds 500 --steps 300`: **0 failing seeds**
      after the four core fixes (and failing seeds before each of them -
      see README table)
- [x] determinism: same seed -> identical log (`scripts/verify.sh` step 3)
