"""FakeEngine - a model of the inner Docker engine, with faults.

It implements exactly the methods the lifecycle core calls on DockerEngine
(see sandbox-api/api/app/engine.py) and nothing else. State is a dict of
sandboxes -> containers so invariants can inspect "engine truth" directly.

Fault knobs (all driven by the scenario's seeded RNG):
  down            engine unreachable: every call raises FakeEngineDown
  fail_create_at  the Nth container create of the next sandbox raises
  slow            readiness wait takes virtual time
  kill(sid, name) a container dies (what `docker kill` inside dind does)
  remove(sid)     containers vanish out-of-band (what `docker rm -f` does)
Each fault produces the same engine events the real daemon would, so the
informer path (Core.on_container_event) is exercised too.
"""
from app import templates


class FakeEngineDown(Exception):
    pass


# the core detects EngineDown by class name ("EngineDown" in type name)
FakeEngineDown.__name__ = "EngineDown"


class FakeEngine:
    def __init__(self, clock, rng, runtimes=("runc", "runsc")):
        self.clock = clock
        self.rng = rng
        self.runtimes = set(runtimes)
        self.sandboxes: dict[str, dict] = {}      # sid -> {name, template, port, isolation, containers:{name:status}}
        self.images: set[str] = set()
        self.down = False
        self.fail_create_at: int | None = None
        self.create_latency = 0.0
        self.events: list[tuple] = []             # pending informer events
        self.calls = 0

    # -- faults -------------------------------------------------------------------

    def _guard(self):
        self.calls += 1
        if self.down:
            raise FakeEngineDown("fake engine down")

    def kill(self, sid: str, cname: str, oom: bool = False) -> None:
        sb = self.sandboxes.get(sid)
        if not sb or sb["containers"].get(cname) != "running":
            return
        sb["containers"][cname] = "exited"
        self.events.append(("oom" if oom else "die", sid, cname, {"exitCode": "137", "name": cname}))

    def remove_out_of_band(self, sid: str) -> None:
        sb = self.sandboxes.pop(sid, None)
        if sb:
            for cname in sb["containers"]:
                self.events.append(("destroy", sid, cname, {"name": cname}))

    def drain_events(self) -> list[tuple]:
        ev, self.events = self.events, []
        return ev

    # -- DockerEngine surface --------------------------------------------------------

    def ping(self) -> bool:
        return not self.down

    def info(self) -> dict:
        self._guard()
        return {"runtimes": sorted(self.runtimes)}

    def has_runtime(self, name: str) -> bool:
        return not self.down and name in self.runtimes

    def ensure_image(self, image: str, log_event) -> None:
        self._guard()
        if image not in self.images:
            log_event("pull", f"(fake) pulled {image}")
            self.images.add(image)

    def create_sandbox_resources(self, sid, name, template, port, isolation, token,
                                 log_event, extra_files=None, extra_env=None) -> None:
        self._guard()
        spec = templates.TEMPLATES[template]["spec"](sid, name, port)
        sb = {"name": name, "template": template, "port": port,
              "isolation": isolation, "containers": {}}
        self.sandboxes[sid] = sb
        for i, cspec in enumerate(spec["containers"], start=1):
            if self.fail_create_at == i:
                self.fail_create_at = None
                # rollback exactly like the real engine: everything so far removed
                del self.sandboxes[sid]
                raise RuntimeError(f"(fake) container {cspec['name']} failed to create")
            sb["containers"][cspec["name"]] = "running"
            log_event("create", f"container {cspec['name']} started")
        if self.create_latency:
            self.clock.advance(self.create_latency)

    def wait_ready(self, sid, template, port, token, timeout, log_event=None) -> None:
        self._guard()
        if sid not in self.sandboxes:
            raise RuntimeError("(fake) sandbox vanished before ready")
        if log_event:
            log_event("agent", "all agents answering")

    def destroy_sandbox_resources(self, sid: str) -> None:
        self._guard()
        self.sandboxes.pop(sid, None)

    def pause_sandbox(self, sid: str) -> None:
        self._guard()
        sb = self.sandboxes.get(sid)
        if sb:
            for c in sb["containers"]:
                if sb["containers"][c] == "running":
                    sb["containers"][c] = "paused"

    def unpause_sandbox(self, sid: str) -> None:
        self._guard()
        sb = self.sandboxes.get(sid)
        if sb:
            for c in sb["containers"]:
                if sb["containers"][c] == "paused":
                    sb["containers"][c] = "running"

    def stop_sandbox(self, sid: str) -> None:
        self._guard()
        sb = self.sandboxes.get(sid)
        if sb:
            for c in sb["containers"]:
                if sb["containers"][c] == "running":
                    sb["containers"][c] = "exited"
                    # real dockerd emits a `die` for every stopped container;
                    # the informer must recognise these as explained by stop()
                    self.events.append(("die", sid, c, {"exitCode": "143", "name": c}))

    def start_sandbox(self, sid: str, token=None) -> None:
        self._guard()
        sb = self.sandboxes.get(sid)
        if sb:
            for c in sb["containers"]:
                if sb["containers"][c] != "running":
                    sb["containers"][c] = "running"
                    self.events.append(("start", sid, c, {"name": c}))

    def engine_sandbox_ids(self) -> set[str]:
        self._guard()
        return set(self.sandboxes)

    def engine_port_map(self) -> dict[str, int]:
        self._guard()
        return {sid: sb["port"] for sid, sb in self.sandboxes.items() if sb["port"]}

    def engine_sandboxes(self) -> dict[str, dict]:
        self._guard()
        return {
            sid: {"name": sb["name"], "template": sb["template"], "isolation": sb["isolation"],
                  "port": sb["port"],
                  "containers": [{"name": n, "status": st, "role": "?"}
                                 for n, st in sb["containers"].items()]}
            for sid, sb in self.sandboxes.items()
        }

    def sandbox_containers(self, sid: str) -> list[dict]:
        self._guard()
        sb = self.sandboxes.get(sid, {"containers": {}})
        return [{"name": n, "status": st, "ip": "10.0.0.1", "role": "?"}
                for n, st in sb["containers"].items()]

    def events_stream(self):
        raise NotImplementedError("the scenario pumps drain_events() instead")
