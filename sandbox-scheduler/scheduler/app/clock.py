"""Injectable clock (same pattern as sandbox-api/app/clock.py)."""
import time


class SystemClock:
    def monotonic(self): return time.monotonic()
    def time(self): return time.time()
    def sleep(self, s): time.sleep(s)


impl = SystemClock()


def now_mono() -> float:
    return impl.monotonic()


def now_wall() -> float:
    return impl.time()


def sleep(s: float) -> None:
    impl.sleep(s)
