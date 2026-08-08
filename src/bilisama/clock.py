"""时钟抽象。

窗口、冷却、宽限期全是时间驱动的，测试里必须能把时间捏在手里。
用一个 15 行的协议换掉 freezegun 在 asyncio 上要处理的事件循环时间源。

单调时钟用于测量间隔，墙钟只用于写进记忆和日志的时间戳。两者不要混用：
`monotonic()` 不受系统对时影响，`wall()` 才是人看的时间。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """注入式时钟。生产代码只依赖这个协议。"""

    def monotonic(self) -> float:
        """单调秒。只用来算间隔，不要格式化给人看。"""
        ...

    def wall(self) -> datetime:
        """带时区的墙钟时间。用于记忆、日志、字幕时间戳。"""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """生产用的实现。"""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """测试用。时间只在 advance() 时前进。

    sleep() 不会真的等待：它注册一个唤醒点，等 advance() 走到那个时刻才放行。
    这样一个跑 20 秒窗口的测试可以在毫秒内跑完，而且是确定性的。
    """

    __slots__ = ("_now", "_waiters", "_wall")

    def __init__(self, start: float = 0.0, wall: datetime | None = None) -> None:
        self._now = start
        self._wall = wall or datetime(2026, 1, 1, tzinfo=UTC)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    def wall(self) -> datetime:
        from datetime import timedelta

        return self._wall + timedelta(seconds=self._now)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    async def advance(self, seconds: float) -> None:
        """把时间往前推，唤醒到期的 sleep，并把控制权让给事件循环。"""
        target = self._now + seconds
        while True:
            due = [(t, f) for t, f in self._waiters if t <= target and not f.done()]
            if not due:
                break
            # 按到期顺序逐个唤醒，中间让出控制权，保证被唤醒的协程能跑到下一个 await
            due.sort(key=lambda p: p[0])
            when, fut = due[0]
            self._now = when
            self._waiters.remove((when, fut))
            fut.set_result(None)
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)
