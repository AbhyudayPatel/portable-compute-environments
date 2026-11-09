"""In-process event bus.

The store appends an event to SQLite and then publishes it here, so live
subscribers (server-sent event streams in main.py) see it immediately
instead of polling. Subscribers that fell behind or just connected catch
up from the store first, then attach - the `cursor` (global rowid) and the
per-sandbox `seq` make that gap-free.

Thread model: subscribers hold a bounded queue; a slow subscriber drops
the OLDEST pending notification (it will re-read from the store using its
cursor), never blocks the publisher.
"""
import queue
import threading


class Bus:
    def __init__(self, maxsize: int = 1000):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()           # drop oldest, keep newest
                    q.put_nowait(event)
                except queue.Empty:
                    pass


bus = Bus()
