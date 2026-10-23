"""Bounded admission: at most `max_inflight` running requests and `max_queue` waiting ones.

A request that would exceed the wait queue is rejected immediately instead of growing memory
and latency without bound. A waiting request gives up after `queue_timeout_s`.
"""

from __future__ import annotations

import asyncio

from .errors import GatewayError


class Ticket:
    """A held in-flight slot. `release()` is idempotent so every exit path can call it."""

    def __init__(self, controller: AdmissionController):
        self._controller = controller
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._controller._release()


class AdmissionController:
    def __init__(self, max_inflight: int, max_queue: int):
        self.max_inflight = max_inflight
        self.max_queue = max_queue
        self._sem = asyncio.Semaphore(max_inflight)
        self.waiting = 0
        self.inflight = 0

    async def acquire(self, timeout_s: float) -> Ticket:
        if self._sem.locked() and self.waiting >= self.max_queue:
            raise GatewayError(429, "queue_full", "Server is at capacity; retry later.", retry_after_s=1)
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout_s)
        except TimeoutError:
            raise GatewayError(
                503, "queue_timeout", f"Waited {timeout_s:g}s for capacity; retry later.", retry_after_s=1
            ) from None
        finally:
            self.waiting -= 1
        self.inflight += 1
        return Ticket(self)

    def _release(self) -> None:
        self.inflight -= 1
        self._sem.release()
