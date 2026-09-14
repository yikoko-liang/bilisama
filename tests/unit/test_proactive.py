"""The proactive topic loop against the L1 acceptance lines.

Plan section 9, stage 3: exactly one topic after dead air, the streamer's
voice takes the floor back instantly, a blocked floor means zero triggers.
All driven on FakeClock — the queue-hop settle fix from backlog item 6 is
what makes this loop testable at all.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from bilisama.clock import FakeClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent, Priority
from bilisama.event_pacing import EventPacer
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.store import MemoryStore
from bilisama.obs.logging import setup
from bilisama.proactive import ProactiveTopicLoop

_QUIETED = ("websockets", "asyncio", "aiohttp", "httpx", "uvicorn")


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """The real setup()/formatter, writing into memory.

    Through the formatter rather than caplog because the property under test is
    what comes out the far end: the candidate was written by a model that READ
    audience danmaku, and only the formatter's scrubber folds it. A LogRecord
    still holds the raw string. setup() clears the root handlers
    (obs/logging.py:318), pytest's own included, so this puts them back.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quieted = {name: logging.getLogger(name).level for name in _QUIETED}
    stream = io.StringIO()
    setup(level="info", stream=stream)
    try:
        yield stream
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, saved in quieted.items():
            logging.getLogger(name).setLevel(saved)


def _lines(stream: io.StringIO, event: str) -> list[dict[str, Any]]:
    parsed = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [line for line in parsed if line["event"] == event]


class FakeSide:
    def __init__(self, topic: str = "聊聊主播的新键盘") -> None:
        self.topic = topic
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        return self.topic

    async def aclose(self) -> None:
        return None


async def test_replay_candidate_reads_only_this_case_and_clears_old_candidate() -> None:
    class RecordingSide(FakeSide):
        def __init__(self) -> None:
            super().__init__()
            self.inputs: list[str] = []

        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            self.inputs.append(user)
            return "本轮候选话题"

    side = RecordingSide()
    async with _running(side=side) as (loop, _floor, _intents, _clock):
        loop._store.replace_facts("stream", str(loop._store.stream_id), [("旧会话记忆", "")])
        loop.note_dialogue("streamer", "旧主播语音")
        loop._candidate = "旧候选"
        await loop.reset_for_replay("本轮背景：本地工具")
        assert not loop.status()["candidate_ready"]
        loop.note_dialogue("streamer", "本轮口述")
        loop.note_replay_event(
            LiveEvent(
                kind=EventKind.DANMAKU, viewer=Viewer(uid=8, name="阿强"), text="本轮观众问题"
            )
        )
        await loop._refresh()
        assert "本轮背景" in side.inputs[-1]
        assert "本轮口述" in side.inputs[-1]
        assert "本轮观众问题" in side.inputs[-1]
        assert "旧会话记忆" not in side.inputs[-1]
        assert "旧主播语音" not in side.inputs[-1]
        await loop.reset_for_replay("下一例背景")
        await loop._refresh()
        assert "下一例背景" in side.inputs[-1]
        assert "本轮口述" not in side.inputs[-1]
        assert "本地工具" not in side.inputs[-1]
        assert "本轮观众问题" not in side.inputs[-1]
        await loop.reset_for_replay(None)
        await loop._refresh()
        assert "旧会话记忆" in side.inputs[-1]


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


async def test_candidate_is_flattened_neutralized_and_capped() -> None:
    """A14's second-order channel: the candidate came out of a model that READ
    audience danmaku. Whitespace must flatten (no fake prompt structure),
    wrapper tokens must break, and the length is capped at 80."""
    dirty = "带  bilisama_live_events  标记\t的话题" + "废" * 100
    async with _running(side=FakeSide(topic=dirty)) as (_loop, _floor, intents, clock):
        await clock.advance(11.0)
        assert len(intents) == 1
        instructions = intents[0].injection.reply.instructions or ""
        assert "bilisama_live_events" not in instructions
        assert "bilisama·live·events" in instructions
        topic_line = next(line for line in instructions.splitlines() if "候选话题是：" in line)
        candidate = topic_line.split("候选话题是：", 1)[1]
        assert "  " not in candidate, "whitespace runs must flatten"
        assert len(candidate) <= 80, "the 80-char cap must hold"


async def test_dead_air_produces_exactly_one_topic() -> None:
    async with _running(side=FakeSide()) as (_loop, _floor, intents, clock):
        await clock.advance(11.0)
        assert len(intents) == 1, "one topic per idle stretch, not a monologue"

        intent = intents[0]
        assert intent.priority is Priority.PROACTIVE
        assert intent.trusted is True
        # Ledger #56: this used to assert item_text is None, which pinned the
        # bug as expected behaviour. DashScope refuses a response.create on a
        # conversation with no user message — probed live 2026-08-24,
        # out-of-band included — so a topic that injects nothing could never
        # open a fresh session there. Plan section 4.5 always said every
        # proactive opening enters as a synthesized user item.
        assert intent.injection.item_text, "主动话题什么都不写，DashScope 上开场必被拒"
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


async def test_a_topic_is_logged_ready_then_submitted_without_its_text(
    log_stream: io.StringIO,
) -> None:
    """「她怎么从来不主动说话」得能分成两段来查。

    没有 topic_ready 就是侧路模型没给出候选；有 ready 没 submitted 就是场子
    一直没静到阈值。至于话题正文——它是侧路模型读着观众弹幕写出来的，属于观众
    的二手内容，只以字数进日志。
    """
    async with _running(side=FakeSide(topic="聊聊主播的新键盘")) as (
        _loop,
        _floor,
        intents,
        clock,
    ):
        await clock.advance(11.0)
        assert len(intents) == 1

    ready = _lines(log_stream, "proactive.topic_ready")
    submitted = _lines(log_stream, "proactive.topic_submitted")
    # 说完一次会清指纹，下一轮刷新必然重新生成一条候选（见上面那条指纹用例），
    # 所以 ready 只保证不少于一条。
    assert len(ready) >= 1 and len(submitted) == 1
    assert submitted[0]["topic_text"] == "<8 chars>", "正文只能是个长度"
    assert "新键盘" not in log_stream.getvalue(), "观众二手内容一个字都不许落盘"
    assert submitted[0]["idle_s"] >= 10.0, "冷场了多久，是这条唯一说得清的事"
    assert submitted[0]["topics_this_hour"] == 1


async def test_a_spent_hourly_budget_says_so_once_not_once_a_second(
    log_stream: io.StringIO,
) -> None:
    """闸门是个状态，不是个事件——每秒复查一次，日志不能跟着响一秒一条。"""
    async with _running(side=FakeSide(), idle_threshold_s=2.0, max_per_hour=1) as (
        _loop,
        _floor,
        intents,
        clock,
    ):
        await clock.advance(60.0)
        assert len(intents) == 1, "配额就是一条"

    blocked = _lines(log_stream, "proactive.budget_exhausted")
    assert len(blocked) == 1, f"翻转一次记一条，实际记了 {len(blocked)} 条"
    assert blocked[0]["topics_this_hour"] == 1
    assert blocked[0]["max_per_hour"] == 1


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


async def test_spoken_topic_does_not_feed_the_same_danmaku_to_the_next_candidate() -> None:
    """A proactive opening must consume the event material it just used."""
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=1,
        event_id="old-question",
        viewer=Viewer(uid=1, name="阿强"),
        text="旧问题不该再次成为主动话题",
    )
    store.on_event(event)

    class RecordingSide(FakeSide):
        def __init__(self) -> None:
            super().__init__(topic="聊聊旧问题")
            self.users: list[str] = []

        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            self.users.append(user)
            return await super().complete(system=system, user=user, max_tokens=max_tokens)

    side = RecordingSide()
    loop = ProactiveTopicLoop(
        side,
        store,
        SpeakingFloor(clock),
        clock,
        submit=lambda _intent: None,
        prompt="想一个话题",
        idle_threshold_s=99.0,
    )
    loop.note_event(event)
    await loop._refresh()
    loop._speak(clock.monotonic())
    await loop._refresh()

    assert len(side.users) == 2
    assert "旧问题不该再次成为主动话题" not in side.users[-1]
    assert "旧问题不该再次成为主动话题" not in loop._opportunities.material()
    assert "旧问题不该再次成为主动话题" in store.recent_events()[0]
    store.close()


async def test_rejected_proactive_submit_keeps_event_material_available() -> None:
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=1,
        event_id="rejected-question",
        viewer=Viewer(uid=1, name="阿强"),
        text="提交失败后仍可再次选题",
    )
    store.on_event(event)
    loop = ProactiveTopicLoop(
        FakeSide(),
        store,
        SpeakingFloor(clock),
        clock,
        submit=lambda _intent: False,
        prompt="想一个话题",
        idle_threshold_s=99.0,
    )
    loop.note_event(event)
    await loop._refresh()
    loop._speak(clock.monotonic())

    assert "提交失败后仍可再次选题" in loop._opportunities.material()
    store.close()


# ------------------------------------------------------------ pacer-driven rework


@asynccontextmanager
async def _running_paced(
    *,
    side: FakeSide | None,
    chattiness_level: str = "medium",
) -> AsyncIterator[tuple[ProactiveTopicLoop, SpeakingFloor, list[Intent], FakeClock, EventPacer]]:
    from bilisama.config.enums import Chattiness

    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    floor = SpeakingFloor(clock)
    pacer = EventPacer(clock, chattiness=lambda: Chattiness(chattiness_level))
    intents: list[Intent] = []
    loop = ProactiveTopicLoop(
        side,
        store,
        floor,
        clock,
        submit=intents.append,
        prompt="想一个话题",
        idle_threshold_s=10.0,
        wake_interval_s=5.0,
        event_pacer=pacer,
        reply_base_instructions=lambda: "公共上下文",
    )
    task = asyncio.create_task(loop.run())
    try:
        yield loop, floor, intents, clock, pacer
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


async def test_without_a_side_model_the_realtime_link_still_breaks_the_ice() -> None:
    """No side model used to mean no proactive topics at all; the fallback
    asks the Realtime model to pick straight from its shared history."""
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(31.0)  # the quiet-room idle target
        assert len(intents) == 1
        rules = intents[0].injection.reply.instructions or ""
        assert "后台候选暂不可用" in rules
        assert intents[0].injection.reply.base_instructions == "公共上下文"
        assert intents[0].injection.reply.write_history is True
        assert loop.status()["fallback_topics"] == 1


async def test_streamer_speech_does_not_reset_the_live_event_clock() -> None:
    """A monologue is topic material, not room activity: with a pacer wired
    the dead-air clock keeps running under the floor."""
    async with _running_paced(side=None) as (loop, floor, intents, clock, _pacer):
        loop.note_dialogue("streamer", "我先把这段跑通")
        floor.on_speech_started()
        await clock.advance(25.0)
        floor.on_speech_stopped(quiet_s=0.0)
        await clock.advance(7.0)  # 25 + 7 > 30: the quiet-room target passed under speech
        assert len(intents) == 1


async def test_unanswered_topics_count_up_and_a_danmaku_resets() -> None:
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(31.0)
        assert len(intents) == 1
        await clock.advance(31.0)
        assert len(intents) == 2
        assert "连续 1 次" in (intents[1].injection.reply.instructions or "")
        loop.note_activity(responds_to_topic=True)
        await clock.advance(31.0)
        assert "连续 0 次" in (intents[2].injection.reply.instructions or "")


async def test_pending_funnel_work_waves_the_topic_off() -> None:
    pending = True
    from bilisama.config.enums import Chattiness

    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    pacer = EventPacer(clock, chattiness=lambda: Chattiness.MEDIUM)
    intents: list[Intent] = []
    loop = ProactiveTopicLoop(
        None,
        store,
        SpeakingFloor(clock),
        clock,
        submit=intents.append,
        prompt="想一个话题",
        idle_threshold_s=10.0,
        event_pacer=pacer,
        ordinary_pending=lambda: pending,
    )
    task = asyncio.create_task(loop.run())
    try:
        await clock.advance(62.0)
        assert intents == [], "an open window or pending welcome outranks an icebreaker"
        pending = False
        await clock.advance(1.5)  # already past the idle target: the next tick speaks
        assert len(intents) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


# ------------------------------------------------- yiko-merge audit closures


async def test_side_model_failure_falls_back_instead_of_silencing_topics() -> None:
    """A configured-but-broken side model must degrade exactly like a missing
    one: the Realtime link still breaks the ice."""
    from bilisama.side import SideModelError

    class BrokenSide(FakeSide):
        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            raise SideModelError("侧路模型超时")

    async with _running_paced(side=BrokenSide()) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(31.0)
        assert len(intents) == 1
        assert "后台候选暂不可用" in (intents[0].injection.reply.instructions or "")
        assert loop.status()["fallback_topics"] == 1


async def test_dialogue_lines_enter_the_candidate_material() -> None:
    """The side model sees what was just said on air — both roles — not only
    the event log."""
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()

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
        assistant_label="豆腐",
    )
    loop.note_dialogue("streamer", "这段显存爆了")
    loop.note_dialogue("assistant", "换低显存模式试试")
    task = asyncio.create_task(loop.run())
    try:
        await clock.advance(2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert side.users, "the candidate refresh ran"
    material = side.users[-1]
    assert "主播：这段显存爆了" in material
    assert "豆腐：换低显存模式试试" in material
    assert "连续无人回应次数" in material
