"""Sliding-window rate limiting for the one endpoint that costs money.

Creating a shipment is the only request that spends real credits: each one fans
out to a vision model per document. Reading a report is free, so limiting it
would only frustrate the browser that is polling for progress.

The window is per client, kept in this process. That is deliberate: a single
worker with a filesystem store is the deployment this app targets, and a
distributed limiter would be a dependency bought for a scale the design does not
have. Behind a reverse proxy, put the limiter there instead and leave this at 0.
"""

from __future__ import annotations

import time
from collections import defaultdict


class RateLimiter:
    """Allow `limit` events per `window_seconds` per key. limit <= 0 disables it.

    `clock` is injectable so the window can be tested by moving time forward
    rather than by sleeping. A wall-clock test with a short window passes on an
    idle machine and fails under suite load, which measures the scheduler and not
    the limiter. Production callers never pass it.
    """

    def __init__(self, limit: int = 30, window_seconds: float = 60.0,
                 clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._events: dict[str, list[float]] = defaultdict(list)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds_until_a_slot_frees). Never accumulates state for a
        disabled limiter, and prunes as it goes so an idle key costs nothing."""
        if self.limit <= 0:
            return True, 0.0
        now = self._clock()
        bucket = self._events[key]
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.pop(0)
        if len(bucket) >= self.limit:
            retry = self.window - (now - bucket[0])
            return False, max(retry, 0.0)
        bucket.append(now)
        return True, 0.0

    def reset(self) -> None:
        self._events.clear()
