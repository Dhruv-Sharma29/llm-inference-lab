"""Per-API-key token bucket (requests per second with a burst allowance)."""

from __future__ import annotations

import time

from .errors import GatewayError


class QuotaLimiter:
    def __init__(self, rate_per_s: float, burst: int, clock=time.monotonic):
        self.rate = rate_per_s
        self.burst = burst
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key_id -> (tokens, last_refill)

    def check(self, key_id: str) -> None:
        if self.rate <= 0:
            return
        now = self._clock()
        tokens, last = self._buckets.get(key_id, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate)
        if tokens < 1.0:
            self._buckets[key_id] = (tokens, now)
            raise GatewayError(429, "rate_limited", "Per-key request quota exceeded.",
                               retry_after_s=(1.0 - tokens) / self.rate)
        self._buckets[key_id] = (tokens - 1.0, now)
