from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """Small in-process sliding-window limiter for the public lookup endpoint."""

    def __init__(
        self,
        *,
        request_limit: int,
        window_seconds: int,
        max_keys: int = 4096,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request_limit = max(1, int(request_limit))
        self._window_seconds = max(1, int(window_seconds))
        self._max_keys = max(32, int(max_keys))
        self._monotonic = monotonic
        self._events: dict[str, deque[float]] = {}

    def _prune(self, cutoff: float) -> None:
        for item_key, events in list(self._events.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(item_key, None)

    def _reserve_key(self, key: str, cutoff: float) -> deque[float]:
        current = self._events.get(key)
        if current is not None:
            return current
        if len(self._events) >= self._max_keys:
            self._prune(cutoff)
        if len(self._events) >= self._max_keys:
            oldest_key = min(
                self._events,
                key=lambda item_key: self._events[item_key][-1],
            )
            self._events.pop(oldest_key, None)
        events: deque[float] = deque()
        self._events[key] = events
        return events

    def allow(self, key: str) -> tuple[bool, int]:
        now = self._monotonic()
        cutoff = now - self._window_seconds
        events = self._reserve_key(str(key), cutoff)
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self._request_limit:
            retry_after = max(1, math.ceil(events[0] + self._window_seconds - now))
            return False, retry_after
        events.append(now)
        return True, 0
