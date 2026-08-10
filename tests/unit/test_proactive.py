"""The proactive topic loop against the L1 acceptance lines.

Plan section 9, stage 3: exactly one topic after dead air, the streamer's
voice takes the floor back instantly, a blocked floor means zero triggers.
All driven on FakeClock — the queue-hop settle fix from backlog item 6 is
what makes this loop testable at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from bilisama.clock import FakeClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent, Priority
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.store import MemoryStore
from bilisama.proactive import ProactiveTopicLoop


class FakeSide:
    def __init__(self, topic: str = "聊聊主播的新键盘") -> None:
        self.topic = topic
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        return self.topic

    async def aclose(self) -> None:
        return None


@asynccontextmanager
async def _running(
    *,
    side: FakeSide | None,
    idle_threshold_s: float = 10.0,
    max_per_hour: int = 12,
) -> AsyncIterator[tuple[ProactiveTopicLoop, SpeakingFloor, list[Intent], FakeClock]]:
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    floor = SpeakingFloor(clock)
    intents: list[Intent] = []
    loop = ProactiveTopicLoop(
        side,
        store,
        floor,
        clock,
        submit=intents.append,
        prompt="想一个话题",
        idle_threshold_s=idle_threshold_s,
        wake_interval_s=5.0,
        max_per_hour=max_per_hour,
    )
    task = asyncio.create_task(loop.run())
    try:
        yield loop, floor, intents, clock
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


async def test_dead_air_produces_exactly_one_topic() -> None:
    async with _running(side=FakeSide()) as (_loop, _floor, intents, clock):
        await clock.advance(11.0)
        assert len(intents) == 1, "one topic per idle stretch, not a monologue"

        intent = intents[0]
        assert intent.priority is Priority.PROACTIVE
        assert intent.trusted is True
        assert intent.injection.item_text is None
        assert "新键盘" in (intent.injection.reply.instructions or "")
        assert intent.expires_at is not None, "a stale topic must die in the queue"

        # More silence without a fresh idle stretch elapsing: still just one.
        await clock.advance(5.0)
        assert len(intents) == 1


async def test_a_second_idle_stretch_gets_a_second_topic() -> None:
    async with _running(side=FakeSide()) as (_loop, _floor, intents, clock):
        await clock.advance(11.0)
        assert len(intents) == 1
        await clock.advance(11.0)
        assert len(intents) == 2, "idle resets after speaking, then accrues again"


async def test_streamer_speech_resets_the_idle_clock() -> None:
    async with _running(side=FakeSide()) as (_loop, floor, intents, clock):
        await clock.advance(8.0)
        floor.on_speech_started()
        await clock.advance(6.0)  # would have crossed the threshold
        assert intents == [], "the streamer holds the floor"

        floor.on_speech_stopped(quiet_s=1.0)
        await clock.advance(5.0)
        assert intents == [], "idle restarts from the moment the floor cleared"
        await clock.advance(7.0)
        assert len(intents) == 1


async def test_a_blocked_floor_never_triggers() -> None:
    async with _running(side=FakeSide()) as (_loop, floor, intents, clock):
        floor.on_reply_active(True)
        await clock.advance(60.0)
        assert intents == [], "gate closed, zero triggers — the acceptance line"


async def test_events_count_as_activity() -> None:
    async with _running(side=FakeSide()) as (loop, _floor, intents, clock):
        for _ in range(3):
            await clock.advance(6.0)
            loop.note_activity()
        assert intents == [], "a lively room needs no topic starter"


async def test_hourly_budget_caps_topics() -> None:
    async with _running(side=FakeSide(), idle_threshold_s=2.0, max_per_hour=2) as (
        _loop,
        _floor,
        intents,
        clock,
    ):
        await clock.advance(120.0)
        assert len(intents) == 2


async def test_no_side_model_stays_silent_but_alive() -> None:
    async with _running(side=None) as (loop, _floor, intents, clock):
        await clock.advance(60.0)
        assert intents == []
        assert loop.status()["side_configured"] is False


async def test_fingerprint_saves_refresh_calls_but_speaking_forces_regeneration() -> None:
    side = FakeSide()
    async with _running(side=side, idle_threshold_s=60.0) as (loop, _floor, _intents, clock):
        await clock.advance(21.0)  # four refresh windows, unchanged input
        assert side.calls == 1, "unchanged material is one call, not four"
        loop._speak(clock.monotonic())  # consume the candidate
        await clock.advance(10.0)
        assert side.calls == 2, "after speaking, the next refresh regenerates"


async def test_status_reflects_candidate_and_budget() -> None:
    async with _running(side=FakeSide()) as (loop, _floor, _intents, clock):
        await clock.advance(6.0)
        status = loop.status()
        assert status["candidate_ready"] is True
        assert status["topics_this_hour"] == 0


@pytest.mark.parametrize("kind", [EventKind.DANMAKU])
async def test_recent_events_feed_the_candidate_material(kind: EventKind) -> None:
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.on_event(LiveEvent(kind=kind, viewer=Viewer(uid=1, name="阿强"), text="键盘怎么样"))

    class Recorder(FakeSide):
        def __init__(self) -> None:
            super().__init__()
            self.users: list[str] = []

        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            self.users.append(user)
            return await super().complete(system=system, user=user, max_tokens=max_tokens)

    side = Recorder()
    loop = ProactiveTopicLoop(
        side,
        store,
        SpeakingFloor(clock),
        clock,
        submit=lambda _i: None,
        prompt="想一个话题",
        idle_threshold_s=99.0,
    )
    task = asyncio.create_task(loop.run())
    try:
        await clock.advance(2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()

    assert side.users and "键盘怎么样" in side.users[0]
