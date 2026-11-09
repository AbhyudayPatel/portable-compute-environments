"""Mutation tests: prove the harness CATCHES bugs by injecting them.

Each test monkeypatches one safeguard out of the core and asserts that
random scenarios now fail an invariant. If a test here passes, the
simulator has teeth for that class of bug.
"""
import pytest

from app import core as coremod
from sim.scenario import run_seed


def _some_seed_fails(n=40, steps=200):
    return any(not run_seed(s, steps)["ok"] for s in range(n))


def test_catches_missing_rollback_on_create_failure(monkeypatch):
    """Bug: provisioning failure marks FAILED but forgets to free the port."""
    orig = coremod.Core.provision_steps

    def buggy(self, sid):
        row = self.store.get(sid)
        try:
            yield from orig(self, sid)
        finally:
            r = self.store.get(sid)
            if r and r["state"] == "FAILED" and row:
                self.store.set_port(sid, row["port"])    # re-leak the port
    monkeypatch.setattr(coremod.Core, "provision_steps", buggy)
    assert _some_seed_fails(), "simulator did not detect the leaked port"


def test_catches_reconcile_that_forgets_vanished_rows(monkeypatch):
    """Bug: reconcile never marks rows whose containers vanished."""
    orig = coremod.Core.reconcile

    def buggy(self, periodic=False):
        engine_view = self.engine.engine_sandboxes()
        for sid in list(engine_view):
            pass
        return {"noop": True}
    monkeypatch.setattr(coremod.Core, "reconcile", buggy)
    assert _some_seed_fails(), "simulator did not detect engine/DB divergence"


def test_catches_ttl_not_reanchored_after_crash(monkeypatch):
    """Bug: v1 behaviour - monotonic deadlines are never rebased on boot, so
    after a restart (new monotonic clock) TTLs slide or never fire."""
    from app.store import Store
    monkeypatch.setattr(Store, "with_deadlines", lambda self: [])   # no re-anchor
    # make crashes reset the monotonic clock like a real new process does
    from sim import scenario as scmod
    orig_crash = scmod.SimApi.crash

    def crash(self):
        orig_crash(self)
        import app.clock as clockmod
        clockmod.impl._mono = 1.0
    monkeypatch.setattr(scmod.SimApi, "crash", crash)
    assert _some_seed_fails(60, 300), "simulator did not detect the TTL drift"


def test_catches_informer_ignoring_stop_lock(monkeypatch):
    """Bug: informer marks DEGRADED without re-checking state under the lock."""
    orig = coremod.Core.on_container_event

    def buggy(self, sid, action, cname, attrs):
        if sid and action == "die":
            row = self.store.get(sid)
            if row and row["state"] in ("READY", "DEGRADED", "STOPPED"):
                self.store.set_state(sid, "DEGRADED", None, {"dead": {cname: "x"}})
                self.store.add_event(sid, "informer", "buggy degraded")
            return
        return orig(self, sid, action, cname, attrs)
    monkeypatch.setattr(coremod.Core, "on_container_event", buggy)
    assert _some_seed_fails(), "simulator did not detect the stop/informer race"
