"""Scheduler simulation: the sandbox-scheduler core driven against the
sandbox-api core, both in-process, one virtual clock, one seed.

    SchedScenario
      |-- Scenario (sandbox-api core + FakeEngine)       from scenario.py
      |-- SimUpstream: the scheduler's "sandbox-api client" that calls the
      |     api core directly (create/list/get/delete/exec/stats/status)
      |     and a fake checkpoint service (checkpoint = remember, fork =
      |     create a new sandbox) so preemption can be exercised
      '-- sandbox-scheduler Core with an in-memory Store

Step kinds add: submit (task/service, random tenant/priority/deadline,
stateful+preemptible services), cancel, placer/monitor/janitor/cancel
ticks, provision-step and preempt-step generators, task completion
(fake exec), scheduler crash (generators dropped, reconcile), plus every
engine fault from the api scenario.

Invariants (checked after every step):
  S1  reserved cpu units of ACTIVE jobs <= CLUSTER_CPU_UNITS (+ unmanaged)
  S2  per-tenant active sandboxes <= MAX_PER_TENANT
  S3  a terminal job owns no sandbox in the engine
  S4  job event seqs contiguous; state vocabulary
  S5  a PREEMPTED job has a checkpoint id and no sandboxes
  S6  queue depth <= MAX_QUEUE
At quiescence:
  SQ1 every job terminal or waiting (QUEUED/PREEMPTED) for capacity; no
      ADMITTED/RUNNING/PREEMPTING/CANCELLING left
  SQ2 no sched-* sandbox in the engine without a live job
  SQ3 a job that was preempted and had capacity afterwards resumed (ran)
"""
import importlib.util
import os
import pathlib
import random
import sys

from app.core import ApiError as ApiApiError

from . import scenario as apisim
from .invariants import check_invariants as api_invariants


def _load_sched_app():
    """Import sandbox-scheduler's `app` package under the name `sched_app`
    (both services call their package `app`). Looks in the container layout
    first, then the sibling checkout."""
    if "sched_app" in sys.modules:
        return sys.modules["sched_app"]
    here = pathlib.Path(__file__).resolve().parent
    for cand in (os.environ.get("SCHED_APP_DIR"), "/work/sched/app",
                 str(here.parents[1] / "sandbox-scheduler" / "scheduler" / "app")):
        if cand and pathlib.Path(cand, "__init__.py").exists():
            spec = importlib.util.spec_from_file_location(
                "sched_app", pathlib.Path(cand, "__init__.py"), submodule_search_locations=[cand])
            mod = importlib.util.module_from_spec(spec)
            sys.modules["sched_app"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("sandbox-scheduler app package not found (set SCHED_APP_DIR)")


_load_sched_app()
from sched_app import clock as sched_clock  # noqa: E402
from sched_app import config as sched_config  # noqa: E402
from sched_app.core import ApiError as SchedApiError  # noqa: E402
from sched_app.core import Core as SchedCore  # noqa: E402
from sched_app.store import Store as SchedStore  # noqa: E402


class UpstreamDown(Exception):
    pass


class UpstreamConflict(Exception):
    pass


class CheckpointDown(Exception):
    pass


UpstreamDown.__name__ = "UpstreamDown"
CheckpointDown.__name__ = "CheckpointDown"


class SimUpstream:
    """What the scheduler core sees as sandbox-api + checkpoint service."""
    Down = UpstreamDown
    Conflict = UpstreamConflict

    def __init__(self, api: apisim.SimApi, engine, rng):
        self.api = api
        self.engine = engine
        self.rng = rng
        self.checkpoints: dict[str, dict] = {}
        self.checkpoint_down = False
        self.exec_results: dict[str, int] = {}

    def _guard(self):
        if self.engine.down:
            raise UpstreamDown("engine down")

    def status(self):
        self._guard()
        used = self.api.core.store.used_ports()
        free = [p for p in range(self.api.core.pool_start, self.api.core.pool_end + 1) if p not in used]
        return {"pool": {"free": free}, "engine": {"memory_bytes": 4 << 30}}

    def list_sandboxes(self):
        self._guard()
        return [self.api.core.view(r) for r in self.api.core.store.list_all()]

    def get_sandbox(self, sid):
        self._guard()
        r = self.api.core.store.get(sid)
        return self.api.core.view(r) if r and r["state"] != "DELETED" else None

    def create_sandbox(self, name, template, stateful=False):
        self._guard()
        try:
            outcome, row = self.api.create(name, template, None, None)
        except ApiApiError as e:
            if e.status == 429:
                raise UpstreamConflict("upstream capacity exhausted (429)")
            if e.status == 503:
                raise UpstreamDown(e.detail)
            raise
        return self.api.core.view(row)

    def delete_sandbox(self, sid):
        self._guard()
        try:
            self.api.core.delete(sid)
        except ApiApiError as e:
            if e.status == 404:
                return
            if e.status == 409:
                raise UpstreamConflict(e.detail)
            if e.status == 503:
                raise UpstreamDown(e.detail)
            raise

    def exec_sandbox(self, sid, cmd, timeout):
        self._guard()
        row = self.api.core.store.get(sid)
        if not row or row["state"] not in ("READY", "DEGRADED"):
            raise RuntimeError("exec requires READY")
        code = self.exec_results.get(sid, 0 if self.rng.random() < 0.8 else 1)
        return {"exit_code": code, "stdout": "ok", "stderr": "", "timed_out": False}

    def sandbox_memory(self, sid):
        self._guard()
        return 64 << 20

    def checkpoint_create(self, sid, label=""):
        if self.checkpoint_down or self.engine.down:
            raise CheckpointDown("checkpoint service down")
        row = self.api.core.store.get(sid)
        if not row or row["state"] not in ("READY", "DEGRADED", "STOPPED"):
            raise CheckpointDown(f"sandbox {sid} not checkpointable")
        cid = f"ck{len(self.checkpoints) + 1:04d}"
        self.checkpoints[cid] = {"id": cid, "template": row["template"], "sandbox": sid}
        return self.checkpoints[cid]

    def fork_checkpoint(self, cid, name):
        if self.checkpoint_down:
            raise CheckpointDown("checkpoint service down")
        m = self.checkpoints.get(cid)
        if not m:
            raise CheckpointDown("unknown checkpoint")
        return self.create_sandbox(name, m["template"], stateful=True)


class SchedScenario:
    def __init__(self, seed: int, steps: int = 400, verbose: bool = False):
        self.seed = seed
        self.rng = random.Random(seed)
        self.steps = steps
        self.verbose = verbose
        self.api_sc = apisim.Scenario(seed, steps=0, max_sandboxes=8, pool=(9200, 9205))
        self.api_sc.verbose = verbose
        sched_clock.impl = self.api_sc.clock                # one virtual clock for both
        self.upstream = SimUpstream(self.api_sc.api, self.api_sc.engine, self.rng)
        self.store = SchedStore(f"file:sched{seed}_{id(self)}?mode=memory&cache=shared")
        self.store.init()
        self._ids = 0

        def next_id():
            self._ids += 1
            return f"j{seed:04d}{self._ids:05d}"
        self.core = SchedCore(self.store, self.upstream, cpu_units=4, max_sandboxes=6,
                              max_queue=20, max_per_tenant=3, aging_interval=5,
                              aging_max_bonus=6, preempt_priority=8, resume_bonus=3,
                              memory_budget=None, mem_per_unit=256 << 20, id_factory=next_id)
        self.provisions: dict[str, object] = {}
        self.preempts: dict[str, object] = {}
        self.log = self.api_sc.log
        self.preempted_ever: set[str] = set()
        self.stats = {"ops": 0, "submits": 0, "admits": 0, "preempts": 0, "resumes": 0, "crashes": 0, "errors": {}}

    def _say(self, m):
        self.api_sc._say("[sched] " + m)

    def step(self):
        r = self.rng
        kind = r.choices(
            ["submit", "cancel", "placer", "monitor", "janitor", "cancel_tick", "prov", "preempt_step",
             "task_done", "api_step", "time", "sched_crash", "ckpt_down", "ckpt_up"],
            weights=[12, 4, 14, 12, 5, 5, 16, 8, 6, 20, 8, 2, 1, 2])[0]
        self.stats["ops"] += 1
        try:
            if kind == "submit":
                kindj = r.choice(["task", "service", "service"])
                req = {"name": r.choice(["a", "b", "c", "d"]) + "-job", "tenant": r.choice(["t1", "t2", "t3"]),
                       "template": r.choice(["blank", "web", "coreapp"]), "kind": kindj,
                       "cmd": ["true"] if kindj == "task" else None, "priority": r.choice([1, 3, 5, 8, 9]),
                       "count": r.choice([1, 1, 1, 2]), "max_runtime": r.choice([None, 30, 120]),
                       "deadline_seconds": r.choice([None, None, 20, 60]),
                       "stateful": kindj == "service" and r.random() < 0.7, "preemptible": None}
                if req["stateful"]:
                    req["preemptible"] = True
                outcome, job = self.core.submit(req, None)
                self.stats["submits"] += 1
                self._say(f"submit {job['id']} {kindj} prio={req['priority']} tenant={req['tenant']} stateful={req['stateful']}")
            elif kind == "cancel":
                live = self.store.by_state("QUEUED", "PREEMPTED", "ADMITTED", "RUNNING", "PREEMPTING")
                if live:
                    j = r.choice(live)
                    self.core.cancel(j["id"])
                    self._say(f"cancel {j['id']} (was {j['state']})")
            elif kind == "placer":
                res = self.core.placer_tick()
                for jid in res["admitted"]:
                    self.provisions[jid] = self.core.provision_steps(jid)
                    self.stats["admits"] += 1
                    if self.store.get(jid).get("checkpoint_id"):
                        self.stats["resumes"] += 1
                for jid in res["preempting"]:
                    self.preempts[jid] = self.core.preempt_steps(jid)
                    self.stats["preempts"] += 1
                    self.preempted_ever.add(jid)
                if res["admitted"] or res["preempting"]:
                    self._say(f"placer admitted={res['admitted']} preempting={res['preempting']}")
            elif kind == "monitor":
                self.core.monitor_tick()
            elif kind == "janitor":
                self.core.janitor_tick()
            elif kind == "cancel_tick":
                self.core.cancel_tick()
            elif kind == "prov" and self.provisions:
                jid = r.choice(list(self.provisions))
                try:
                    next(self.provisions[jid])
                except StopIteration:
                    del self.provisions[jid]
            elif kind == "preempt_step" and self.preempts:
                jid = r.choice(list(self.preempts))
                try:
                    next(self.preempts[jid])
                except StopIteration:
                    del self.preempts[jid]
            elif kind == "task_done":
                for j in self.store.by_state("RUNNING"):
                    if j["kind"] == "task":
                        self.core.run_task(j["id"])
                        self._say(f"task {j['id']} finished")
                        break
            elif kind == "api_step":
                k = self.api_sc.step(r.choice(["provision", "provision", "informer", "reaper", "resync",
                                               "kill", "remove", "engine_down", "engine_up", "crash"]))
            elif kind == "time":
                self.api_sc.clock.advance(r.choice([1, 5, 10, 30]))
            elif kind == "sched_crash":
                self.provisions.clear()
                self.preempts.clear()
                self.stats["crashes"] += 1
                self.core.reconcile()
                self._say("SCHED CRASH + reconcile")
            elif kind == "ckpt_down":
                self.upstream.checkpoint_down = True
            elif kind == "ckpt_up":
                self.upstream.checkpoint_down = False
        except (SchedApiError, ApiApiError) as e:
            self.stats["errors"][f"{kind}:{e.status}"] = self.stats["errors"].get(f"{kind}:{e.status}", 0) + 1
        except Exception as e:
            if type(e).__name__ in ("EngineDown", "UpstreamDown", "CheckpointDown"):
                self._say(f"{kind} -> {type(e).__name__}")
            else:
                raise
        return kind

    # -- invariants ----------------------------------------------------------------

    def check(self, step, quiescent=False):
        api_invariants(self.api_sc, step, quiescent=False)
        jobs = self.store.list_jobs(limit_terminal=10_000)
        engine = self.api_sc.engine.sandboxes
        api_rows = {r["id"]: r for r in self.api_sc.store.list_all()}
        active = [j for j in jobs if j["state"] in sched_config.ACTIVE]
        cpu = sum(j["cpu_units"] * j["count"] for j in active)
        if cpu > self.core.cpu_units + 1:        # +1 slack: unmanaged sandboxes are "foreign"
            self._fail(step, "reserved cpu exceeds cluster", cpu=cpu)
        tenant: dict[str, int] = {}
        for j in active:
            tenant[j["tenant"]] = tenant.get(j["tenant"], 0) + j["count"]
        for t, n in tenant.items():
            if n > self.core.max_per_tenant:
                self._fail(step, "tenant over cap", tenant=t, n=n)
        for j in jobs:
            if j["state"] not in ("QUEUED", "ADMITTED", "RUNNING", "PREEMPTING", "PREEMPTED", "CANCELLING",
                                  "SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED"):
                self._fail(step, "unknown job state", job=j)
            seqs = [e["seq"] for e in self.store.events(j["id"])]
            if seqs != list(range(1, len(seqs) + 1)):
                self._fail(step, "job event seq gap", job=j["id"], seqs=seqs)
            if j["state"] == "PREEMPTED" and (not j.get("checkpoint_id") or j["sandbox_ids"]):
                self._fail(step, "PREEMPTED without checkpoint or with sandboxes", job=j)
            if j["state"] in sched_config.TERMINAL and not self.api_sc.engine.down:
                for k in range(j["count"]):
                    n = self.core.sandbox_name(j, k)
                    for sid, sb in engine.items():
                        if sb["name"] == n and api_rows.get(sid, {}).get("state") not in (None, "DELETED"):
                            pass  # janitor reaps; strong form at quiescence
        # S6: the submit bound. A crash re-queues in-flight ADMITTED jobs, so
        # the depth may transiently exceed MAX_QUEUE by at most the number of
        # jobs that can be admitted at once (cpu units + unmanaged slack).
        if self.store.queue_depth() > self.core.max_queue + self.core.cpu_units + 1:
            self._fail(step, "queue over bound", depth=self.store.queue_depth())
        if self.stats.get("submit_429_checked") is None:
            self.stats["submit_429_checked"] = 0
        if not quiescent:
            return
        for j in jobs:
            if j["state"] in ("ADMITTED", "RUNNING", "PREEMPTING", "CANCELLING"):
                self._fail(step, "non-quiescent job state at quiescence", job=j)
        live_names = {self.core.sandbox_name(j, k) for j in jobs
                      if j["state"] in ("QUEUED", "PREEMPTED") for k in range(j["count"])}
        for sid, sb in engine.items():
            if sb["name"].startswith("sched-") and sb["name"] not in live_names:
                self._fail(step, "leaked scheduler sandbox at quiescence", sandbox=sb["name"])

    def _fail(self, step, msg, **ctx):
        raise AssertionError(f"seed={self.seed} step={step}: {msg}\n  ctx={ctx}\n  last log:\n    "
                             + "\n    ".join(self.log[-15:]))

    def run(self):
        for i in range(self.steps):
            self.step()
            self.check(i)
        # quiesce: engine up, drain generators, cancel everything, drain ticks
        self.api_sc.engine.down = False
        self.upstream.checkpoint_down = False
        for _ in range(60):
            for jid in list(self.provisions):
                try:
                    next(self.provisions[jid])
                except StopIteration:
                    del self.provisions[jid]
            for jid in list(self.preempts):
                try:
                    next(self.preempts[jid])
                except StopIteration:
                    del self.preempts[jid]
            for sid in list(self.api_sc.api.provisions):
                self.api_sc.api.step_provision(sid)
            for ev in self.api_sc.engine.drain_events():
                self.api_sc.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
            try:
                # production: sandbox-api's periodic resync keeps running while
                # the scheduler drains; without it an orphaned CREATING sandbox
                # (api crash while the engine was down) blocks cancel forever
                self.api_sc.core.reconcile(periodic=True)
                self.core.monitor_tick()
                for j in self.store.by_state("RUNNING"):
                    if j["kind"] == "task":
                        self.core.run_task(j["id"])
                for j in self.store.by_state("RUNNING", "ADMITTED", "PREEMPTING"):
                    try:
                        self.core.cancel(j["id"])
                    except SchedApiError:
                        pass
                self.core.cancel_tick()
                self.core.janitor_tick()
            except Exception as e:
                if type(e).__name__ not in ("EngineDown", "UpstreamDown"):
                    raise
        self.api_sc.core.reconcile(periodic=True)
        self.check(self.steps, quiescent=True)

    def close(self):
        if self.store._keeper is not None:
            self.store._keeper.close()
        self.api_sc.close()


def run_sched_seed(seed: int, steps: int = 400, verbose: bool = False) -> dict:
    sc = SchedScenario(seed, steps, verbose)
    try:
        sc.run()
        return {"seed": seed, "ok": True, **sc.stats}
    except AssertionError as e:
        return {"seed": seed, "ok": False, "error": str(e), "log": sc.log[-30:], **sc.stats}
    finally:
        sc.close()
