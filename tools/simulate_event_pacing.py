"""Replay synthetic room loads through the shipped live-event pacing policy.

This is a product-level capacity probe, not a model-quality evaluation. It
counts how many ordinary danmaku/proactive reply opportunities survive the
dynamic selector window and event budget. Paid and VIP events are intentionally
absent because they bypass the ordinary budget by design.

Usage:
    .venv/bin/python tools/simulate_event_pacing.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bilisama.config.enums import Chattiness
from bilisama.event_pacing import EventPacer
from bilisama.ingest.events import EventKind, LiveEvent, Viewer


@dataclass(slots=True)
class SimulationClock:
    """Small deterministic clock implementing the production Clock protocol."""

    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return datetime(2026, 8, 27, tzinfo=UTC) + timedelta(seconds=self.now)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("simulation time cannot move backwards")
        self.now += seconds


@dataclass(frozen=True, slots=True)
class SimulationResult:
    chattiness: Chattiness
    room: str
    danmaku_per_min: int
    ending_activity: str
    danmaku_replies: int
    proactive_topics: int

    @property
    def total_replies(self) -> int:
        return self.danmaku_replies + self.proactive_topics


ROOM_LOADS = {
    "无人": 0,
    "冷清": 2,
    "稀疏": 6,
    "活跃": 18,
    "繁忙": 60,
}

_TICK_S = 0.25


def simulate(
    chattiness: Chattiness,
    room: str,
    danmaku_per_min: int,
    *,
    duration_s: float = 180.0,
) -> SimulationResult:
    """Run one three-minute synthetic room through the effective policy."""
    clock = SimulationClock()
    pacer = EventPacer(clock, chattiness=lambda: chattiness)
    interval_s = 60.0 / danmaku_per_min if danmaku_per_min else float("inf")
    next_event_at = 0.0 if danmaku_per_min else float("inf")
    window_opened: float | None = None
    window_s = 0.0
    last_live_activity = 0.0
    danmaku_replies = 0
    proactive_topics = 0
    sequence = 0

    while clock.monotonic() < duration_s:
        now = clock.monotonic()
        while next_event_at <= now:
            sequence += 1
            event = LiveEvent(
                kind=EventKind.DANMAKU,
                room_id=1,
                viewer=Viewer(uid=sequence, name=f"观众{sequence}"),
                text=f"第 {sequence} 个有效问题",
                recv_at=now,
            )
            pacer.note_event(event)
            last_live_activity = now
            if window_opened is None:
                window_opened = now
                window_s = pacer.snapshot().danmaku_window_s
            next_event_at += interval_s

        if window_opened is not None and now - window_opened >= window_s:
            if pacer.try_consume("danmaku"):
                danmaku_replies += 1
            window_opened = None

        policy = pacer.snapshot()
        if (
            window_opened is None
            and policy.proactive_enabled
            and now - last_live_activity >= policy.proactive_idle_s
            and pacer.try_consume("proactive")
        ):
            proactive_topics += 1
            last_live_activity = now

        clock.advance(_TICK_S)

    return SimulationResult(
        chattiness=chattiness,
        room=room,
        danmaku_per_min=danmaku_per_min,
        ending_activity=pacer.snapshot().activity.value,
        danmaku_replies=danmaku_replies,
        proactive_topics=proactive_topics,
    )


def matrix(*, duration_s: float = 180.0) -> list[SimulationResult]:
    """Return Low/Medium/High crossed with all shipped activity fixtures."""
    return [
        simulate(level, room, rate, duration_s=duration_s)
        for level in Chattiness
        for room, rate in ROOM_LOADS.items()
    ]


def main() -> None:
    print("3 分钟直播事件节奏模拟（数字=弹幕回复+主动话题）")
    print("话痨\t场景\t弹幕/分\t结束状态\t弹幕回复\t主动话题\t合计")
    for row in matrix():
        print(
            f"{row.chattiness.value}\t{row.room}\t{row.danmaku_per_min}\t"
            f"{row.ending_activity}\t{row.danmaku_replies}\t"
            f"{row.proactive_topics}\t{row.total_replies}"
        )


if __name__ == "__main__":
    main()
