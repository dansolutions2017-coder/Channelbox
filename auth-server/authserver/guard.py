"""Failsafes that keep the login server from calling your IPTV servers more than needed.

- ResultCache: remembers a correct login for a while (CACHE_OK_SECONDS) and a wrong one for a
  shorter while (CACHE_FAIL_SECONDS). Logins are keyed by an HMAC fingerprint of
  username + password, with a random key made at start-up. It's kept in memory only, so no
  password (or anything that could be turned back into one) is ever stored.
- ServerGuard: per IPTV server, skips a server that just failed to answer (for SERVER_DOWN_SECONDS,
  doubling up to 10 minutes while it stays down) and caps how many checks per minute go to it
  (SERVER_RATE_PER_MIN), so the VPS can never flood a panel.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from .ratelimit import SlidingWindow


def servers_signature(servers) -> str:
    """Changes whenever the list of enabled servers (or an address) changes."""
    return hashlib.sha256("|".join(f"{s.id}={s.url}" for s in servers).encode()).hexdigest()[:16]


@dataclass
class CachedResult:
    outcome: str                    # "ok" or "invalid"
    server_id: str | None
    expires_at: str | None
    max_connections: int | None
    signature: str                  # servers_signature at the time (for "invalid")
    until: float


class ResultCache:
    def __init__(self, ok_seconds: int, fail_seconds: int):
        self.ok_seconds = ok_seconds
        self.fail_seconds = fail_seconds
        self._key = secrets.token_bytes(32)
        self._items: dict[str, CachedResult] = {}
        self._lock = threading.Lock()

    def fingerprint(self, username: str, password: str) -> str:
        return hmac.new(self._key, f"{username.lower()}\0{password}".encode(), hashlib.sha256).hexdigest()

    def get(self, fp: str) -> CachedResult | None:
        with self._lock:
            item = self._items.get(fp)
            if item and item.until > time.monotonic():
                return item
            self._items.pop(fp, None)
            return None

    def put_ok(self, fp: str, server_id: str, expires_at: str | None, max_connections: int | None) -> None:
        if self.ok_seconds <= 0:
            return
        with self._lock:
            self._items[fp] = CachedResult("ok", server_id, expires_at, max_connections, "",
                                           time.monotonic() + self.ok_seconds)

    def put_invalid(self, fp: str, signature: str) -> None:
        if self.fail_seconds <= 0:
            return
        with self._lock:
            self._items[fp] = CachedResult("invalid", None, None, None, signature,
                                           time.monotonic() + self.fail_seconds)

    def drop(self, fp: str) -> None:
        with self._lock:
            self._items.pop(fp, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._items.items() if v.until <= now]:
                del self._items[k]

    def __len__(self) -> int:
        return len(self._items)


class ServerGuard:
    MAX_DOWN_SECONDS = 600

    def __init__(self, down_seconds: int, rate_per_min: int):
        self.down_seconds = down_seconds
        self._limiter = SlidingWindow(rate_per_min, 60)
        self._down_until: dict[str, float] = {}
        self._down_streak: dict[str, int] = {}
        self._lock = threading.Lock()

    def skip_reason(self, server_id: str) -> str | None:
        """Why this server shouldn't be asked right now ("down" / "busy"), or None."""
        with self._lock:
            if self._down_until.get(server_id, 0) > time.monotonic():
                return "down"
        if self._limiter.retry_after(server_id):
            return "busy"
        return None

    def record_call(self, server_id: str) -> None:
        self._limiter.hit(server_id)

    def mark_down(self, server_id: str) -> None:
        if self.down_seconds <= 0:
            return
        with self._lock:
            streak = self._down_streak.get(server_id, 0) + 1
            self._down_streak[server_id] = streak
            wait = min(self.down_seconds * 2 ** (streak - 1), self.MAX_DOWN_SECONDS)
            self._down_until[server_id] = time.monotonic() + wait

    def mark_up(self, server_id: str) -> None:
        with self._lock:
            self._down_until.pop(server_id, None)
            self._down_streak.pop(server_id, None)

    def down_for(self, server_id: str) -> int:
        """Seconds until a 'down' server is tried again (0 = not down)."""
        with self._lock:
            return max(0, int(self._down_until.get(server_id, 0) - time.monotonic()))

    def sweep(self) -> None:
        self._limiter.sweep()
