"""The lifecycle core - all sandbox logic, no HTTP, no threads, no clock.

Everything main.py exposes over REST is a call into this class. Everything
the simulator (../sandbox-sim) tests is this class driven with a fake
engine and a virtual clock. That split is the whole point of v2: the
behaviour that matters (state machine, idempotency, rollback, reconcile,
TTL, races) lives here and can be exercised thousands of times per second
without Docker.

Ports (injected):
    store   Store            SQLite (or in-memory for the simulator)
    engine  DockerEngine     or FakeEngine with the same methods
    clock   module           clock.now_mono()/now_wall() (virtual in sim)

Concurrency contract:
    * per-sandbox locks serialize mutating operations on one sandbox
      (delete vs reaper vs stop vs informer) - both sides re-read state
      inside the lock, so racing operations are idempotent, never torn;
    * provisioning is a GENERATOR of steps (provision_steps). The driver
      (a thread in production, the scheduler in the simulator) advances it;
      a crash between steps is recovered by reconcile() on boot.
"""
import logging
import re
import secrets
import threading
import uuid

from . import clock, config, templates

log = logging.getLogger("sandbox-api.core")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}


class EngineUnavailable(ApiError):
    def __init__(self):
        super().__init__(503, "inner engine (dind) unavailable", {"Retry-After": "5"})


class Core:
    def __init__(self, store, engine, *, max_sandboxes: int = config.MAX_SANDBOXES,
                 pool_start: int = config.POOL_START, pool_end: int = config.POOL_END,
                 default_isolation: str = config.DEFAULT_ISOLATION,
                 ready_timeouts: dict | None = None,
                 id_factory=None, token_factory=None):
        self.store = store
        self.engine = engine
        self.max_sandboxes = max_sandboxes
        self.pool_start = pool_start
        self.pool_end = pool_end
        self.default_isolation = default_isolation
        # Randomness is a port too: production uses uuid4/secrets, the
        # simulator injects seeded generators so a run is fully replayable.
        self.new_id = id_factory or (lambda: uuid.uuid4().hex[:12])
        self.new_token = token_factory or (lambda: secrets.token_hex(16))
        self.ready_timeouts = ready_timeouts or {
            "web": config.READY_TIMEOUT_WEB, "coreapp": config.READY_TIMEOUT_COREAPP,
            "desktop": config.READY_TIMEOUT_DESKTOP, "default": config.READY_TIMEOUT_DEFAULT,
        }
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # True until one FULL (boot-mode) reconcile has succeeded. If the
        # engine is down at boot, the first periodic resync that reaches the
        # engine runs in boot mode instead - found by the simulator: without
        # this, rows left CREATING by a crash were never cleaned up.
        self.boot_pending = True
        # Sandbox ids whose provision generator is alive IN THIS PROCESS.
        # reconcile() rolls back CREATING rows that are NOT in here: after a
        # crash the set is empty, so every CREATING row is an orphan; while
        # running, a row owned by a live generator is never touched. (Found
        # by the simulator: a boot-mode reconcile used to roll back a create
        # that was legitimately in flight.)
        self.in_flight: set[str] = set()

    def lock_for(self, sid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.Lock())

    # -- engine guard --------------------------------------------------------------

    def _engine_down(self) -> bool:
        return not self.engine.ping()

    # -- views -----------------------------------------------------------------------

    @staticmethod
    def view(row: dict) -> dict:
        return {
            "id": row["id"], "name": row["name"], "template": row["template"],
            "state": row["state"], "port": row["port"],
            "url": (f"http://localhost:{row['port']}" if row["port"] else None),
            "ttl_seconds": row["ttl_seconds"], "error": row["error"],
            "created_at": row["created_at"], "isolation": row.get("isolation", "container"),
            "detail": row.get("detail") or {},
            "deadline_wall": row.get("deadline_wall"),
        }

    def get_or_404(self, sid: str) -> dict:
        row = self.store.get(sid)
        if not row or row["state"] == "DELETED":
            raise ApiError(404, f"sandbox {sid} not found")
        return row

    # -- create ------------------------------------------------------------------------

    def create(self, name: str, template: str, ttl_seconds: int | None,
               isolation: str | None = None, options: dict | None = None) -> tuple[str, dict]:
        """Returns ("existing"|"created", row). Raises ApiError."""
        if not NAME_RE.match(name or ""):
            raise ApiError(422, "name must match ^[a-z0-9][a-z0-9-]{1,40}$")
        if template not in templates.TEMPLATES:
            raise ApiError(422, f"unknown template; valid: {sorted(templates.TEMPLATES)}")
        isolation = isolation or self.default_isolation
        tmpl = templates.TEMPLATES[template]
        if isolation not in tmpl["isolation"]:
            raise ApiError(422, f"template {template} supports isolation {tmpl['isolation']}")
        if self._engine_down():
            raise EngineUnavailable()
        if isolation == "gvisor" and not self.engine.has_runtime("runsc"):
            raise ApiError(400, "isolation=gvisor requested but the engine has no runsc runtime")

        spec = {"template": template, "ttl_seconds": ttl_seconds, "isolation": isolation,
                **({"options": options} if options else {})}

        # edge case #1: idempotent create by name
        existing = self.store.get_by_name(name)
        if existing and existing["state"] != "DELETED":
            if existing["spec"] == spec:
                return "existing", existing
            raise ApiError(409, f"name '{name}' exists with a different spec; "
                                "delete it or pick a new name")
        self.store.free_name(name)

        # edge case #10: capacity cap = what the ENGINE holds (survives DB
        # wipes) plus rows still CREATING (their containers do not exist
        # yet). Found by the simulator: counting the engine alone let N
        # concurrent creates all pass the check and overshoot the cap.
        try:
            live = self.engine.engine_sandbox_ids()
        except Exception:
            raise EngineUnavailable()
        creating = {r["id"] for r in self.store.list_all() if r["state"] == "CREATING"}
        if len(live | creating) >= self.max_sandboxes:
            raise ApiError(429, f"capacity reached ({self.max_sandboxes} sandboxes)",
                           {"Retry-After": "30"})

        sid = self.new_id()
        now_m, now_w = clock.now_mono(), clock.now_wall()
        self.store.create({
            "id": sid, "name": name, "template": template, "spec": spec,
            "state": "CREATING", "port": None, "ttl_seconds": ttl_seconds,
            "deadline_mono": (now_m + ttl_seconds) if ttl_seconds else None,
            "deadline_wall": (now_w + ttl_seconds) if ttl_seconds else None,
            "isolation": isolation, "agent_token": self.new_token(),
        })
        self.store.add_event(sid, "state", f"CREATING (isolation={isolation})")

        if tmpl["needs_port"]:
            port = self.store.alloc_port(sid, self.pool_start, self.pool_end)
            if port is None:
                # edge case #2: port exhaustion - nothing created, nothing to roll back
                self.store.set_state(sid, "FAILED", "port pool exhausted")
                self.store.add_event(sid, "error",
                                     f"port pool {self.pool_start}-{self.pool_end} "
                                     "exhausted; nothing created")
                raise ApiError(429, "sandbox port pool exhausted", {"Retry-After": "30"})
            self.store.add_event(sid, "alloc", f"allocated port {port}")
        # Register as in flight NOW, not when the provision generator first
        # runs: a resync in the gap between this insert and the generator's
        # first step would otherwise see an "orphan" CREATING row and roll it
        # back (observed live: rollback 20 ms after creation).
        self.in_flight.add(sid)
        return "created", self.store.get(sid)

    # -- provisioning (generator of steps) -----------------------------------------

    def provision_steps(self, sid: str):
        """Yields after each engine interaction. Terminal outcome is written
        to the store (READY or FAILED); the generator itself never raises
        for provisioning failures."""
        row = self.store.get(sid)
        if not row or row["state"] != "CREATING":
            self.in_flight.discard(sid)
            return
        tmpl = templates.TEMPLATES[row["template"]]
        token = row["agent_token"]
        self.in_flight.add(sid)                      # idempotent (create() did it)

        def log_event(t, m):
            self.store.add_event(sid, t, m)

        def still_creating() -> bool:
            r = self.store.get(sid)
            return bool(r) and r["state"] == "CREATING"

        try:
            images = list(templates.resolve_images(row["template"]))
            if ((row.get("spec") or {}).get("options") or {}).get("egress"):
                images.append(templates.image_tag("sandbox-egress-proxy"))   # the policy proxy
            for image in images:
                self.engine.ensure_image(image, log_event)
                yield f"image:{image}"
                if not still_creating():
                    return                         # rolled back/deleted under us
            self.engine.create_sandbox_resources(
                sid, row["name"], row["template"], row["port"], row["isolation"],
                token, log_event, extra_files=self._extra_files(row),
                extra_env=self._extra_env(row),
                options=(row.get("spec") or {}).get("options"))
            yield "resources"
            if not still_creating():
                self.engine.destroy_sandbox_resources(sid)
                return
            timeout = self.ready_timeouts.get(tmpl["ready_timeout_key"],
                                              self.ready_timeouts["default"])
            self.engine.wait_ready(sid, row["template"], row["port"], token, timeout, log_event)
            yield "ready"
        except Exception as e:                       # edge case #3
            with self.lock_for(sid):
                fresh = self.store.get(sid)
                if fresh and fresh["state"] == "CREATING":
                    self.store.set_state(sid, "FAILED", str(e)[:500])
                    self.store.add_event(sid, "error", f"create failed: {str(e)[:300]}")
                    self.store.set_port(sid, None)   # FAILED never holds a slot
                    try:
                        self.engine.destroy_sandbox_resources(sid)
                    except Exception:
                        pass
            return
        finally:
            self.in_flight.discard(sid)
        with self.lock_for(sid):
            if self.store.set_state_if(sid, ("CREATING",), "READY"):
                self.store.add_event(sid, "state", "READY")

    # hooks for extensions (egress policy injects CA + proxy env)
    def _extra_files(self, row: dict) -> dict[str, bytes] | None:
        return None

    def _extra_env(self, row: dict) -> dict[str, str] | None:
        return None

    # -- delete / stop / start ----------------------------------------------------------

    def delete(self, sid: str, reason: str = "manual delete") -> None:
        row = self.store.get(sid)
        if not row or row["state"] == "DELETED":            # edge case #7
            raise ApiError(404, f"sandbox {sid} not found")
        if row["state"] == "CREATING":
            raise ApiError(409, "sandbox is CREATING; retry in a moment")
        with self.lock_for(sid):
            fresh = self.store.get(sid)
            if not fresh or fresh["state"] == "DELETED":
                raise ApiError(404, f"sandbox {sid} not found")
            if fresh["state"] == "CREATING":
                raise ApiError(409, "sandbox is CREATING; retry in a moment")
            self._destroy_and_mark(sid, reason)

    def _destroy_and_mark(self, sid: str, reason: str) -> None:
        """Shared teardown path for manual delete AND the TTL reaper.
        Caller holds the per-sandbox lock (edge case #9)."""
        try:
            self.engine.destroy_sandbox_resources(sid)
        except Exception as e:
            if "EngineDown" in type(e).__name__:
                raise EngineUnavailable()
            raise
        self.store.set_state(sid, "DELETED")
        self.store.add_event(sid, "state", f"DELETED ({reason})")

    def pause(self, sid: str) -> dict:
        """Freeze every process (cgroup freezer). Used by checkpoints to get
        a crash-consistent snapshot without stopping the containers."""
        self.get_or_404(sid)
        with self.lock_for(sid):
            row = self.get_or_404(sid)
            if row["state"] not in ("READY", "DEGRADED"):
                raise ApiError(409, f"cannot pause from state {row['state']}")
            try:
                self.engine.pause_sandbox(sid)
            except Exception:
                raise EngineUnavailable()
            self.store.set_state(sid, "PAUSED")
            self.store.add_event(sid, "state", "PAUSED")
        return self.store.get(sid)

    def unpause(self, sid: str) -> dict:
        self.get_or_404(sid)
        with self.lock_for(sid):
            row = self.get_or_404(sid)
            if row["state"] != "PAUSED":
                raise ApiError(409, f"cannot unpause from state {row['state']}")
            try:
                self.engine.unpause_sandbox(sid)
            except Exception:
                raise EngineUnavailable()
            self.store.set_state(sid, "READY", None, {})
            self.store.add_event(sid, "state", "READY (unpaused)")
        return self.store.get(sid)

    def stop(self, sid: str) -> dict:
        self.get_or_404(sid)
        with self.lock_for(sid):
            row = self.get_or_404(sid)
            if row["state"] == "PAUSED":
                raise ApiError(409, "sandbox is PAUSED; unpause before stopping")
            if row["state"] not in ("READY", "DEGRADED"):
                raise ApiError(409, f"cannot stop from state {row['state']}")
            try:
                self.engine.stop_sandbox(sid)
            except Exception:
                raise EngineUnavailable()
            self.store.set_state(sid, "STOPPED")
            self.store.add_event(sid, "state", "STOPPED")
        return self.store.get(sid)

    def start(self, sid: str) -> dict:
        self.get_or_404(sid)
        with self.lock_for(sid):
            row = self.get_or_404(sid)
            if row["state"] not in ("STOPPED", "DEGRADED"):
                raise ApiError(409, f"cannot start from state {row['state']}")
            try:
                self.engine.start_sandbox(sid, row.get("agent_token"))
            except Exception:
                raise EngineUnavailable()
            self.store.set_state(sid, "READY", None, {})
            self.store.add_event(sid, "state",
                                 "READY (started)" if row["state"] == "STOPPED"
                                 else "READY (healed: dead containers restarted)")
        return self.store.get(sid)

    # -- TTL reaper ----------------------------------------------------------------------

    def reap_expired(self) -> list[str]:
        """One sweep. Returns ids reaped. Safe to call concurrently with
        manual deletes (per-sandbox lock + re-read)."""
        reaped = []
        for row in self.store.expired(clock.now_mono()):
            with self.lock_for(row["id"]):
                fresh = self.store.get(row["id"])
                if not fresh or fresh["state"] in ("DELETED", "CREATING"):
                    continue
                try:
                    self.store.add_event(row["id"], "state", "TTL expired")
                    self._destroy_and_mark(row["id"], "ttl")
                    reaped.append(row["id"])
                except EngineUnavailable:
                    log.warning("reaper: engine down, retry next sweep")
        return reaped

    # -- informer hook -------------------------------------------------------------------

    def on_container_event(self, sid: str | None, action: str, cname: str | None,
                           attrs: dict) -> None:
        """Live reaction to engine events. Only READY/DEGRADED sandboxes are
        touched: stop() and delete() hold the lock while they cause die
        events, so by the time we get the lock the state already explains
        the event and we ignore it."""
        if not sid:
            return
        if action in ("die", "oom", "kill") and action != "kill":
            with self.lock_for(sid):
                row = self.store.get(sid)
                if not row or row["state"] not in ("READY", "DEGRADED"):
                    return
                code = attrs.get("exitCode", "?")
                dead = dict((row.get("detail") or {}).get("dead", {}))
                dead[cname or "?"] = f"exit={code}" + (" oom" if action == "oom" else "")
                self.store.set_state(sid, "DEGRADED", None, {"dead": dead})
                self.store.add_event(sid, "informer",
                                     f"container {cname} died ({dead[cname or '?']}) -> DEGRADED")
        elif action == "start":
            with self.lock_for(sid):
                row = self.store.get(sid)
                if not row or row["state"] != "DEGRADED":
                    return
                dead = dict((row.get("detail") or {}).get("dead", {}))
                dead.pop(cname, None)
                if dead:
                    self.store.set_detail(sid, {"dead": dead})
                else:
                    self.store.set_state(sid, "READY", None, {})
                    self.store.add_event(sid, "informer",
                                         f"container {cname} back -> READY")
        elif action == "destroy":
            # if every container of a live sandbox is gone, it was removed
            # behind our back (docker rm inside dind): reflect the truth
            with self.lock_for(sid):
                row = self.store.get(sid)
                if not row or row["state"] in ("DELETED", "CREATING"):
                    return
                try:
                    if sid in self.engine.engine_sandbox_ids():
                        return
                except Exception:
                    return
                self.store.set_state(sid, "DELETED")
                self.store.add_event(sid, "informer",
                                     "all containers removed outside the API -> DELETED")

    # -- reconciliation (boot + periodic resync) -----------------------------------------

    def reconcile(self, periodic: bool = False) -> dict:
        """Bring the DB in line with the engine.

          engine has it, DB doesn't    -> ADOPT (insert row, state from engine)
          DB has it, engine doesn't    -> mark DELETED
          row DELETED, containers left -> finish the interrupted delete
          row CREATING (boot only)     -> roll back the interrupted create
          states disagree              -> engine wins (READY/DEGRADED/STOPPED)
        Also re-anchors TTL deadlines to this process's monotonic clock.
        """
        engine_view = self.engine.engine_sandboxes()      # raises on EngineDown
        if self.boot_pending:
            periodic = False                              # boot work still owed
        db_rows = {r["id"]: r for r in self.store.list_all(include_deleted=True)}
        stats = {"adopted": 0, "vanished": 0, "finished_delete": 0,
                 "rolled_back": 0, "synced": 0, "reanchored": 0, "leftovers": 0,
                 "mode": "boot" if not periodic else "periodic"}

        for sid, sb in engine_view.items():
            running = sum(1 for c in sb["containers"] if c["status"] == "running")
            paused = sum(1 for c in sb["containers"] if c["status"] == "paused")
            total = len(sb["containers"])
            if paused and running + paused == total:
                engine_state = "PAUSED"
            elif running == total:
                engine_state = "READY"
            elif running == 0:
                engine_state = "STOPPED"
            else:
                engine_state = "DEGRADED"
            row = db_rows.get(sid)
            if row is None:
                self.store.create({
                    "id": sid, "name": sb["name"], "template": sb["template"],
                    "spec": {"adopted": True, "template": sb["template"]},
                    "state": engine_state, "port": sb["port"], "ttl_seconds": None,
                    "isolation": sb["isolation"], "agent_token": None,
                })
                self.store.add_event(sid, "reconcile", f"adopted from engine as {engine_state}")
                stats["adopted"] += 1
                continue
            with self.lock_for(sid):
                row = self.store.get(sid)
                if row["state"] == "DELETED":
                    self.engine.destroy_sandbox_resources(sid)
                    self.store.add_event(sid, "reconcile", "finished interrupted delete")
                    stats["finished_delete"] += 1
                elif row["state"] == "CREATING":
                    if sid in self.in_flight:
                        continue                          # a live generator owns it
                    self.engine.destroy_sandbox_resources(sid)
                    self.store.set_state(sid, "FAILED", "api restarted mid-create")
                    self.store.set_port(sid, None)
                    self.store.add_event(sid, "reconcile", "rolled back interrupted create")
                    stats["rolled_back"] += 1
                elif row["state"] == "FAILED":
                    # a FAILED create may have left resources behind if the
                    # engine went down mid-rollback: a FAILED row owns nothing
                    self.engine.destroy_sandbox_resources(sid)
                    self.store.add_event(sid, "reconcile",
                                         "removed leftovers of a failed create")
                    stats["leftovers"] += 1
                elif row["state"] != engine_state:
                    self.store.set_state(sid, engine_state, None,
                                         {} if engine_state == "READY" else None)
                    self.store.add_event(sid, "reconcile",
                                         f"state {row['state']} -> {engine_state}")
                    stats["synced"] += 1

        for sid, row in db_rows.items():
            if sid in engine_view or row["state"] in ("DELETED", "FAILED"):
                continue
            if row["state"] == "CREATING" and sid in self.in_flight:
                continue
            with self.lock_for(sid):
                fresh = self.store.get(sid)
                if fresh and fresh["state"] not in ("DELETED", "FAILED", "CREATING"):
                    self.store.set_state(sid, "DELETED")
                    self.store.add_event(sid, "reconcile",
                                         "containers vanished while API was down")
                    stats["vanished"] += 1
                elif fresh and fresh["state"] == "CREATING" and sid not in self.in_flight:
                    self.store.set_state(sid, "FAILED", "api restarted mid-create")
                    self.store.set_port(sid, None)
                    self.store.add_event(sid, "reconcile", "rolled back interrupted create")
                    stats["rolled_back"] += 1

        if not periodic:
            # TTLs: monotonic deadlines are per-process; re-derive from wall
            now_m, now_w = clock.now_mono(), clock.now_wall()
            for row in self.store.with_deadlines():
                remaining = row["deadline_wall"] - now_w
                self.store.set_deadline(row["id"], now_m + max(0.0, remaining),
                                        row["deadline_wall"])
                stats["reanchored"] += 1
            self.boot_pending = False
        return stats
