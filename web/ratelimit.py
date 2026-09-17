"""Tiny in-memory sliding-window rate limiter (stdlib only).

Used to throttle login attempts (brute-force defence) and the chat endpoint
(runaway / abuse control). State is per-process, which is correct for the current
single-worker deployment; a multi-worker setup would move this to a shared store
(Redis), like the other single-worker caveats in the README.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_events: int, window_seconds: float):
        self.max = max(1, int(max_events))
        self.window = float(window_seconds)
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, float]:
        """Record one event for `key`. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._events[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                return False, max(0.0, dq[0] + self.window - now)
            dq.append(now)
            if len(self._events) > 4096:           # opportunistic cleanup
                for k in [k for k, v in self._events.items() if not v]:
                    self._events.pop(k, None)
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)
