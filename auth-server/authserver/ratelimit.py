"""In-memory sliding-window rate limits. Run the server with ONE worker so the counts are shared."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, key: str) -> int:
        """0 if allowed, otherwise seconds to wait. Doesn't record a hit."""
        if self.limit <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            self._trim(q, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def hit(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            self._trim(q, now)
            q.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def sweep(self) -> None:
        """Drops empty keys so memory doesn't grow forever."""
        now = time.monotonic()
        with self._lock:
            for key in list(self._hits):
                q = self._hits[key]
                self._trim(q, now)
                if not q:
                    del self._hits[key]
