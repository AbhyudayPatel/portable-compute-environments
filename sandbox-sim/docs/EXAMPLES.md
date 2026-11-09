# sandbox-sim - Exercises

Beginner -> expert. Everything runs without Docker:
`cd sandbox-sim; set PYTHONPATH=..\sandbox-api\api` (PowerShell:
`$env:PYTHONPATH="..\sandbox-api\api"`), or inside the image with
`docker compose run --rm sim <command>`.

## Level 0 - Run it (2 min)

```
python -m sim.run --seeds 50 --steps 200
```
**Observe:** the summary line (ops/s, creates, crashes, failing seeds).
**Teaches:** 10,000 operations with faults in a few seconds.

## Level 1 - Read one run (5 min)

```
python -m sim.run --seed 3 --steps 60 --verbose
```
Every line is `[t=<virtual seconds>] <what happened>`. Find a `CRASH`,
the `restart -> reconcile {...}` right after it, an `ENGINE DOWN` followed
by `create -> 503`, a `FAULT kill` followed later by `informer die`.
**Teaches:** the vocabulary of steps; note that nothing advances time
except `time +N` lines.

## Level 2 - Break the core on purpose (10 min)

Open `sandbox-api/api/app/core.py`, find `self.store.set_port(sid, None)`
in `provision_steps`' failure branch and comment it out. Run:

```
python -m sim.run --seeds 100 --steps 200
```
**Observe:** `FAIL seed=... FAILED sandbox holds a port` and a replay
command. Run the replay with `--verbose`; the log ends at the step that
tripped the invariant. Restore the line.
**Teaches:** a seed is a reproducer; shrinking finds the shortest one.

## Level 3 - Add a fault (15 min)

In `sim/fake_engine.py` add a `slow_ready` knob: `wait_ready` advances the
clock by 60 s when set. In `scenario.py` add a step kind `"slow"` that
sets it (weight 2). Run 200 seeds. **Expected:** nothing fails - TTLs
that expire during a slow create are handled (the reaper skips CREATING,
the TTL fires after READY).

## Level 4 - Add an invariant (15 min)

Add to `invariants.py` an always-check: "a READY sandbox with a port has
that port in the engine port map". Run the fuzzer. Then inject a bug that
forgets the `sandbox.port` label equivalent in the fake
(`engine_port_map` returns `{}`) and confirm your invariant fires.
**Teaches:** every invariant should be paired with a mutation that
proves it can fail.

## Level 5 - Model a new feature (30 min)

Suppose you add "pause/unpause" to the core. Checklist:

1. core: `pause(sid)`/`unpause(sid)` with states `PAUSED`; lock + re-read.
2. fake engine: `pause_sandbox`, emitting the events dockerd emits
   (`pause`, `unpause`); informer handling for them.
3. scenario: two new step kinds; `user_paused` intent set if the stop
   invariant pattern applies.
4. invariants: extend I5 vocabulary; Q2 mapping for PAUSED.
5. a mutation test: informer that treats `pause` as `die`.

Run 500 seeds. Only then write the REST route.

## Level 6 - Expert: statistics and coverage (20 min)

`python -m sim.run --seeds 300 --steps 300 --json | tail -1` prints
totals. Add counters for how many times each invariant *could* have
fired (e.g. how many DEGRADED rows were ever observed) to spot checks
that never exercise. Try `--steps 2000` for long-horizon drift (TTL
re-anchoring across many crashes).
