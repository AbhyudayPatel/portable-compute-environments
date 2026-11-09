"""The informer: watch the engine, don't poll it.

Kubernetes calls this pattern an informer - a long-lived watch on the
source of truth plus a periodic full resync to repair anything the watch
missed. Here:

  * `watch_loop` subscribes to dockerd's /events (filtered to managed
    containers) and hands every die/oom/start/destroy to Core.on_container_event,
    which flips READY <-> DEGRADED and notices out-of-band removals live;
  * `resync_loop` runs Core.reconcile(periodic=True) every RESYNC_INTERVAL
    seconds, covering dind restarts and dropped events;
  * `reaper_loop` sweeps TTL deadlines.

All three are plain threads that call into the core; the core itself never
spawns anything (so the simulator can drive the same code single-threaded).
"""
import logging
import threading

from . import clock, config
from .core import Core
from .engine import EngineDown

log = logging.getLogger("sandbox-api.informer")


class Informer:
    def __init__(self, core: Core):
        self.core = core
        self.connected = False
        self.events_seen = 0
        self.resyncs = 0
        self.last_resync: dict | None = None

    def start(self) -> None:
        for fn in (self.watch_loop, self.resync_loop, self.reaper_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def watch_loop(self) -> None:
        if not config.INFORMER_ENABLED:
            return
        while True:
            try:
                self.connected = True
                for action, sid, cname, attrs in self.core.engine.events():
                    self.events_seen += 1
                    try:
                        self.core.on_container_event(sid, action, cname, attrs)
                    except Exception:
                        log.exception("informer: handling %s for %s failed", action, sid)
            except EngineDown as e:
                self.connected = False
                log.warning("informer: event stream down (%s); retrying", e)
            except Exception:
                self.connected = False
                log.exception("informer: event stream failed; retrying")
            clock.sleep(3)

    def resync_loop(self) -> None:
        while True:
            clock.sleep(config.RESYNC_INTERVAL)
            try:
                self.last_resync = self.core.reconcile(periodic=True)
                self.resyncs += 1
            except EngineDown:
                log.warning("resync: engine down, skipping")
            except Exception:
                log.exception("resync failed")

    def reaper_loop(self) -> None:
        while True:
            clock.sleep(config.REAPER_INTERVAL)
            try:
                self.core.reap_expired()
            except Exception:
                log.exception("reaper sweep failed")

    def status(self) -> dict:
        return {"connected": self.connected, "events_seen": self.events_seen,
                "resyncs": self.resyncs, "last_resync": self.last_resync}
