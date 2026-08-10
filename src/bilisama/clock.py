"""Injectable clock.

Windows, cooldowns and grace periods are all time-driven, so tests need to hold
time still. A 15-line protocol does that without freezegun having to patch the
event loop's time source.

Keep the two clocks apart: `monotonic()` measures intervals and is immune to
system clock changes, `wall()` is what humans and stored records see.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

# How many loop turns advance() grants a woken coroutine to reach its next
# clock.sleep() through intermediate awaits. See FakeClock.advance.
_SETTLE_TURNS = 32


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
            # asyncio.sleep does exactly this for <= 0: one turn of the loop and no
            # more (CPython 3.12.13 asyncio/tasks.py:655-657, `await __sleep0()`).
            # Callers that write `await clock.sleep(0)` to hand control over must
            # behave the same under both clocks.
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        waiter = (self._now + seconds, fut)
        self._waiters.append(waiter)
        try:
            await fut
        finally:
            # A cancelled sleep would otherwise leave its deadline behind forever:
            # advance() skips done futures but never drops them. asyncio.sleep drops
            # its own pending wakeup the same way (CPython 3.12.13
            # asyncio/tasks.py:664-667). On the normal wake path advance() has
            # already removed the tuple, hence the suppress.
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)

    async def advance(self, seconds: float) -> None:
        """Move time forward, waking sleepers as their deadlines pass.

        Wakes them one at a time, in deadline order, moving the clock to each
        deadline first and yielding right after — a woken coroutine runs at its
        own deadline and never observes a time past it.

        When nothing is due, the loop gets a bounded settle window before this
        call gives up: a woken coroutine may cross up to _SETTLE_TURNS
        intermediate awaits — a queue hop, an event, a lock — on its way to the
        next clock.sleep(), and the re-registered deadline is still honoured by
        this same call. asyncio has no public "loop is quiescent" hook (trio's
        MockClock autojumps on one), so a bounded window is the design decision:
        a chain longer than _SETTLE_TURNS awaits is jumped past, and that
        boundary is pinned in tests/unit/test_clock.py.

        Raises:
            ValueError: if `seconds` is negative.
        """
        if seconds < 0:
            # Rewinding a monotonic clock hides the caller's arithmetic bug behind a
            # cooldown that never expires. Making it visible is the point of the fake.
            raise ValueError("advance() only moves time forward")
        target = self._now + seconds
        while True:
            due = [(t, f) for t, f in self._waiters if t <= target and not f.done()]
            if due:
                due.sort(key=lambda pair: pair[0])
                when, fut = due[0]
                self._now = when
                self._waiters.remove((when, fut))
                fut.set_result(None)
                # The woken coroutine gets its turn now, at its own deadline,
                # before any later sleeper can move the clock.
                await asyncio.sleep(0)
                continue
            if not await self._settle(target):
                break
        self._now = target
        await asyncio.sleep(0)

    async def _settle(self, target: float) -> bool:
        """Yield up to _SETTLE_TURNS loop turns; True once a due waiter appears."""
        for _ in range(_SETTLE_TURNS):
            await asyncio.sleep(0)
            if any(t <= target and not f.done() for t, f in self._waiters):
                return True
        return False
