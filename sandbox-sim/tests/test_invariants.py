"""Property tests: many seeds must pass; specific scenarios must behave.

Run:  pytest -q          (inside the sim image or with PYTHONPATH set)
"""
import random

import pytest

from sim.scenario import Scenario, run_seed


@pytest.mark.parametrize("seed", range(0, 60))
def test_random_scenarios_hold_invariants(seed):
    res = run_seed(seed, steps=250)
    assert res["ok"], res["error"]


def _fresh(seed=1):
    return Scenario(seed, steps=0)


def test_idempotent_create_same_spec_returns_existing():
    sc = _fresh()
    o1, r1 = sc.api.create("alpha", "web")
    o2, r2 = sc.api.create("alpha", "web")
    assert (o1, o2) == ("created", "existing") and r1["id"] == r2["id"]
    sc.close()


def test_create_conflict_different_spec_409():
    sc = _fresh()
    sc.api.create("alpha", "web")
    with pytest.raises(Exception) as ei:
        sc.api.create("alpha", "coreapp")
    assert ei.value.status == 409
    sc.close()


def test_port_exhaustion_429_and_failed_holds_no_port():
    sc = Scenario(2, steps=0, pool=(9200, 9201))
    sc.api.create("alpha", "web")
    sc.api.create("beta", "web")
    with pytest.raises(Exception) as ei:
        sc.api.create("gamma", "web")
    assert ei.value.status == 429
    failed = [r for r in sc.store.list_all() if r["name"] == "gamma"][0]
    assert failed["state"] == "FAILED" and failed["port"] is None
    sc.close()


def test_partial_create_failure_rolls_back_and_frees_port():
    sc = _fresh(3)
    sc.engine.fail_create_at = 2                  # coreapp: db ok, backend fails
    _, row = sc.api.create("alpha", "coreapp")
    while sc.api.step_provision(row["id"]):
        pass
    r = sc.store.get(row["id"])
    assert r["state"] == "FAILED" and r["port"] is None
    assert row["id"] not in sc.engine.sandboxes
    assert any("create failed" in e["message"] for e in sc.store.events(row["id"]))
    sc.close()


def test_crash_mid_create_is_rolled_back_on_boot():
    sc = _fresh(4)
    _, row = sc.api.create("alpha", "coreapp")
    while row["id"] not in sc.engine.sandboxes:   # images..., then resources
        sc.api.step_provision(row["id"])
    assert sc.store.get(row["id"])["state"] == "CREATING"   # not READY yet
    sc.api.crash()
    sc.core.reconcile()
    r = sc.store.get(row["id"])
    assert r["state"] == "FAILED" and r["port"] is None
    assert row["id"] not in sc.engine.sandboxes
    sc.close()


def test_ttl_survives_restart_via_wall_deadline():
    sc = _fresh(5)
    _, row = sc.api.create("alpha", "blank", ttl=30)
    while sc.api.step_provision(row["id"]):
        pass
    sc.clock.advance(20)
    sc.api.crash()
    # a NEW process has a new monotonic clock: simulate by rebasing
    sc.clock._mono = 5.0
    sc.core.reconcile()                           # re-anchors deadline_mono
    assert sc.core.reap_expired() == []           # 10 s still left
    sc.clock.advance(11)
    assert sc.core.reap_expired() == [row["id"]]
    sc.close()


def test_informer_marks_degraded_and_start_heals():
    sc = _fresh(6)
    _, row = sc.api.create("alpha", "coreapp")
    while sc.api.step_provision(row["id"]):
        pass
    cname = f"sbx-{row['id']}-backend"
    sc.engine.kill(row["id"], cname)
    ev = sc.engine.drain_events()[0]
    sc.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
    r = sc.store.get(row["id"])
    assert r["state"] == "DEGRADED" and cname in r["detail"]["dead"]
    sc.core.start(row["id"])
    assert sc.store.get(row["id"])["state"] == "READY"
    sc.close()


def test_stop_then_die_events_do_not_degrade():
    """stop() causes die events; they must be ignored because the state
    already explains them (the per-sandbox lock + re-read inside)."""
    sc = _fresh(7)
    _, row = sc.api.create("alpha", "web")
    while sc.api.step_provision(row["id"]):
        pass
    sc.core.stop(row["id"])
    sc.engine.events.append(("die", row["id"], f"sbx-{row['id']}-web", {"exitCode": "0"}))
    ev = sc.engine.drain_events()[0]
    sc.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
    assert sc.store.get(row["id"])["state"] == "STOPPED"
    sc.close()


def test_out_of_band_removal_marks_deleted_and_frees_port():
    sc = _fresh(8)
    _, row = sc.api.create("alpha", "web")
    while sc.api.step_provision(row["id"]):
        pass
    port = sc.store.get(row["id"])["port"]
    sc.engine.remove_out_of_band(row["id"])
    for ev in sc.engine.drain_events():
        sc.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
    assert sc.store.get(row["id"])["state"] == "DELETED"
    assert port not in sc.store.used_ports()
    sc.close()


def test_reaper_vs_delete_race_is_idempotent():
    sc = _fresh(9)
    _, row = sc.api.create("alpha", "blank", ttl=10)
    while sc.api.step_provision(row["id"]):
        pass
    sc.clock.advance(11)
    sc.core.delete(row["id"])                     # manual delete wins
    assert sc.core.reap_expired() == []           # reaper finds nothing torn
    with pytest.raises(Exception) as ei:
        sc.core.delete(row["id"])
    assert ei.value.status == 404
    sc.close()


def test_engine_down_gives_503_never_hangs():
    sc = _fresh(10)
    sc.engine.down = True
    with pytest.raises(Exception) as ei:
        sc.api.create("alpha", "web")
    assert ei.value.status == 503 and ei.value.headers.get("Retry-After")
    sc.close()


def test_determinism_same_seed_same_log():
    a = Scenario(11, steps=120); a.run(); la = list(a.log); a.close()
    b = Scenario(11, steps=120); b.run(); lb = list(b.log); b.close()
    assert la == lb
