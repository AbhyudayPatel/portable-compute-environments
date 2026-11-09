"""CLI: run N seeds, report, shrink failures.

    python -m sim.run --seeds 500 --steps 300
    python -m sim.run --seed 42 --steps 300 --verbose     # replay one
"""
import argparse
import json
import logging
import sys
import time

from .scenario import run_seed, shrink

# the core logs expected conditions (engine down, reaper retry) at WARNING;
# in a fault-injecting simulator that is noise
logging.getLogger().setLevel(logging.ERROR)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--seed", type=int, default=None, help="replay a single seed")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.seed is not None:
        res = run_seed(a.seed, a.steps, a.verbose)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1

    t0 = time.time()
    failures = []
    totals = {"ops": 0, "crashes": 0, "created": 0, "deleted": 0}
    for seed in range(a.start, a.start + a.seeds):
        res = run_seed(seed, a.steps)
        for k in totals:
            totals[k] += res.get(k, 0)
        if not res["ok"]:
            failures.append(res)
            print(f"FAIL seed={seed}: {res['error'].splitlines()[0]}")
            if len(failures) >= 5:
                break
    dt = time.time() - t0
    print(f"\n{a.seeds} seeds x {a.steps} steps in {dt:.1f}s "
          f"({totals['ops'] / max(dt, 1e-9):.0f} ops/s): "
          f"{totals['created']} creates, {totals['deleted']} deletes, "
          f"{totals['crashes']} crashes, {len(failures)} failing seeds")
    for f in failures:
        n = shrink(f["seed"], a.steps)
        print(f"  seed {f['seed']} fails within {n} steps; replay: "
              f"python -m sim.run --seed {f['seed']} --steps {n} --verbose")
    if a.json:
        print(json.dumps({"seeds": a.seeds, "steps": a.steps, "seconds": dt,
                          "failures": [f["seed"] for f in failures], **totals}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
