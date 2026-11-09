"""The clock port.

Every read of time in the control plane goes through `clock.now_mono()` /
`clock.now_wall()` and every pause through `clock.sleep()`. In production
this is the system clock. In the deterministic simulator (../sandbox-sim)
it is a virtual clock that only advances when the simulator says so -
which is what makes 10,000 randomized runs reproducible from one seed.
"""
import time


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# The swap point. Production never touches it; the simulator assigns a
# VirtualClock before constructing the core.
impl = SystemClock()


def now_mono() -> float:
    return impl.monotonic()


def now_wall() -> float:
    return impl.time()


def sleep(seconds: float) -> None:
    impl.sleep(seconds)
