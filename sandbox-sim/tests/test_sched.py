"""Scheduler core under simulation (preemption, EDF, fairness, crashes)."""
import pytest

from sim.sched import SchedScenario, run_sched_seed


@pytest.mark.parametrize("seed", range(0, 40))
def test_random_scheduler_scenarios(seed):
    res = run_sched_seed(seed, steps=300)
    assert res["ok"], res["error"]


def _drive(sc: SchedScenario, n=40):
    """Advance everything deterministically: placer, provisions, monitor."""
    for _ in range(n):
        res = sc.core.placer_tick()
        for jid in res["admitted"]:
            sc.provisions[jid] = sc.core.provision_steps(jid)
        for jid in res["preempting"]:
            sc.preempts[jid] = sc.core.preempt_steps(jid)
        for d in (sc.provisions, sc.preempts):
            for jid in list(d):
                try:
                    next(d[jid])
                except StopIteration:
                    del d[jid]
        for sid in list(sc.api_sc.api.provisions):
            sc.api_sc.api.step_provision(sid)
        sc.core.monitor_tick()
        sc.api_sc.clock.advance(1)


def _submit(sc, **kw):
    req = {"name": "x", "tenant": "t1", "template": "blank", "kind": "service", "cmd": None,
           "priority": 3, "count": 1, "max_runtime": None, "deadline_seconds": None,
           "stateful": False, "preemptible": None}
    req.update(kw)
    return sc.core.submit(req, None)[1]


def test_preemption_evicts_lowest_priority_stateful_service_and_resumes_by_fork():
    sc = SchedScenario(1, steps=0)
    # fill 4 cpu units with 4 low-priority stateful services (preemptible)
    low = [_submit(sc, priority=1, stateful=True, preemptible=True, cpu_units=1) for _ in range(4)]
    _drive(sc, 10)
    assert all(sc.store.get(j["id"])["state"] == "RUNNING" for j in low)
    # a priority-9 job arrives: cannot fit -> preempt one victim
    high = _submit(sc, priority=9, cpu_units=1, kind="task", cmd=["true"])
    _drive(sc, 12)
    pre = [sc.store.get(j["id"]) for j in low]
    preempted = [j for j in pre if j["state"] == "PREEMPTED" or j.get("preempt_count")]
    assert len(preempted) >= 1 and preempted[0]["checkpoint_id"]
    assert sc.store.get(high["id"])["state"] in ("RUNNING", "ADMITTED", "SUCCEEDED")
    # finish the high job -> the preempted one resumes by fork
    sc.core.run_task(high["id"])
    _drive(sc, 15)
    resumed = sc.store.get(preempted[0]["id"])
    assert resumed["state"] == "RUNNING"
    assert any("forked from checkpoint" in e["message"] for e in sc.store.events(resumed["id"]))
    sc.close()


def test_non_stateful_jobs_are_never_preempted():
    sc = SchedScenario(2, steps=0)
    low = [_submit(sc, priority=1, stateful=False, cpu_units=1) for _ in range(4)]
    _drive(sc, 10)
    _submit(sc, priority=9, cpu_units=1, kind="task", cmd=["true"])
    _drive(sc, 12)
    assert all(sc.store.get(j["id"])["state"] == "RUNNING" for j in low)
    assert sc.core.state["preemptions"] == 0
    sc.close()


def test_edf_orders_equal_priority_by_deadline():
    sc = SchedScenario(3, steps=0)
    blockers = [_submit(sc, priority=5, cpu_units=1) for _ in range(4)]
    _drive(sc, 8)
    late = _submit(sc, priority=5, cpu_units=1, deadline_seconds=600, name="late")
    soon = _submit(sc, priority=5, cpu_units=1, deadline_seconds=30, name="soon")
    sc.core.cancel(blockers[0]["id"])
    for _ in range(5):
        sc.core.cancel_tick()
        _drive(sc, 1)
    assert sc.store.get(soon["id"])["state"] in ("ADMITTED", "RUNNING")
    assert sc.store.get(late["id"])["state"] == "QUEUED"
    sc.close()


def test_deadline_miss_is_counted():
    sc = SchedScenario(4, steps=0)
    j = _submit(sc, kind="task", cmd=["true"], deadline_seconds=5)
    _drive(sc, 3)
    sc.api_sc.clock.advance(100)
    sc.core.run_task(j["id"])
    assert sc.store.get(j["id"])["deadline_missed"] is True or sc.store.get(j["id"])["deadline_missed"] == 1
    assert sc.core.state["deadline_missed"] == 1
    sc.close()


def test_preemption_aborts_cleanly_when_checkpoint_service_is_down():
    sc = SchedScenario(5, steps=0)
    low = [_submit(sc, priority=1, stateful=True, preemptible=True, cpu_units=1) for _ in range(4)]
    _drive(sc, 10)
    sc.upstream.checkpoint_down = True
    _submit(sc, priority=9, cpu_units=1, kind="task", cmd=["true"])
    _drive(sc, 12)
    assert all(sc.store.get(j["id"])["state"] == "RUNNING" for j in low)   # victims untouched
    assert sc.core.state["checkpoint_down"] is True
    sc.close()


def test_scheduler_crash_during_preemption_recovers():
    sc = SchedScenario(6, steps=0)
    low = [_submit(sc, priority=1, stateful=True, preemptible=True, cpu_units=1) for _ in range(4)]
    _drive(sc, 10)
    _submit(sc, priority=9, cpu_units=1, kind="task", cmd=["true"])
    res = sc.core.placer_tick()
    assert res["preempting"]
    victim = res["preempting"][0]
    assert sc.store.get(victim)["state"] == "PREEMPTING"
    sc.provisions.clear(); sc.preempts.clear()
    sc.core.reconcile()                                    # crash + restart
    assert sc.store.get(victim)["state"] == "RUNNING"      # back to a truthful state
    _drive(sc, 15)                                         # ... and preempted again, cleanly
    assert sc.core.state["preemptions"] >= 1
    sc.close()
