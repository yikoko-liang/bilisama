"""Compose replay, Assembly, selector and scheduler without booting a model."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from bilisama.config.derive import derive
from bilisama.config.enums import Chattiness
from bilisama.config.schema import SpeakSwitches
from bilisama.dev_talk import _Fanout
from bilisama.director.floor import SpeakingFloor
from bilisama.director.scheduler import Scheduler
from bilisama.event_pacing import EventPacer, RoomActivity
from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer
from bilisama.ingest.events import EventKind, GuardLevel, LiveEvent, Viewer
from bilisama.realtime import link
from bilisama.ui.intent_test_runner import IntentTestRunner, ScenarioHooks, ScenarioSource
from bilisama.ui.test_runner import (
    MockEvent,
    MockTestCase,
    MockTestCatalog,
    MockTestSet,
    MockViewer,
    ScenarioStep,
    load_test_catalog,
)
from tests.unit.conftest import build_assembly_kit
from tests.unit.test_dev_talk_uplink import _calls, _run_director
from tests.unit.test_director import _ScriptedLink


def _test_voice_wiring_problems(tree: ast.AST) -> list[str]:
    """Pin production input wiring without opening a model or PortAudio."""
    problems: list[str] = []
    constructors = _calls(tree, "TestVoiceAudio")
    if len(constructors) != 1:
        problems.append("测试语音应只初始化一次")
    for constructor in constructors:
        config = next((kw.value for kw in constructor.keywords if kw.arg == "config"), None)
        if config is None or ast.unparse(config) != "settings.test_voice":
            problems.append("测试台词必须使用独立 test_voice 配置")
    hooks = _calls(tree, "ScenarioHooks")
    if len(hooks) != 1:
        problems.append("测试输入应只有一组运行回调")
    expected = {
        "audio": "test_audio.pcm",
        "push_audio": "audio_input.push_test_audio",
        "monitor_audio": "monitor_test_voice",
    }
    for hook in hooks:
        actual = {kw.arg: ast.unparse(kw.value) for kw in hook.keywords}
        for key, value in expected.items():
            if actual.get(key) != value:
                problems.append(f"测试 {key} 没接到独立输入通道")
    return problems


def test_production_test_voice_config_and_hooks_are_separate_from_assistant_output() -> None:
    assert _test_voice_wiring_problems(_run_director()) == []


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("config=settings.test_voice", "config=settings.speech.volcano"),
        ("audio=test_audio.pcm", "audio=assistant_audio.pcm"),
        ("push_audio=audio_input.push_test_audio", "push_audio=audio_input.push_microphone"),
        ("monitor_audio=monitor_test_voice", "monitor_audio=speaker.play"),
    ],
)
def test_production_test_voice_wiring_guard_rejects_crossed_audio_paths(
    before: str, after: str
) -> None:
    source = ast.unparse(_run_director())
    assert before in source
    assert _test_voice_wiring_problems(ast.parse(source.replace(before, after)))


class _WiredReplay:
    """Use real stateful components; only the audio/model boundary is inert."""

    def __init__(
        self,
        tmp_path: Path,
        steps: list[ScenarioStep],
        *,
        case_id: str = "wired.case",
        context: list[str] | None = None,
    ) -> None:
        self.case_id = case_id
        self.events: list[LiveEvent] = []
        self.kit = build_assembly_kit(
            tmp_path,
            speak=SpeakSwitches(vip_enter=True, entry=True, super_chat=True),
            event_observer=self.events.append,
            voice_rules="当前语音来自主播。",
            event_rules="当前直播事件来自观众。",
        )
        self.notes = ""
        self.kit.assembly._stream_intro = lambda: "今天调试本地工具。\n" + self.notes
        self.source = ScenarioSource("wired-replay")
        self.states: list[dict[str, object]] = []
        case = MockTestCase(
            id=case_id,
            group="输入意图",
            title="接线验证",
            operator="自动运行",
            expected=["金标准只在测试页展示。"],
            expected_intent="TO_ME",
            duration_s=0,
            context=context if context is not None else ["主播刚说：这个工具只在本地使用。"],
            steps=steps,
        )
        catalog = MockTestCatalog(
            [
                MockTestSet(id="simple", title="简单", description="简单接线", cases=[case]),
                MockTestSet(
                    id="hard",
                    title="困难",
                    description="多轮接线",
                    cases=[case.model_copy(update={"id": "wired.other"})],
                ),
            ]
        )
        self.runner = IntentTestRunner(
            catalog,
            self.source,
            self.kit.clock,
            self.states.append,
            ScenarioHooks(
                check_ready=lambda: None,
                audio=self.audio,
                begin=self.begin,
                end=self.end,
                push_audio=self.push_audio,
                playback_busy=lambda: False,
                response_busy=lambda: False,
            ),
        )
        self.source_task = asyncio.create_task(self.source.start(self.kit.assembly.on_event))

    async def audio(self, text: str) -> bytes:
        return b"\x00\x20" * 1024

    async def push_audio(self, pcm: bytes) -> None:
        return None

    async def begin(self, context: list[str]) -> None:
        self.source.accepted_run = int(str(self.runner.state()["run_id"]))
        self.notes = "\n".join(context)
        await self.kit.assembly.refresh_context()

    async def end(self) -> None:
        self.source.accepted_run = None
        self.notes = ""
        await self.kit.assembly.refresh_context()

    async def start(self) -> None:
        await self.runner.start(self.case_id)
        await self.kit.clock.advance(0)

    async def close(self) -> None:
        await self.runner.stop()
        await self.source.stop()
        await asyncio.wait_for(self.source_task, timeout=1)


class _CatalogReplay(_WiredReplay):
    """Keep the input path real; replace only speech synthesis and ASR receipts."""

    def __init__(
        self, tmp_path: Path, case_id: str, steps: list[ScenarioStep], context: list[str]
    ) -> None:
        self.rendered: list[str] = []
        self.first_voice_events: list[int] = []
        self._transcribed: set[str] = set()
        super().__init__(tmp_path, steps, case_id=case_id, context=context)
        self.selector = DanmakuSelector(self.kit.clock, thresholds=lambda: derive(Chattiness.HIGH))
        self.pacer = EventPacer(self.kit.clock, chattiness=lambda: Chattiness.MEDIUM)
        self.entries = EntryCoalescer(self.kit.clock, policy=self.pacer.snapshot)
        self.kit.assembly._selector = self.selector
        self.kit.assembly._entries = self.entries

    async def audio(self, text: str) -> bytes:
        self.rendered.append(text)
        return b"\x00\x20" * 64000

    async def push_audio(self, pcm: bytes) -> None:
        if not any(pcm):
            return
        rows: Any = self.runner.state()["observations"]
        row = next(item for item in reversed(rows) if item["kind"] == "voice")
        if row["step_id"] not in self._transcribed:
            self._transcribed.add(row["step_id"])
            self.first_voice_events.append(len(self.events))
            self.runner.observe(link.UserTranscriptDone(row["input"]))


_INTENT_CATALOG = load_test_catalog(
    Path(__file__).resolve().parents[2] / "config" / "testsets", intent=True
)
_PLATFORM_STEPS = [
    (case.id, step, case.context)
    for test_set in _INTENT_CATALOG.sets
    for case in test_set.cases
    for step in case.steps
    if step.event is not None or step.events_during
]


@pytest.mark.parametrize("case_id", ["simple-07", "simple-08"])
async def test_reading_catalog_sends_platform_signal_before_its_spoken_acknowledgment(
    tmp_path: Path, case_id: str
) -> None:
    case = _INTENT_CATALOG.case(case_id)
    replay = _CatalogReplay(tmp_path, case.id, case.steps, case.context)
    try:
        await replay.start()
        await replay.kit.clock.advance(20)

        assert replay.runner.state()["status"] == "completed"
        assert len(replay.events) == 1, "不能只朗读收到事件的背景，必须实际注入平台信号"
        assert replay.first_voice_events == [1], "主播接答音频开始前，事件必须已经进入 Assembly"
        assert replay.rendered == [step.text for step in case.steps if step.kind == "voice"]
        event = replay.events[0]
        assert event.room_id == 990000
        assert event.raw == {"mock_test": case_id, "intent_test_run": 1}
        assert replay.selector.status()["offered"] == 1
        assert len(replay.kit.store.recent_events()) == 1
        assert event.viewer.name in replay.kit.store.recent_events()[0]
        for context in (*replay.kit.pushed, replay.kit.assembly.build_event_context()):
            assert "READING" not in context
            assert "金标准" not in context
    finally:
        await replay.close()


@pytest.mark.parametrize(
    ("case_id", "step", "context"),
    _PLATFORM_STEPS,
    ids=[f"{case_id}.{step.id}" for case_id, step, _context in _PLATFORM_STEPS],
)
async def test_each_catalog_platform_stimulus_reaches_assembly_memory_and_event_funnel(
    tmp_path: Path, case_id: str, step: ScenarioStep, context: list[str]
) -> None:
    """Isolate stimulus wiring, without pretending to pass preceding behavior gates."""
    stimulus = step.model_copy(
        update={"after": "delay", "delay_s": 0, "observe_s": 0, "reply_from_row": 0}
    )
    replay = _CatalogReplay(tmp_path, case_id, [stimulus], context)
    expected = (
        [step.event] if step.event is not None else [edge.event for edge in step.events_during]
    )
    try:
        await replay.start()
        await replay.kit.clock.advance(5)

        assert replay.runner.state()["status"] == "completed"
        assert len(replay.events) == len(expected)
        assert len(replay.kit.store.recent_events()) == len(expected)
        assert replay.selector.status()["offered"] == sum(
            event.kind in {EventKind.DANMAKU, EventKind.GIFT} and not event.viewer.is_anchor
            for event in expected
        )
        assert replay.entries.status()["pending"] == sum(
            event.kind is EventKind.ENTRY and event.viewer.guard_level is GuardLevel.NONE
            for event in expected
        )
        for actual, spec in zip(replay.events, expected, strict=True):
            assert actual.kind is spec.kind
            assert actual.viewer.name == spec.viewer.name
            assert actual.viewer.is_anchor is spec.viewer.is_anchor
            assert actual.viewer.guard_level is spec.viewer.guard_level
            assert actual.text == spec.text
            assert actual.room_id == 990000
            assert actual.event_id.startswith(f"ui-test:1:{case_id}:")
            viewer = replay.kit.store.viewer(actual.viewer.identity)
            assert viewer is not None and viewer.uname == spec.viewer.name
            if spec.gift is not None:
                assert actual.gift is not None
                assert (actual.gift.name, actual.gift.num, actual.gift.unit_battery) == (
                    spec.gift.name,
                    spec.gift.num,
                    spec.gift.unit_battery,
                )
            if spec.kind is EventKind.ENTRY and spec.viewer.guard_level is GuardLevel.CAPTAIN:
                assert any(intent.source == "vip_enter" for intent in replay.kit.intents)
        rows: Any = replay.runner.state()["observations"]
        assert len(rows) == 1
        assert len(rows[0]["events"]) == len(expected)
        if step.events_during:
            assert replay.rendered == [step.text]
            for signal, edge in zip(rows[0]["events"], step.events_during, strict=True):
                assert signal["source_row"] == edge.source_row
                assert signal["target_offset_s"] == edge.offset_s
                assert rows[0]["voice_start_s"] <= signal["at_s"] < rows[0]["voice_end_s"]
        else:
            assert replay.rendered == [], "平台事件不应被合成为主播语音"
    finally:
        await replay.close()


def _wait_step(observe_s: float = 1) -> ScenarioStep:
    return ScenarioStep(id="wait", kind="wait", expected="暂时不插话。", observe_s=observe_s)


def _event_step(*, vip: bool = False) -> ScenarioStep:
    return ScenarioStep(
        id="event",
        kind="event",
        expected="根据事件回应观众。",
        observe_s=0,
        event=MockEvent(
            at_s=0,
            kind=EventKind.ENTRY if vip else EventKind.SUPER_CHAT,
            route="crowd",
            viewer=MockViewer(
                uid=42, name="小禾", guard_level=GuardLevel.CAPTAIN if vip else GuardLevel.NONE
            ),
            text="这个工具为什么不能联网？" if not vip else "",
            value_cny=30 if not vip else 0,
        ),
    )


async def test_replay_notes_enter_both_shared_contexts_and_restore_on_stop(tmp_path: Path) -> None:
    replay = _WiredReplay(tmp_path, [_wait_step()])
    try:
        await replay.kit.assembly.refresh_context()
        before = replay.kit.pushed[-1]
        await replay.start()
        await replay.kit.clock.advance(0.05)
        voice_context = replay.kit.pushed[-1]
        event_context = replay.kit.assembly.build_event_context()
        for context in (voice_context, event_context):
            assert "这个工具只在本地使用" in context
            assert "今天调试本地工具" in context
            assert "金标准只在测试页展示" not in context
            assert "TO_ME" not in context
        assert "当前语音来自主播" in voice_context
        assert "当前直播事件来自观众" not in voice_context
        assert "当前直播事件来自观众" in event_context
        await replay.runner.stop()
        assert replay.kit.pushed[-1] == before
        assert replay.source.accepted_run is None
    finally:
        await replay.close()


async def test_retired_source_events_never_reach_real_memory_or_submission(tmp_path: Path) -> None:
    replay = _WiredReplay(tmp_path, [_wait_step()])
    try:
        replay.source.accepted_run = 2
        for run in (1, None, 2):
            await replay.source.push(
                LiveEvent(
                    kind=EventKind.DANMAKU,
                    viewer=Viewer(uid=42, name="小禾"),
                    text=f"第 {run} 轮的问题怎么解决？",
                    event_id=f"run:{run}",
                    raw={"intent_test_run": run} if run is not None else None,
                )
            )
        await replay.kit.clock.advance(0)
        assert [event.event_id for event in replay.events] == ["run:2"]
        assert len(replay.kit.intents) == 1
        viewer = replay.kit.store.viewer("uid:42")
        assert viewer is not None and viewer.msg_count == 1
    finally:
        await replay.close()


async def test_repeated_vip_case_gets_one_welcome_in_each_run(tmp_path: Path) -> None:
    replay = _WiredReplay(tmp_path, [_event_step(vip=True)])
    try:
        await replay.start()
        await replay.kit.clock.advance(0.1)
        assert replay.runner.state()["status"] == "completed"
        assert len(replay.kit.intents) == 1
        await replay.start()
        await replay.kit.clock.advance(0.1)
        assert replay.runner.state()["status"] == "completed"
        assert len(replay.kit.intents) == 2, "上一轮欢迎记录不应吞掉本轮的 VIP 进房"
        assert replay.events[0].viewer.name == replay.events[1].viewer.name == "小禾"
        assert replay.events[0].viewer.identity != replay.events[1].viewer.identity
        assert all(event.viewer.guard_level is GuardLevel.CAPTAIN for event in replay.events)
    finally:
        await replay.close()


async def test_real_scheduler_maps_late_event_reply_back_to_its_original_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay = _WiredReplay(tmp_path, [_event_step(), _wait_step()])
    speech = _ScriptedLink()
    fanout = _Fanout(speech)
    scheduler = Scheduler(fanout, SpeakingFloor(replay.kit.clock), replay.kit.clock)
    monkeypatch.setattr(replay.kit.assembly, "_submit", scheduler.submit)

    async def observe() -> None:
        async for event in fanout.events():
            event_id = ""
            if isinstance(
                event,
                (link.ReplyStarted, link.ReplyTextDelta, link.ReplyAudioDelta, link.ReplyDone),
            ):
                intent = scheduler.reply_intent(event.handle.handle_id)
                if intent is not None and intent.event is not None:
                    event_id = intent.event.event_id
            replay.runner.observe(event, source_event_id=event_id)

    tasks = [asyncio.create_task(scheduler.run()), asyncio.create_task(observe())]
    fanout.start()
    try:
        await replay.start()
        await replay.kit.clock.advance(0.1)
        assert len(speech.handles) == 1
        handle = speech.handles[0]
        intent = scheduler.reply_intent(handle.handle_id)
        assert intent is not None and intent.event is not None
        assert intent.event.event_id == replay.events[0].event_id
        assert "这个工具为什么不能联网" in speech.items[0]
        assert len(replay.runner.state()["observations"]) == 2  # type: ignore[arg-type]
        for event in (
            link.ReplyStarted(handle),
            link.ReplyTextDelta(handle, "小禾，这个版本只支持本地运行。"),
            link.ReplyAudioDelta(handle, b"\x00\x20"),
            link.ReplyDone(handle, link.ReplyStatus.COMPLETED),
        ):
            await speech.feed.put(event)
        await replay.kit.clock.advance(1.1)
        assert not tasks[1].done(), "测试观测任务不应因接线参数不匹配而退出"
        rows: Any = replay.runner.state()["observations"]
        assert rows[0]["replies"] == ["小禾，这个版本只支持本地运行。"]
        assert rows[0]["audio_chunks"] == 1
        assert rows[1]["replies"] == [], "晚到的事件回复不能归到当前静默步骤"
    finally:
        await replay.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await fanout.aclose()


async def test_replay_reset_clears_deferred_candidates_and_allows_same_question_again(
    tmp_path: Path,
) -> None:
    kit = build_assembly_kit(tmp_path)
    blocked = True
    selector = DanmakuSelector(
        kit.clock,
        thresholds=lambda: derive(Chattiness.HIGH),
        delivery_blocked=lambda: blocked,
    )
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=990000,
        viewer=Viewer(uid=42, name="小禾"),
        text="为什么这个工具不能联网？",
        event_id="same-question",
    )
    delivered: list[LiveEvent] = []

    async def deliver(value: LiveEvent) -> None:
        delivered.append(value)

    selector.offer(event)
    await kit.clock.advance(derive(Chattiness.HIGH).danmaku_window_s + 0.1)
    await selector._advance(kit.clock.monotonic(), deliver)
    assert selector.status()["deferred_count"] == 1
    selector.reset_for_replay()
    blocked = False
    await selector._advance(kit.clock.monotonic(), deliver)
    assert delivered == []
    selector.offer(event)
    await kit.clock.advance(derive(Chattiness.HIGH).danmaku_window_s + 0.1)
    await selector._advance(kit.clock.monotonic(), deliver)
    assert delivered == [event]


async def test_replay_reset_restores_quiet_budget_and_arrival_presence(tmp_path: Path) -> None:
    kit = build_assembly_kit(tmp_path)
    pacer = EventPacer(kit.clock, chattiness=lambda: Chattiness.MEDIUM)
    coalescer = EntryCoalescer(kit.clock, policy=pacer.snapshot)
    arrival = LiveEvent(kind=EventKind.ENTRY, room_id=990000, viewer=Viewer(uid=42, name="小禾"))
    coalescer.offer(arrival)
    assert coalescer.status()["pending"] == 1
    for uid in range(40):
        pacer.note_event(
            LiveEvent(
                kind=EventKind.DANMAKU,
                room_id=990000,
                viewer=Viewer(uid=uid, name=f"观众{uid}"),
                text="这个方案怎么样？",
            )
        )
    assert pacer.snapshot().activity is RoomActivity.BUSY
    while pacer.try_consume("danmaku"):
        pass
    assert pacer.status()["ordinary_tokens"] == 0
    pacer.reset_for_replay()
    coalescer.reset_for_replay()
    assert pacer.snapshot().activity is RoomActivity.QUIET
    assert pacer.status()["ordinary_tokens"] == pacer.status()["ordinary_capacity"]
    assert coalescer.status()["pending"] == 0
    coalescer.offer(arrival)
    assert coalescer.status()["pending"] == 1, "前一轮的进房去重记录应被清除"
