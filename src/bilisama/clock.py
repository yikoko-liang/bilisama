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

        Wakes them one at a time and yields in between, so each woken coroutine
        gets to run up to its next await before the clock moves again. Waking them
        all at once would let a later sleeper observe a time it should not see yet.

        A woken sleeper that sleeps again straight away is picked up by this same
        call. One that awaits anything else in between is not — see the KNOWN
        BROKEN note below.

        Raises:
            ValueError: if `seconds` is negative.
        """
        if seconds < 0:
            # Rewinding a monotonic clock hides the caller's arithmetic bug behind a
            # cooldown that never expires. Making it visible is the point of the fake.
            raise ValueError("advance() only moves time forward")
        # KNOWN BROKEN, no backlog entry yet: the `await asyncio.sleep(0)` below buys
        # the woken coroutine exactly one turn. If it awaits anything before its next
        # clock.sleep(), this loop finds nothing due, breaks, and jumps straight to
        # target — the very time jump the docstring above promises not to make. A
        # `await clock.sleep(w); await queue.get()` loop hits it immediately. Pinned by
        # test_a_resleep_behind_another_await_is_missed in tests/unit/test_clock.py.
        # asyncio has no public "loop is quiescent" hook (trio's MockClock autojump
        # does), so the fix is a design decision, not a one-liner.
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
