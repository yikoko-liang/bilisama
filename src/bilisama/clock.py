"""Injectable clock.

Windows, cooldowns and grace periods are all time-driven, so tests need to hold
time still. A 15-line protocol does that without freezegun having to patch the
event loop's time source.

Keep the two clocks apart: `monotonic()` measures intervals and is immune to
system clock changes, `wall()` is what humans and stored records see.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """What production code depends on. Never `time.monotonic()` directly."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin. For intervals only — never format it."""
        ...

    def wall(self) -> datetime:
        """Timezone-aware wall time, for memory rows, logs and subtitles."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real one."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Time only moves when a test calls `advance()`.

    `sleep()` registers a wake-up point instead of waiting, so a test covering a
    20-second danmaku window finishes in milliseconds and does so deterministically.
    """

    __slots__ = ("_now", "_waiters", "_wall")

    def __init__(self, start: float = 0.0, wall: datetime | None = None) -> None:
        self._now = start
        self._wall = wall or datetime(2026, 1, 1, tzinfo=UTC)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    def wall(self) -> datetime:
        return self._wall + timedelta(seconds=self._now)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking sleepers as their deadlines pass.

        Wakes them one at a time and yields in between, so each woken coroutine
        gets to run up to its next await before the clock moves again. Waking them
        all at once would let a later sleeper observe a time it should not see yet.
        """
        target = self._now + seconds
        while True:
            due = [(t, f) for t, f in self._waiters if t <= target and not f.done()]
            if not due:
                break
            due.sort(key=lambda pair: pair[0])
            when, fut = due[0]
            self._now = when
            self._waiters.remove((when, fut))
            fut.set_result(None)
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)
