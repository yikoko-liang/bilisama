"""Event-loop lag monitor: the runtime half of the blocking-call defence.

P2 is one event loop; a single synchronous slow call delays everything behind
it, barge-in handling included. The static half (ruff's ASYNC rules) catches
blocking calls written directly in async functions — anything hiding inside a
sync helper still lands here. The monitor sleeps a fixed interval and measures
how late it woke up: a stalled loop cannot wake it on time, so the overshoot
IS the stall, with a timestamp attached (plan section 16.8 item 25 — the
dev-talk blocking-audio-write incident cost a debugging session for want of
exactly this line in the log).

Deliberately not on the injected Clock: the thing under measurement is the
real event loop, and FakeClock-driven tests never run this monitor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bilisama.obs.logging import get_logger

__all__ = ["LoopLagMonitor"]

log = get_logger(__name__)


class LoopLagMonitor:
    """Samples loop wake-up lag; warns past a threshold, throttled."""

    def __init__(
        self,
        *,
        interval_s: float = 0.1,
        warn_over_s: float = 0.05,
        log_cooldown_s: float = 5.0,
    ) -> None:
        self._interval = interval_s
        self._warn_over = warn_over_s
        self._log_cooldown = log_cooldown_s
        self.max_lag_ms = 0.0
        self.warn_count = 0
        self._last_logged = float("-inf")

    async def run(self) -> None:
        """Sample forever. Cancel to stop."""
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(self._interval)
            self._note(loop.time() - before - self._interval, now=loop.time())

    def _note(self, lag_s: float, *, now: float) -> None:
        """Record one sample. Split out so the accounting is testable without
        real time."""
        lag_ms = max(0.0, lag_s * 1000.0)
        if lag_ms > self.max_lag_ms:
            self.max_lag_ms = lag_ms
        if lag_s < self._warn_over:
            return
        self.warn_count += 1
        # Every incident is counted; the log line is throttled so one long
        # stall (many consecutive late wakes) cannot flood the console.
        if now - self._last_logged >= self._log_cooldown:
            self._last_logged = now
            log.warning("loop.lag", lag_ms=round(lag_ms, 1))

    def status(self) -> dict[str, Any]:
        """The health probe's view."""
        return {"max_lag_ms": round(self.max_lag_ms, 1), "warn_count": self.warn_count}
