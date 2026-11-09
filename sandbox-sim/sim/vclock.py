"""A virtual clock for deterministic simulation.

The control plane reads time only through `app.clock` (now_mono, now_wall,
sleep). The simulator installs this object there. Time never passes on its
own; the scenario driver advances it explicitly (`advance(seconds)`), so a
run with the same seed always sees the same timestamps, the same TTL
expiries, the same aging - and the same bugs.

`sleep()` is a no-op that records the request: the core never sleeps
(only production drivers do), so a sleep in the core would be a design
violation worth seeing in the log.
"""


class VirtualClock:
    def __init__(self, start_mono: float = 1000.0, start_wall: float = 1_700_000_000.0):
        self._mono = start_mono
        self._wall = start_wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += seconds

    def skew_wall(self, seconds: float) -> None:
        """Wall clock jumps (NTP step) while monotonic keeps ticking."""
        self._wall += seconds
