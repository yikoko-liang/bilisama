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
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.event_pacing import EventPacer
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.store import MemoryStore
from bilisama.obs.logging import setup
from bilisama.proactive import ProactiveTopicLoop
from bilisama.proactive_sources import Layer, TopicPool
from bilisama.realtime.link import ReplySpec

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


# Distinct openings, the way a side model that reads the ledger answers: a
# side that keeps returning one line is what the ledger exists to drop.
_FRESH_TOPICS = (
    "聊聊主播的新键盘",
    "问问大家周末有没有出门",
    "今晚这个插件到底装不装得上",
    "观众里有没有人也在学画画",
    "主播桌上那杯咖啡已经第几杯了",
    "最近有什么好看的番推荐一下",
    "谁家的猫今天又拆家了",
    "上次说的那个显卡到货了吗",
)


class FakeSide:
    def __init__(self, topic: str | None = None) -> None:
        self.topic = topic
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        if self.topic is not None:
            return self.topic
        return _FRESH_TOPICS[(self.calls - 1) % len(_FRESH_TOPICS)]

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
    min_gap_s: float = 0.0,
    topic_pool: TopicPool | None = None,
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
        min_gap_s=min_gap_s,
        topic_pool=topic_pool,
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
    topic_pool: TopicPool | None = None,
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
        min_gap_s=0.0,
        topic_pool=topic_pool,
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


async def test_streamer_speech_resets_the_live_event_clock() -> None:
    """Flipped on 2026-09-16. A monologue used to be "material, not activity":
    the dead-air clock kept running under the floor, so the moment the
    streamer stopped she opened a topic. That is jumping in on the streamer,
    and it is what put a proactive line nine seconds after a streamer↔her
    exchange at 16:02:44. The streamer's completed line restarts the clock."""
    async with _running_paced(side=None) as (loop, floor, intents, clock, _pacer):
        floor.on_speech_started()
        await clock.advance(25.0)
        floor.on_speech_stopped(quiet_s=0.0)
        loop.note_dialogue("streamer", "我先把这段跑通")
        await clock.advance(7.0)  # would have crossed the 30s quiet target under the old rule
        assert intents == []
        await clock.advance(24.0)  # 30s after the streamer's line
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


async def test_her_own_spoken_reply_resets_the_idle_clock() -> None:
    """16:02:35 she answered the streamer; 16:02:44 a proactive topic repeated
    it. Her own completed line is room activity for the dead-air clock."""
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(25.0)
        loop.note_dialogue("assistant", "小满问为什么砍了自动分类，主播解释是怕测试太复杂")
        await clock.advance(7.0)
        assert intents == []
        await clock.advance(24.0)
        assert len(intents) == 1


async def test_two_openings_keep_the_minimum_gap_whatever_the_room() -> None:
    async with _running(side=FakeSide(), min_gap_s=90.0) as (_loop, _floor, intents, clock):
        await clock.advance(11.0)
        assert len(intents) == 1
        await clock.advance(11.0)
        assert len(intents) == 1, "idle again, but inside the 90s gap"
        await clock.advance(70.0)
        assert len(intents) == 1, "t=92: still eight seconds short of the gap"
        await clock.advance(10.0)
        assert len(intents) == 2


async def test_the_dialogue_ring_keeps_thirty_lines() -> None:
    class RecordingSide(FakeSide):
        def __init__(self) -> None:
            super().__init__()
            self.inputs: list[str] = []

        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            self.inputs.append(user)
            return await super().complete(system=system, user=user, max_tokens=max_tokens)

    side = RecordingSide()
    async with _running(side=side, idle_threshold_s=999.0) as (loop, _floor, _intents, _clock):
        for index in range(35):
            loop.note_dialogue("streamer", f"第{index}句话")
        await loop._refresh()
        assert "第34句话" in side.inputs[-1]
        assert "第5句话" in side.inputs[-1], "thirty lines, not twelve"
        assert "第4句话" not in side.inputs[-1]


async def test_a_candidate_that_reheats_a_recent_opening_is_dropped(
    log_stream: io.StringIO,
) -> None:
    """The ledger is checked before the candidate is ever spoken."""
    side = FakeSide("大家平时更常用哪个模型？")
    async with _running_paced(side=side) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(31.0)
        assert len(intents) == 1
        first = intents[0].injection.reply.instructions or ""
        assert "候选话题是：大家平时更常用哪个模型" in first
        await clock.advance(31.0)
        assert len(intents) == 2
        second = intents[1].injection.reply.instructions or ""
        assert (
            "候选话题是：大家平时更常用哪个模型" not in second
        ), "the same candidate again is a reheat: dropped, the realtime fallback opens instead"
        assert "后台候选暂不可用" in second
        assert (
            "已经发起过" in second and "大家平时更常用哪个模型" in second
        ), "the ledger still shows her the opening she must not repeat"
        assert _lines(log_stream, "proactive.candidate_duplicate")
        assert loop.status()["candidates_dropped"] >= 1


async def test_what_she_actually_said_enters_the_ledger_and_a_repeat_cools_her_down(
    log_stream: io.StringIO,
) -> None:
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        await clock.advance(31.0)
        assert len(intents) == 1
        loop.note_spoken(
            intents[0], "小满问为什么砍了自动分类，主播解释是怕测试太复杂，先把手动流程跑通"
        )
        assert "自动分类" in loop.status()["recent_topics"][0]
        await clock.advance(31.0)
        assert len(intents) == 2
        loop.note_spoken(
            intents[1], "刚才小满问自动分类为啥砍了，我解释过是怕测试太复杂，先跑通手动流程"
        )
        assert _lines(log_stream, "proactive.repeat_detected")
        assert loop.status()["repeats_detected"] == 1
        await clock.advance(31.0)
        assert len(intents) == 2, "a detected repeat buys a cooldown before the next opening"
        await clock.advance(280.0)
        assert len(intents) == 3


async def test_a_topic_that_never_played_gives_its_material_back() -> None:
    from bilisama.obs.outcome import Outcome, Phase, Verdict

    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        event = LiveEvent(
            kind=EventKind.DANMAKU,
            room_id=10,
            event_id="q1",
            viewer=Viewer(uid=1, name="小满"),
            text="这个工具会联网吗",
        )
        loop.note_event(event)
        await clock.advance(31.0)
        assert len(intents) == 1
        assert loop.status()["last_layer"] == int(Layer.UNANSWERED)
        assert "这个工具会联网吗" not in loop._opportunities.material(), "consumed at submit"
        loop.note_verdict(
            Verdict(
                intent_id=intents[0].dedup_key,
                source="proactive",
                outcome=Outcome.EXPIRED,
                phase=Phase.QUEUED,
            )
        )
        assert (
            "这个工具会联网吗" in loop._opportunities.material()
        ), "never played: back in the pool"


async def test_layers_follow_the_room_band_and_the_pool_is_the_quiet_room_floor(
    tmp_path: Any,
) -> None:
    """A sparse room (six people walked in, nobody spoke) may not open with
    trivia; the moment the pacer reads quiet, it may."""
    from pathlib import Path

    pool_dir = Path(tmp_path)
    (pool_dir / "aigc.md").write_text("你们第一次用 AI 画图是哪一年？\n", encoding="utf-8")
    pool = TopicPool.load(pool_dir)
    async with _running_paced(side=None, topic_pool=pool) as (loop, _floor, intents, clock, pacer):
        for uid in range(1, 7):
            pacer.note_event(
                LiveEvent(
                    kind=EventKind.ENTRY,
                    room_id=10,
                    event_id=f"in{uid}",
                    viewer=Viewer(uid=uid, name=f"v{uid}"),
                )
            )
        loop.note_activity()
        assert pacer.snapshot().activity.value == "sparse"
        await clock.advance(85.0)  # idle > 60s, still sparse (downshift hold)
        assert pacer.snapshot().activity.value == "sparse"
        assert intents == [], "sparse room, no material in any allowed layer: no trivia, no topic"
        await clock.advance(10.0)  # t=95: quiet now
        assert pacer.snapshot().activity.value == "quiet"
        assert len(intents) == 1
        rules = intents[0].injection.reply.instructions or ""
        assert "趣味池" in rules and "第一次用 AI 画图" in rules
        assert loop.status()["last_layer"] == int(Layer.POOL)
        assert pool.draw(5) == [], "offered once, marked used"


async def test_answered_danmaku_seed_one_discussion_then_the_pool_takes_over(tmp_path: Any) -> None:
    """Three answered danmaku are a discussable angle once (layer 3, allowed
    in a sparse room); the same three lines do not seed a second one, so the
    next quiet-room opening falls through to the pool."""
    from pathlib import Path

    from bilisama.director.intents import _event_ref

    pool_dir = Path(tmp_path)
    (pool_dir / "aigc.md").write_text("你们第一次用 AI 画图是哪一年？\n", encoding="utf-8")
    pool = TopicPool.load(pool_dir)
    async with _running_paced(side=None, topic_pool=pool) as (loop, _floor, intents, clock, pacer):
        for uid in (1, 2, 3):
            event = LiveEvent(
                kind=EventKind.DANMAKU,
                room_id=10,
                event_id=f"e{uid}",
                viewer=Viewer(uid=uid, name=f"v{uid}"),
                text="这个模型多少钱",
            )
            pacer.note_event(event)
            loop.note_event(event)
            loop.note_activity()
            loop._opportunities.mark_answered({_event_ref(event)})
            await clock.advance(10.0)
        assert pacer.snapshot().activity.value == "sparse"
        await clock.advance(60.0)  # t=90: idle 60s in a sparse room
        assert len(intents) == 1
        first = intents[0].injection.reply.instructions or ""
        assert "第 3 层" in first and "[已回答，不要再答]" in first and "趣味池" not in first
        assert loop.status()["last_layer"] == int(Layer.DISCUSSION)
        await clock.advance(40.0)  # quiet by now, idle again
        assert len(intents) == 2, "the pool is the quiet room's floor"
        second = intents[1].injection.reply.instructions or ""
        assert "第 7 层" in second and "第一次用 AI 画图" in second
        assert "这个模型多少钱" in second, "the ledger shows the discussion she already opened"


async def test_a_reply_cut_mid_playback_is_owed_first_and_says_so() -> None:
    """Layer 1 carries both kinds of interruption: a fragment she never
    finished and a whole reply the room only heard the start of. The prompt
    tells the model which, so it continues rather than restarts."""
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        loop.note_interrupted(
            "danmaku:batch:1",
            "danmaku",
            "[记录 a 观众 UID 1] [弹幕] 小满：这个工具会联网吗",
            "小满问会不会联网，答案是本地跑的，不联网，模型文件都在你自己电脑上",
            event_refs=("a",),
            stage="playing",
        )
        await clock.advance(31.0)
        assert len(intents) == 1
        assert loop.status()["last_layer"] == int(Layer.OWED)
        body = intents[0].injection.item_text or ""
        assert "danmaku·播放被打断" in body and "观众只听到了开头一部分" in body
        assert "会不会联网" in body


async def test_the_gates_voice_notes_are_not_something_to_pick_up() -> None:
    """ "[主播语音] 整理弹幕热议话题" is what she saw him doing, not what he
    said: a delegation must not come back 20 s later as layer 4 material."""
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        loop.note_dialogue("streamer", "[主播语音] 整理弹幕热议话题")
        await clock.advance(31.0)
        assert len(intents) == 1
        assert loop.status()["last_layer"] != int(Layer.STREAMER)
        assert "整理弹幕热议话题" not in (intents[0].injection.reply.instructions or "")


async def test_an_owed_opening_that_never_played_gives_the_replies_back() -> None:
    """Taking the owed replies at submit must not lose them when the opening
    expires in the queue or is pre-empted before a word: they return, and the
    next idle stretch opens from them again."""
    from bilisama.obs.outcome import Outcome, Phase, Verdict

    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        loop.note_interrupted(
            "danmaku:batch:1",
            "danmaku",
            "[弹幕] 糯米：任务会不会传到服务器",
            "糯米别担心，任务数据",
            event_refs=("a",),
        )
        await clock.advance(31.0)
        assert len(intents) == 1 and loop.status()["last_layer"] == int(Layer.OWED)
        assert loop._opportunities.interrupted_material() == "", "taken by the opening"
        loop.note_verdict(
            Verdict(
                intent_id=intents[0].dedup_key,
                source="proactive",
                outcome=Outcome.EXPIRED,
                phase=Phase.QUEUED,
            )
        )
        assert "糯米" in loop._opportunities.interrupted_material(), "never played: owed again"
        await clock.advance(31.0)
        assert len(intents) == 2 and loop.status()["last_layer"] == int(Layer.OWED)
        loop.note_verdict(
            Verdict(
                intent_id=intents[1].dedup_key,
                source="proactive",
                outcome=Outcome.SPOKEN,
                phase=Phase.PLAYED,
            )
        )
        assert loop._opportunities.interrupted_material() == "", "played: settled"


async def test_an_owed_opening_talked_over_is_not_owed_as_a_copy_of_itself() -> None:
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        loop.note_interrupted(
            "danmaku:batch:1",
            "danmaku",
            "[弹幕] 糯米：任务会不会传到服务器",
            "糯米别担心",
            event_refs=("a",),
        )
        await clock.advance(31.0)
        key = intents[0].dedup_key
        loop.note_interrupted(key, "proactive", intents[0].injection.item_text or "", "刚才糯米问")
        assert loop._opportunities.status()["interrupted_candidates"] == 0
        from bilisama.obs.outcome import Outcome, Phase, Verdict

        loop.note_verdict(
            Verdict(
                intent_id=key, source="proactive", outcome=Outcome.CANCELLED, phase=Phase.SPEAKING
            )
        )
        assert loop._opportunities.status()["interrupted_candidates"] == 1, "the original, once"


async def test_leaving_a_replay_case_keeps_what_the_room_experienced() -> None:
    """The intent-test console resets on the way INTO a case; on the way out
    the owed replies, the unanswered danmaku and the ledger survive, so the
    next idle stretch continues the case instead of opening trivia."""
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        await loop.reset_for_replay("本例背景")
        loop.note_interrupted(
            "danmaku:batch:1",
            "danmaku",
            "[弹幕] 糯米：任务会不会传到服务器",
            "糯米别担心",
            event_refs=("a",),
        )
        loop.note_spoken(
            Intent(
                source="proactive",
                priority=Priority.PROACTIVE,
                injection=Injection(reply=ReplySpec()),
                dedup_key="proactive:earlier",
            ),
            "大家觉得哪个模型写代码最顺手",
        )
        await loop.reset_for_replay(None)
        assert "糯米" in loop._opportunities.interrupted_material()
        assert "写代码最顺手" in loop.status()["recent_topics"][0]
        await clock.advance(31.0)
        assert len(intents) == 1 and loop.status()["last_layer"] == int(Layer.OWED)
        await loop.reset_for_replay("下一例背景")
        assert (
            loop._opportunities.interrupted_material() == ""
            and loop.status()["recent_topics"] == []
        )


async def test_an_opening_reports_its_layer_for_the_panel() -> None:
    async with _running_paced(side=None) as (loop, _floor, intents, clock, _pacer):
        loop.note_dialogue("streamer", "这个自动分类我先砍了")
        await clock.advance(31.0)
        info = loop.opening_info(intents[0].dedup_key)
        assert info == {"layer": int(Layer.STREAMER), "label": "接主播的话", "candidate": False}
        assert loop.opening_info("proactive:opinions:1") is None
