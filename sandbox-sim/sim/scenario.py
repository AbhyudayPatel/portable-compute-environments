"""The deterministic scenario driver.

One scenario = one seed. From the seed we draw a random sequence of
"steps"; each step is ONE atomic interaction with the core, chosen from:

    client ops     create / delete / stop / start / get   (what REST does)
    provision      advance one in-flight provision generator by one step
    reaper         one TTL sweep
    informer       deliver one pending engine event to the core
    resync         Core.reconcile(periodic=True)
    time           advance the virtual clock
    faults         engine down/up, kill a container, remove out-of-band,
                   fail the next create, wall-clock skew
    crash          "kill -9" the API: drop every in-flight provision, forget
                   locks, then restart = Core.reconcile() on boot

After EVERY step the invariants are checked. Because everything (RNG, clock,
fault timing) derives from the seed, a failing seed replays identically,
and `shrink()` finds a shorter step sequence that still fails.
"""
import random
import sqlite3

from app import clock as clockmod
from app.core import ApiError, Core
from app.store import Store

from .fake_engine import FakeEngine
from .invariants import check_invariants
from .vclock import VirtualClock

NAMES = ["alpha", "beta", "gamma", "delta", "eps", "zeta", "eta", "theta"]
TEMPLATES = ["web", "blank", "coreapp"]


class SimApi:
    """The thing a simulated client calls. Mirrors main.py's thin routes."""

    def __init__(self, core: Core):
        self.core = core
        self.provisions: dict[str, object] = {}     # sid -> generator

    def create(self, name, template, ttl=None, isolation=None):
        outcome, row = self.core.create(name, template, ttl, isolation)
        if outcome == "created":
            self.provisions[row["id"]] = self.core.provision_steps(row["id"])
        return outcome, row

    def step_provision(self, sid) -> bool:
        gen = self.provisions.get(sid)
        if gen is None:
            return False
        try:
            next(gen)
            return True
        except StopIteration:
            del self.provisions[sid]
            return False

    def crash(self):
        """Process death: generators and locks vanish, the DB stays, and
        the new process owes a boot reconcile."""
        self.provisions.clear()
        self.core._locks.clear()
        self.core.in_flight.clear()
        self.core.boot_pending = True


class Scenario:
    def __init__(self, seed: int, steps: int = 300, verbose: bool = False,
                 max_sandboxes: int = 6, pool: tuple[int, int] = (9200, 9204)):
        self.seed = seed
        self.rng = random.Random(seed)
        self.steps = steps
        self.verbose = verbose
        self.clock = VirtualClock()
        clockmod.impl = self.clock
        self.engine = FakeEngine(self.clock, self.rng)
        self.store = Store(f"file:sim{seed}_{id(self)}?mode=memory&cache=shared")
        self.store.init()
        self._ids = 0

        def next_id():
            self._ids += 1
            return f"s{seed:04d}{self._ids:06d}"
        self.core = Core(self.store, self.engine, max_sandboxes=max_sandboxes,
                         pool_start=pool[0], pool_end=pool[1],
                         id_factory=next_id, token_factory=lambda: f"tok{self._ids}")
        self.api = SimApi(self.core)
        self.log: list[str] = []
        self.skewed = False                           # wall clock skew injected?
        self.user_stopped: set[str] = set()           # sids the "user" stopped
        self.stats = {"ops": 0, "errors": {}, "crashes": 0, "created": 0,
                      "deleted": 0, "degraded": 0}

    # -- helpers -----------------------------------------------------------------------

    def _say(self, msg: str):
        self.log.append(f"[t={self.clock.monotonic():.0f}] {msg}")
        if self.verbose:
            print(self.log[-1])

    def _live_ids(self):
        return [r["id"] for r in self.store.list_all()]

    def _err(self, op, e: ApiError):
        self.stats["errors"][f"{op}:{e.status}"] = self.stats["errors"].get(f"{op}:{e.status}", 0) + 1
        self._say(f"{op} -> {e.status} {e.detail}")

    # -- one step ----------------------------------------------------------------------

    def step(self, kind: str | None = None) -> str:
        r = self.rng
        kind = kind or r.choices(
            ["create", "delete", "stop", "start", "provision", "reaper", "informer",
             "resync", "time", "engine_down", "engine_up", "kill", "remove", "fail_next",
             "crash", "skew", "pause", "unpause"],
            weights=[14, 8, 4, 4, 22, 5, 8, 3, 10, 2, 3, 4, 2, 2, 2, 1, 2, 2])[0]
        self.stats["ops"] += 1
        live = self._live_ids()
        try:
            if kind == "create":
                name = r.choice(NAMES)
                tmpl = r.choice(TEMPLATES)
                ttl = r.choice([None, None, 20, 60])
                iso = r.choice(["container", "container", "gvisor"])
                outcome, row = self.api.create(name, tmpl, ttl, iso)
                self._say(f"create {name}/{tmpl} ttl={ttl} -> {outcome} {row['id']}")
                self.stats["created"] += outcome == "created"
            elif kind == "delete" and live:
                sid = r.choice(live)
                self.api.core.delete(sid)
                self.user_stopped.discard(sid)
                self._say(f"delete {sid} -> ok")
                self.stats["deleted"] += 1
            elif kind == "stop" and live:
                sid = r.choice(live)
                self.api.core.stop(sid)
                self.user_stopped.add(sid)
                self._say(f"stop {sid} -> ok")
            elif kind == "start" and live:
                sid = r.choice(live)
                self.api.core.start(sid)
                self.user_stopped.discard(sid)
                self._say(f"start {sid} -> ok")
            elif kind == "pause" and live:
                sid = r.choice(live)
                self.api.core.pause(sid)
                self._say(f"pause {sid} -> ok")
            elif kind == "unpause" and live:
                sid = r.choice(live)
                self.api.core.unpause(sid)
                self._say(f"unpause {sid} -> ok")
            elif kind == "provision" and self.api.provisions:
                sid = r.choice(list(self.api.provisions))
                more = self.api.step_provision(sid)
                self._say(f"provision step {sid} -> {'more' if more else 'done'}")
            elif kind == "reaper":
                reaped = self.core.reap_expired()
                self._say(f"reaper -> {reaped}")
            elif kind == "informer" and self.engine.events:
                ev = self.engine.events.pop(0)
                self.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
                self._say(f"informer {ev[0]} {ev[1]}/{ev[2]}")
            elif kind == "resync":
                st = self.core.reconcile(periodic=True)
                self._say(f"resync -> {st}")
            elif kind == "time":
                dt = r.choice([1, 5, 10, 30])
                self.clock.advance(dt)
                self._say(f"time +{dt}")
            elif kind == "engine_down":
                self.engine.down = True
                self._say("ENGINE DOWN")
            elif kind == "engine_up":
                self.engine.down = False
                self._say("engine up")
            elif kind == "kill" and self.engine.sandboxes:
                sid = r.choice(list(self.engine.sandboxes))
                cname = r.choice(list(self.engine.sandboxes[sid]["containers"]))
                self.engine.kill(sid, cname, oom=r.random() < 0.3)
                self._say(f"FAULT kill {sid}/{cname}")
            elif kind == "remove" and self.engine.sandboxes:
                sid = r.choice(list(self.engine.sandboxes))
                self.engine.remove_out_of_band(sid)
                self.user_stopped.discard(sid)        # gone: no intent to keep
                self._say(f"FAULT remove {sid}")
            elif kind == "fail_next":
                self.engine.fail_create_at = r.choice([1, 2, 3])
                self._say(f"FAULT next create fails at container {self.engine.fail_create_at}")
            elif kind == "crash":
                self.api.crash()
                self.stats["crashes"] += 1
                self._say("CRASH (kill -9) ...")
                if not self.engine.down:
                    st = self.core.reconcile()
                    self._say(f"restart -> reconcile {st}")
                else:
                    self._say("restart -> engine down, reconcile skipped")
            elif kind == "skew":
                dt = r.choice([-300, 300, 3600])
                self.clock.skew_wall(dt)
                self.skewed = True
                self._say(f"wall clock skew {dt:+}")
            else:
                kind = "noop"
        except ApiError as e:
            self._err(kind, e)
        except Exception as e:
            if type(e).__name__ == "EngineDown":
                self._say(f"{kind} -> engine down")
            else:
                raise
        return kind

    def run(self) -> None:
        for i in range(self.steps):
            self.step()
            check_invariants(self, i)
        # quiescence: drain everything, then the strong invariants must hold
        self.engine.down = False
        for _ in range(50):
            for sid in list(self.api.provisions):
                self.api.step_provision(sid)
        for ev in self.engine.drain_events():
            self.core.on_container_event(ev[1], ev[0], ev[2], ev[3])
        self.core.reconcile(periodic=True)
        check_invariants(self, self.steps, quiescent=True)

    def close(self):
        if self.store._keeper is not None:
            self.store._keeper.close()


def run_seed(seed: int, steps: int = 300, verbose: bool = False) -> dict:
    sc = Scenario(seed, steps, verbose)
    try:
        sc.run()
        return {"seed": seed, "ok": True, **sc.stats}
    except AssertionError as e:
        return {"seed": seed, "ok": False, "error": str(e), "log": sc.log[-25:], **sc.stats}
    finally:
        sc.close()


def shrink(seed: int, steps: int) -> int:
    """Smallest step count (binary search) that still fails for this seed."""
    lo, hi = 1, steps
    while lo < hi:
        mid = (lo + hi) // 2
        if run_seed(seed, mid)["ok"]:
            lo = mid + 1
        else:
            hi = mid
    return lo
