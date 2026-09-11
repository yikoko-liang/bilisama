"""Real audio edges and cleanup, not expected labels, drive scenario execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from typing import Any

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import VoiceReplyMode
from bilisama.director.turn_protocol import TurnPolicy
from bilisama.director.voice_turn import VoiceTurnGate
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.realtime import link
from bilisama.ui.intent_test_runner import (
    IntentTestRunner,
    ScenarioHooks,
    ScenarioSource,
    check_scenario_ready,
)
from bilisama.ui.test_runner import (
    MockEvent,
    MockTestCase,
    MockTestCatalog,
    MockTestSet,
    MockViewer,
    ScenarioEvent,
    ScenarioStep,
)

_PCM = b"\x00\x20" * 1024


def _voice(step_id: str = "voice", **changes: Any) -> ScenarioStep:
    return ScenarioStep.model_validate(
        {
            "id": step_id,
            "kind": "voice",
            "text": "先听我把这个问题讲完。",
            "expected": "不要抢话",
            "source_row": 1,
            "observe_s": 0,
            **changes,
        }
    )


def _wait(step_id: str = "wait", **changes: Any) -> ScenarioStep:
    return ScenarioStep.model_validate(
        {
            "id": step_id,
            "kind": "wait",
            "expected": "等待真实条件",
            "observe_s": 0,
            **changes,
        }
    )


def _event() -> MockEvent:
    return MockEvent(
        at_s=0,
        kind=EventKind.DANMAKU,
        viewer=MockViewer(uid=7, name="麦芽"),
        text="这个错误怎么复现？",
    )


class _Rig:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.source = ScenarioSource("intent-test")
        self.states: list[dict[str, object]] = []
        self.frames: list[tuple[float, bytes]] = []
        self.monitored: list[tuple[float, bytes]] = []
        self.lease = False
        self.begins = 0
        self.ends = 0
        self.playing = False
        self.responding = False
        self.audio_texts: list[str] = []
        self.contexts: list[list[str]] = []
        self.on_audio: Callable[[str], Awaitable[bytes]] | None = None
        self.on_push: Callable[[bytes], Awaitable[None]] | None = None
        self.on_begin: Callable[[], Awaitable[None]] | None = None
        self.on_end: Callable[[], Awaitable[None]] | None = None
        self.on_monitor: Callable[[bytes], None] | None = None
        self.runner: IntentTestRunner
        self.configure([_voice()])

    def configure(self, steps: list[ScenarioStep]) -> None:
        case = MockTestCase(
            id="case.main",
            group="输入意图",
            title="测试",
            operator="自动运行",
            expected=["只观测，不自动评分"],
            duration_s=0,
            steps=steps,
            context=["主播说：这次在排查本地工具错误。"],
        )
        catalog = MockTestCatalog(
            [
                MockTestSet(id="simple", title="简单", description="测试", cases=[case]),
                MockTestSet(
                    id="hard",
                    title="复杂",
                    description="测试",
                    cases=[case.model_copy(update={"id": "case.other"})],
                ),
            ]
        )
        self.runner = IntentTestRunner(
            catalog,
            self.source,
            self.clock,
            self.states.append,
            ScenarioHooks(
                check_ready=lambda: None,
                audio=self.audio,
                begin=self.begin,
                end=self.end,
                push_audio=self.push,
                playback_busy=lambda: self.playing,
                response_busy=lambda: self.responding,
                monitor_audio=self.monitor,
            ),
        )

    async def audio(self, text: str) -> bytes:
        self.audio_texts.append(text)
        return await self.on_audio(text) if self.on_audio is not None else _PCM

    async def begin(self, context: list[str]) -> None:
        self.begins += 1
        self.contexts.append(context)
        self.lease = True
        self.source.accepted_run = int(str(self.runner.state()["run_id"]))
        if self.on_begin is not None:
            await self.on_begin()

    async def end(self) -> None:
        self.ends += 1
        self.lease = False
        self.source.accepted_run = None
        if self.on_end is not None:
            await self.on_end()

    async def push(self, pcm: bytes) -> None:
        if self.on_push is not None:
            await self.on_push(pcm)
        self.frames.append((self.clock.monotonic(), pcm))

    def monitor(self, pcm: bytes) -> None:
        assert self.frames[-1][1] == pcm, "monitoring must follow a successful model input"
        self.monitored.append((self.clock.monotonic(), pcm))
        if self.on_monitor is not None:
            self.on_monitor(pcm)

    async def start(self) -> None:
        await self.runner.start("case.main")
        await self.clock.advance(0)

    def asr(self, text: str = "先听我把这个问题讲完。") -> None:
        self.runner.observe(link.UserTranscriptDone(text))

    def reply(self, *, audio: bool = True, done: bool = False) -> link.ReplyHandle:
        handle = link.ReplyHandle()
        self.runner.observe(link.ReplyStarted(handle))
        self.runner.observe(link.ReplyTextDelta(handle, "我听着，你继续。"))
        if audio:
            self.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"))
        if done:
            self.runner.observe(link.ReplyDone(handle, link.ReplyStatus.COMPLETED))
        return handle

    def rows(self) -> list[dict[str, Any]]:
        return self.runner._observations


@pytest.fixture
async def rig() -> AsyncIterator[_Rig]:
    value = _Rig()
    yield value
    await value.runner.stop()
    await value.source.stop()


@pytest.mark.parametrize("skip", [True, False])
async def test_voice_gate_output_does_not_invent_audible_test_replies(
    rig: _Rig, skip: bool
) -> None:
    await rig.start()
    gate = VoiceTurnGate(
        rig.clock,
        policy=TurnPolicy(),
        mode=VoiceReplyMode.WHEN_ADDRESSED,
        on_skip=lambda _skip: None,
    )
    gate.attach(rig.runner.observe)
    handle = link.ReplyHandle(implicit=True)
    frames: list[link.LinkEvent] = [
        link.ReplyStarted(handle),
        link.ReplyTextDelta(handle, "[SKIP] 主播在讲解" if skip else "好，我来回答。"),
        link.ReplyAudioDelta(handle, _PCM),
        link.ReplyDone(handle, link.ReplyStatus.COMPLETED),
    ]
    try:
        for frame in frames:
            for released in gate.feed(frame):
                rig.runner.observe(released)
        row = rig.rows()[0]
        assert row["audio_chunks"] == (0 if skip else 1)
        assert bool(row["replies"]) is not skip
    finally:
        gate.close()


@pytest.mark.parametrize(
    "field",
    [
        "paused",
        "panicked",
        "room_connected",
        "live_mock_running",
        "busy",
        "link_connected",
        "output_enabled",
    ],
)
def test_readiness_rejects_conflicting_real_inputs(field: str) -> None:
    values = dict(
        paused=False,
        panicked=False,
        room_connected=False,
        live_mock_running=False,
        busy=False,
        link_connected=True,
        output_enabled=True,
    )
    values[field] = not values[field]
    with pytest.raises(ValueError):
        check_scenario_ready(**values)


async def test_preparation_never_sends_expectations_or_takes_lease_early(rig: _Rig) -> None:
    entered = asyncio.Event()

    async def blocked(_text: str) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        return _PCM

    rig.on_audio = blocked
    await rig.start()
    await asyncio.wait_for(entered.wait(), 1)
    assert not rig.lease and rig.begins == 0 and not rig.frames
    assert rig.audio_texts == ["先听我把这个问题讲完。"]
    await rig.runner.stop()
    assert rig.ends == 0 and rig.runner.state()["status"] == "stopped"


async def test_completed_execution_waits_for_late_asr_and_never_claims_pass(rig: _Rig) -> None:
    await rig.start()
    handle = rig.reply(done=True)
    await rig.clock.advance(0.2)
    assert rig.runner.active, "ASR may arrive after ReplyDone and the voice window"
    rig.asr()
    await rig.clock.advance(0.2)
    state = rig.runner.state()
    assert state["status"] == "completed" and not rig.lease
    assert state["classification_available"] is False
    assert rig.rows()[0]["actual_intent"] is None
    assert rig.rows()[0]["asr"] == ["先听我把这个问题讲完。"]
    assert rig.rows()[0]["reply_details"][0]["handle_id"] == handle.handle_id
    assert "人工" in str(state["text"])


async def test_missing_asr_is_incomplete_not_completed(rig: _Rig) -> None:
    await rig.start()
    await rig.clock.advance(31)
    assert rig.runner.state()["status"] == "incomplete"
    assert not rig.lease and rig.ends == 1


async def test_stop_preserves_cleanup_failure_instead_of_overwriting_stopped(rig: _Rig) -> None:
    async def fail() -> None:
        raise RuntimeError("恢复失败")

    rig.on_end = fail
    await rig.start()
    await rig.runner.stop()
    assert rig.runner.state()["status"] == "failed"
    assert "恢复" in str(rig.runner.state()["text"])
    assert not rig.lease and not rig.runner.active and rig.ends == 1


async def test_partial_begin_failure_releases_lease_once(rig: _Rig) -> None:
    async def fail() -> None:
        raise RuntimeError("刷新上下文失败")

    rig.on_begin = fail
    await rig.start()
    await rig.clock.advance(0)
    assert rig.runner.state()["status"] == "failed"
    assert not rig.lease and rig.begins == rig.ends == 1


async def test_late_audio_send_never_catches_up_with_a_burst(rig: _Rig) -> None:
    rig.configure([_wait(observe_s=0.2)])
    count = 0

    async def slow(_pcm: bytes) -> None:
        nonlocal count
        count += 1
        if count == 2:
            await rig.clock.sleep(0.05)

    rig.on_push = slow
    await rig.start()
    await rig.clock.advance(0.3)
    sends = [at for at, _pcm in rig.frames]
    assert len(sends) >= 3
    assert all(b - a >= 0.032 - 1e-8 for a, b in pairwise(sends))


async def test_embedded_event_occurs_inside_one_frame_voice_not_after_it(rig: _Rig) -> None:
    async def short(_text: str) -> bytes:
        return _PCM[:640]

    rig.on_audio = short
    rig.configure([_voice(events_during=[ScenarioEvent(offset_s=0, event=_event(), source_row=2)])])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.3)
    row = rig.rows()[0]
    assert row["events"][0]["at_s"] < row["voice_end_s"]
    assert row["events"][0]["source_row"] == 2
    assert rig.runner.state()["status"] == "completed"


async def test_voice_waits_for_final_push_not_just_empty_queue(rig: _Rig) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def blocked(_pcm: bytes) -> None:
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await release.wait()

    rig.on_push = blocked
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.1)
    assert entered.is_set()
    assert "voice_end_s" not in rig.rows()[0] and rig.runner.active
    release.set()
    await rig.clock.advance(0.2)
    assert rig.runner.state()["status"] == "completed"


async def test_pcm_failure_is_visible_and_releases_lease(rig: _Rig) -> None:
    async def fail(_pcm: bytes) -> None:
        raise OSError("发送失败")

    rig.on_push = fail
    await rig.start()
    await rig.clock.advance(0.2)
    assert rig.runner.state()["status"] == "failed"
    assert "发送" in str(rig.runner.state()["text"])
    assert not rig.lease and rig.ends == 1


async def test_wait_row_does_not_steal_a_late_implicit_reply(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(observe_s=1)])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.2)
    handle = rig.reply(done=True)
    assert rig.rows()[0]["reply_details"][0]["handle_id"] == handle.handle_id
    assert rig.rows()[1]["reply_details"] == []


async def test_reply_started_requires_audio_and_actual_playback(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", timeout_s=0.4)])
    await rig.start()
    rig.asr()
    handle = rig.reply(audio=False)
    rig.playing = True
    await rig.clock.advance(0.2)
    assert len(rig.rows()) == 1
    rig.playing = False
    rig.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"))
    await rig.clock.advance(0.1)
    assert len(rig.rows()) == 1
    rig.playing = True
    await rig.clock.advance(0.05)
    assert len(rig.rows()) == 2


async def test_cancelled_old_audio_cannot_unlock_reply_started(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", timeout_s=0.15)])
    await rig.start()
    rig.asr()
    handle = rig.reply()
    rig.runner.observe(link.ReplyDone(handle, link.ReplyStatus.CANCELLED))
    rig.playing = True
    await rig.clock.advance(0.3)
    assert rig.runner.state()["status"] == "incomplete"
    assert len(rig.rows()) == 1


async def test_reply_finished_waits_for_done_and_empty_real_output(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_finished", timeout_s=1)])
    await rig.start()
    rig.asr()
    handle = rig.reply()
    rig.playing = True
    await rig.clock.advance(0.1)
    rig.runner.observe(link.ReplyDone(handle, link.ReplyStatus.COMPLETED))
    await rig.clock.advance(0.1)
    assert len(rig.rows()) == 1
    rig.playing = False
    rig.responding = True
    await rig.clock.advance(0.1)
    assert len(rig.rows()) == 1
    rig.responding = False
    await rig.clock.advance(0.1)
    assert len(rig.rows()) == 2 and rig.runner.state()["status"] == "completed"


async def test_stop_during_reply_wait_sends_no_more_frames_or_events(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", timeout_s=1)])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.1)
    await rig.runner.stop()
    sent = len(rig.frames)
    await rig.clock.advance(2)
    assert len(rig.frames) == sent
    assert not rig.lease and rig.source.accepted_run is None


async def test_source_discards_retired_run_events_before_a_later_run(rig: _Rig) -> None:
    rig.configure(
        [ScenarioStep(id="event", kind="event", event=_event(), expected="回复弹幕", observe_s=1)]
    )
    await rig.start()
    await rig.runner.stop()
    await rig.start()
    delivered: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        delivered.append(event)

    source_task = asyncio.create_task(rig.source.start(sink))
    await rig.clock.advance(0)
    assert len(delivered) == 1 and delivered[0].raw is not None
    assert delivered[0].raw["intent_test_run"] == 2
    await rig.source.stop()
    await source_task


@pytest.mark.parametrize("is_anchor", [True, False])
async def test_event_injection_preserves_anchor_identity(rig: _Rig, is_anchor: bool) -> None:
    event = _event().model_copy(
        update={"viewer": MockViewer(uid=7, name="测试用户", is_anchor=is_anchor), "route": "crowd"}
    )
    rig.configure(
        [ScenarioStep(id="event", kind="event", event=event, expected="记录身份", observe_s=1)]
    )
    await rig.start()
    delivered: list[LiveEvent] = []

    async def sink(item: LiveEvent) -> None:
        delivered.append(item)

    task = asyncio.create_task(rig.source.start(sink))
    await rig.clock.advance(0)
    await rig.source.stop()
    await task
    assert len(delivered) == 1
    assert delivered[0].viewer.is_anchor is is_anchor
    assert delivered[0].room_id > 0


async def test_published_observation_snapshots_do_not_mutate_after_receipts(rig: _Rig) -> None:
    await rig.start()
    previous = rig.states[-1]["observations"]
    rig.asr()
    assert isinstance(previous, list)
    assert previous[0]["asr"] == []


async def test_specified_nested_event_reply_unlocks_gate_but_implicit_voice_does_not(
    rig: _Rig,
) -> None:
    rig.configure(
        [
            _voice(events_during=[ScenarioEvent(offset_s=0.01, event=_event(), source_row=15)]),
            _wait("between", observe_s=0.1),
            _wait("gate", after="reply_started", reply_from_row=15, timeout_s=1),
        ]
    )
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.2)
    rig.playing = True
    rig.reply()
    await rig.clock.advance(0.1)
    assert len(rig.rows()) == 2
    source_id = str(rig.rows()[0]["events"][0]["event_id"])
    handle = link.ReplyHandle()
    rig.runner.observe(link.ReplyStarted(handle), source_event_id=source_id)
    rig.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"), source_event_id=source_id)
    await rig.clock.advance(0.05)
    assert len(rig.rows()) == 3
    detail = rig.rows()[0]["reply_details"][-1]
    assert detail["source_row"] == 15 and detail["association"] == "event_id"


async def test_unknown_event_origin_remains_unattributed_and_cannot_unlock_gate(rig: _Rig) -> None:
    rig.configure(
        [
            ScenarioStep(
                id="event",
                kind="event",
                event=_event(),
                source_row=79,
                expected="等弹幕回复",
                observe_s=0.1,
            ),
            _wait(after="reply_started", reply_from_row=79, timeout_s=0.15),
        ]
    )
    await rig.start()
    handle = link.ReplyHandle()
    rig.playing = True
    rig.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"), source_event_id="unknown")
    await rig.clock.advance(0.4)
    assert rig.runner.state()["status"] == "incomplete"
    assert rig.rows()[0]["reply_details"] == []
    assert len(rig.runner.state()["unattributed_replies"]) == 1  # type: ignore[arg-type]


async def test_late_explicit_origin_moves_reply_from_temporal_guess_to_real_event(
    rig: _Rig,
) -> None:
    rig.configure(
        [
            ScenarioStep(
                id="event",
                kind="event",
                event=_event(),
                source_row=30,
                expected="等回复",
                observe_s=0,
            ),
            _voice(observe_s=1),
        ]
    )
    await rig.start()
    await rig.clock.advance(0.1)
    source_id = str(rig.rows()[0]["events"][0]["event_id"])
    handle = link.ReplyHandle()
    rig.runner.observe(link.ReplyStarted(handle))
    rig.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"), source_event_id=source_id)
    assert rig.rows()[0]["reply_details"][0]["source_row"] == 30
    assert rig.rows()[1]["reply_details"] == []


async def test_stale_or_empty_audio_is_not_playback_evidence(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", timeout_s=0.15)])
    await rig.start()
    rig.asr()
    handle = rig.reply(audio=False)
    rig.playing = True
    rig.runner.observe(link.ReplyAudioDelta(handle, b""))
    handle.stale = True
    rig.runner.observe(link.ReplyAudioDelta(handle, b"\x00\x20"))
    await rig.clock.advance(0.3)
    assert rig.runner.state()["status"] == "incomplete"


async def test_delay_after_started_rechecks_that_playback_has_not_ended(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", delay_s=0.15, timeout_s=0.3)])
    await rig.start()
    rig.asr()
    handle = rig.reply()
    rig.playing = True
    await rig.clock.advance(0.1)
    rig.playing = False
    rig.runner.observe(link.ReplyDone(handle, link.ReplyStatus.COMPLETED))
    await rig.clock.advance(0.3)
    assert rig.runner.state()["status"] == "incomplete"
    assert len(rig.rows()) == 1


async def test_event_offset_outside_real_clip_fails_before_acquiring(rig: _Rig) -> None:
    rig.configure([_voice(events_during=[ScenarioEvent(offset_s=0.064, event=_event())])])
    await rig.start()
    assert rig.runner.state()["status"] == "failed"
    assert rig.begins == rig.ends == 0 and not rig.frames


async def test_pcm_padding_preserves_samples_and_sends_equal_silence_between_steps(
    rig: _Rig,
) -> None:
    async def short(_text: str) -> bytes:
        return b"\x00\x20" * 3

    rig.on_audio = short
    rig.configure([_voice(observe_s=0.1)])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.2)
    assert rig.frames[0][1] == b"\x00\x20" * 3 + bytes(1018)
    assert all(frame == bytes(1024) for _at, frame in rig.frames[1:])


async def test_double_stop_waits_for_cleanup_once_and_blocks_new_run(rig: _Rig) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_cleanup() -> None:
        entered.set()
        await release.wait()

    rig.on_end = delayed_cleanup
    await rig.start()
    first = asyncio.create_task(rig.runner.stop())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(rig.runner.stop())
    with pytest.raises(ValueError, match="停止"):
        await rig.runner.start("case.other")
    assert rig.runner.active
    release.set()
    await asyncio.gather(first, second)
    assert rig.ends == 1 and not rig.runner.active


async def test_immediate_stop_before_task_entry_does_not_acquire_or_cleanup(rig: _Rig) -> None:
    await rig.runner.start("case.main")
    await rig.runner.stop()
    assert rig.runner.state()["status"] == "stopped"
    assert rig.begins == rig.ends == 0 and not rig.runner.active


async def test_cancellation_of_stop_caller_does_not_cancel_input_restoration(rig: _Rig) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_cleanup() -> None:
        entered.set()
        await release.wait()

    rig.on_end = delayed_cleanup
    await rig.start()
    stop = asyncio.create_task(rig.runner.stop())
    await asyncio.wait_for(entered.wait(), 1)
    stop.cancel()
    await asyncio.sleep(0)
    assert rig.runner.active
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert rig.ends == 1 and not rig.runner.active


async def test_same_viewer_is_stable_within_run_but_distinct_between_runs(rig: _Rig) -> None:
    rig.configure(
        [
            ScenarioStep(
                id="first", kind="event", event=_event(), source_row=1, expected="欢迎", observe_s=0
            ),
            ScenarioStep(
                id="second",
                kind="event",
                event=_event(),
                source_row=2,
                expected="只欢迎一次",
                observe_s=1,
            ),
        ]
    )
    await rig.start()
    first = await rig.source._queue.get()
    second = await rig.source._queue.get()
    assert first is not None and second is not None
    assert first.viewer.identity == second.viewer.identity
    assert first.viewer.uid == 0 and first.viewer.name == "麦芽"
    await rig.runner.stop()
    await rig.start()
    new = await rig.source._queue.get()
    assert new is not None and new.viewer.identity != first.viewer.identity


async def test_disconnect_during_audio_is_incomplete_and_releases_inputs(rig: _Rig) -> None:
    await rig.start()
    rig.runner.observe(link.LinkDown("测试断连"))
    await rig.clock.advance(0.1)
    assert rig.runner.state()["status"] == "incomplete"
    assert not rig.lease and rig.ends == 1


async def test_embedded_event_tracks_actual_voice_offset(rig: _Rig) -> None:
    rig.configure(
        [_voice(events_during=[ScenarioEvent(offset_s=0.045, event=_event(), source_row=2)])]
    )
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.2)
    row = rig.rows()[0]
    event = row["events"][0]
    assert event["at_s"] - row["voice_start_s"] == pytest.approx(0.045, abs=0.001)
    assert event["at_s"] < row["voice_end_s"]


async def test_event_backpressure_cannot_claim_voice_overlap(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    push = rig.source.push

    async def delayed(event: LiveEvent) -> None:
        await rig.clock.sleep(0.2)
        await push(event)

    monkeypatch.setattr(rig.source, "push", delayed)
    rig.configure([_voice(events_during=[ScenarioEvent(offset_s=0.01, event=_event())])])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.4)
    assert rig.runner.state()["status"] == "incomplete"
    assert "结束后" in str(rig.runner.state()["text"])
    assert not rig.lease and rig.source.accepted_run is None


async def test_cancel_during_begin_restores_partial_lease(rig: _Rig) -> None:
    entered = asyncio.Event()

    async def blocked() -> None:
        entered.set()
        await asyncio.Event().wait()

    rig.on_begin = blocked
    await rig.start()
    await asyncio.wait_for(entered.wait(), 1)
    await rig.runner.stop()
    assert not rig.lease and rig.begins == rig.ends == 1
    assert rig.runner.state()["status"] == "stopped"


async def test_cancel_during_pcm_push_restores_lease_and_cancels_the_push(rig: _Rig) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(_pcm: bytes) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.on_push = blocked
    await rig.start()
    await asyncio.wait_for(entered.wait(), 1)
    await rig.runner.stop()
    assert cancelled.is_set() and not rig.lease and rig.ends == 1


async def test_unfinished_reply_cannot_be_reported_as_completed(rig: _Rig) -> None:
    await rig.start()
    rig.asr()
    rig.reply(audio=False)
    await rig.clock.advance(31)
    assert rig.runner.state()["status"] == "incomplete"
    assert "未在" in str(rig.runner.state()["text"])


async def test_asr_after_all_voice_rows_is_unmatched_not_attached_to_event(rig: _Rig) -> None:
    rig.configure(
        [
            _voice(),
            ScenarioStep(
                id="event", kind="event", event=_event(), source_row=2, expected="事件", observe_s=1
            ),
        ]
    )
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.1)
    rig.asr("另一个没有输入编号的回执")
    assert rig.rows()[1]["asr"] == []
    await rig.clock.advance(1)
    assert rig.runner.state()["unmatched_asr"] == ["另一个没有输入编号的回执"]


async def test_monitor_mirrors_each_voice_frame_once_without_silence_or_reinjection(
    rig: _Rig,
) -> None:
    rig.configure([_voice(observe_s=0.15)])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.3)
    assert len(rig.monitored) == 2
    assert b"".join(frame for _at, frame in rig.monitored) == _PCM
    assert len(rig.frames) > len(rig.monitored), "silence still maintains the model audio clock"
    assert [item for item in rig.frames if any(item[1])] == rig.monitored
    assert rig.runner.state()["status"] == "completed"


async def test_monitor_rate_follows_successful_sends_without_catchup(rig: _Rig) -> None:
    count = 0

    async def long_audio(_text: str) -> bytes:
        return _PCM * 4

    async def jitter(_pcm: bytes) -> None:
        nonlocal count
        count += 1
        if count == 2:
            await rig.clock.sleep(0.05)

    rig.on_audio = long_audio
    rig.on_push = jitter
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.5)
    assert len(rig.monitored) == 8
    assert rig.monitored == [item for item in rig.frames if any(item[1])]
    assert all(b[0] - a[0] >= 0.032 - 1e-8 for a, b in pairwise(rig.monitored))


async def test_failed_model_input_is_not_monitored(rig: _Rig) -> None:
    async def fail(_pcm: bytes) -> None:
        raise OSError("真实语音输入失败")

    rig.on_push = fail
    await rig.start()
    await rig.clock.advance(0.1)
    assert rig.monitored == []
    assert rig.runner.state()["status"] == "failed" and rig.ends == 1 and not rig.lease


async def test_monitor_failure_fails_run_and_restores_lease(rig: _Rig) -> None:
    def fail(_pcm: bytes) -> None:
        raise RuntimeError("监播输出失败")

    rig.on_monitor = fail
    await rig.start()
    await rig.clock.advance(0.1)
    assert len(rig.frames) == len(rig.monitored) == 1
    assert rig.runner.state()["status"] == "failed"
    assert "监播输出失败" in str(rig.runner.state()["text"])
    assert rig.ends == 1 and not rig.lease


async def test_stop_during_model_push_prevents_monitor_and_runs_cleanup(rig: _Rig) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(_pcm: bytes) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.on_push = blocked
    await rig.start()
    await asyncio.wait_for(entered.wait(), 1)
    await rig.runner.stop()
    await rig.clock.advance(0.2)
    assert cancelled.is_set() and rig.monitored == [] and rig.frames == []
    assert rig.ends == 1 and not rig.lease


async def test_stop_clears_monitor_through_existing_end_hook_without_more_frames(rig: _Rig) -> None:
    pending_monitor: list[bytes] = []

    async def clear() -> None:
        pending_monitor.clear()

    rig.on_monitor = pending_monitor.append
    rig.on_end = clear
    await rig.start()
    assert pending_monitor
    await rig.runner.stop()
    count = len(rig.monitored)
    await rig.clock.advance(1)
    assert not pending_monitor and len(rig.monitored) == count
    assert rig.ends == 1


async def test_monitor_is_not_assistant_reply_audio_or_a_gate_receipt(rig: _Rig) -> None:
    rig.configure([_voice(), _wait(after="reply_started", timeout_s=0.1)])
    await rig.start()
    rig.asr()
    await rig.clock.advance(0.3)
    assert rig.monitored and rig.rows()[0]["audio_chunks"] == 0
    assert rig.rows()[0]["reply_details"] == []
    assert rig.runner.state()["status"] == "incomplete"


async def test_optional_monitor_default_keeps_existing_hook_callers_compatible(rig: _Rig) -> None:
    original = rig.runner._hooks
    hooks = ScenarioHooks(
        check_ready=original.check_ready,
        audio=original.audio,
        begin=original.begin,
        end=original.end,
        push_audio=original.push_audio,
        playback_busy=original.playback_busy,
        response_busy=original.response_busy,
    )
    hooks.monitor_audio(b"\x00\x20")
    assert rig.monitored == [] and rig.frames == []


async def test_stop_requested_inside_push_does_not_mirror_its_late_completion(rig: _Rig) -> None:
    stops: list[asyncio.Task[None]] = []

    async def stop_during_send(_pcm: bytes) -> None:
        stops.append(asyncio.create_task(rig.runner.stop()))
        await asyncio.sleep(0)

    rig.on_push = stop_during_send
    await rig.start()
    await asyncio.gather(*stops)
    assert rig.monitored == [] and not rig.lease and rig.ends == 1
    assert rig.runner.state()["status"] == "stopped"
