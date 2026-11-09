"""CLI: fuzz the scheduler core against the sandbox-api core.

    python -m sim.run_sched --seeds 200 --steps 400
    python -m sim.run_sched --seed 7 --steps 400 --verbose
"""
import argparse
import json
import logging
import sys
import time

from .sched import run_sched_seed

logging.getLogger().setLevel(logging.ERROR)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.seed is not None:
        res = run_sched_seed(a.seed, a.steps, a.verbose)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    t0 = time.time()
    fails = []
    tot = {"ops": 0, "submits": 0, "admits": 0, "preempts": 0, "resumes": 0, "crashes": 0}
    for seed in range(a.start, a.start + a.seeds):
        res = run_sched_seed(seed, a.steps)
        for k in tot:
            tot[k] += res.get(k, 0)
        if not res["ok"]:
            fails.append(res)
            print(f"FAIL seed={seed}: {res['error'].splitlines()[0]}")
            if len(fails) >= 5:
                break
    dt = time.time() - t0
    print(f"\n{a.seeds} seeds x {a.steps} steps in {dt:.1f}s: {tot['submits']} submits, "
          f"{tot['admits']} admits, {tot['preempts']} preemptions, {tot['resumes']} resumes, "
          f"{tot['crashes']} scheduler crashes, {len(fails)} failing seeds")
    for f in fails:
        print(f"  replay: python -m sim.run_sched --seed {f['seed']} --steps {a.steps} --verbose")
    if a.json:
        print(json.dumps({"seeds": a.seeds, "steps": a.steps, "seconds": dt,
                          "failures": [f["seed"] for f in fails], **tot}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
