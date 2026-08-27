"""The assembly loop and source supervision: backlog items 18 and 9.

The switch-matrix acceptance from plan section 10.3 lives here: speak off
means memory still grows and zero intents come out. So does the collect-mode
acceptance: growth files on disk, zero of their text in the pushed context.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.schema import GrowthSwitches, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.ingest.sources import QueueSource, SupervisedSource, merge
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.obs import logging as obs_logging
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop
from tests.unit.conftest import build_assembly_kit

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "tofu"


def _event(text: str = "你好", kind: EventKind = EventKind.DANMAKU, uid: int = 1) -> LiveEvent:
    return LiveEvent(kind=kind, viewer=Viewer(uid=uid, name="观众"), text=text, event_id=text)


def _fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """The `fields=` payload of every record carrying this event name.

    EventLogger puts them under `record.fields` (obs/logging.py:232), which is
    where the JSON formatter and the panel both read them from — asserting on
    the formatted line instead would test the formatter, not the call site.
    """
    return [
        getattr(record, "fields", {}) for record in caplog.records if record.getMessage() == event
    ]


def _assembly(
    tmp_path: Path,
    *,
    growth: GrowthSwitches | None = None,
    speak: SpeakSwitches | None = None,
) -> tuple[Assembly, MemoryStore, PersonaStore, list[Intent], list[str], FakeClock]:
    kit = build_assembly_kit(tmp_path, growth=growth, speak=speak)
    return kit.assembly, kit.store, kit.persona, kit.intents, kit.pushed, kit.clock


# ------------------------------------------------------------ emit path


async def test_speak_off_still_remembers_but_never_speaks(tmp_path: Path) -> None:
    """The section 2.7 acceptance: not speaking is not not knowing."""
    speak = SpeakSwitches(danmaku=False)
    assembly, store, _persona, intents, _pushed, _clock = _assembly(tmp_path, speak=speak)

    await assembly.on_event(_event("第一条"))
    await assembly.on_event(_event("第二条"))

    viewer = store.viewer("uid:1")
    assert viewer is not None and viewer.msg_count == 2, "memory grew"
    assert intents == [], "zero intents with the switch off"


async def test_speak_on_produces_an_intent(tmp_path: Path) -> None:
    assembly, _store, _persona, intents, _pushed, _clock = _assembly(tmp_path)
    await assembly.on_event(_event())
    assert len(intents) == 1
    assert intents[0].source == "danmaku"


async def test_a_failed_paid_delivery_stays_retryable(tmp_path: Path) -> None:
    """The direct ring marked the key on the ATTEMPT, so a raise from submit
    burned it: the retry the selector's deliver-then-commit contract exists for
    came back to a ring saying "already seen", and the paid thank-you was gone
    while the books called it delivered."""
    kit = build_assembly_kit(tmp_path, speak=SpeakSwitches(super_chat=True))
    boom = True

    def submit(intent: Intent) -> None:
        if boom:
            raise RuntimeError("调度器这一下没接住")
        kit.intents.append(intent)

    kit.assembly._submit = submit
    sc = LiveEvent(
        kind=EventKind.SUPER_CHAT,
        viewer=Viewer(uid=9, name="阿强"),
        text="加油",
        value_cny=30.0,
        event_id="sc-1",
    )
    with pytest.raises(RuntimeError):
        await kit.assembly.on_event(sc)
    assert kit.intents == []

    boom = False
    await kit.assembly.on_event(sc)  # the same SC, retried
    assert len(kit.intents) == 1, "第一次投递失败后，付费事件必须还能重投"
    await kit.assembly.on_event(sc)  # and a genuine replay is still deduped
    assert len(kit.intents) == 1


async def test_feed_only_kinds_never_reach_the_scheduler(tmp_path: Path) -> None:
    """entry has no speaking path in this stage even with its switch on."""
    speak = SpeakSwitches(entry=True)
    assembly, store, _persona, intents, _pushed, _clock = _assembly(tmp_path, speak=speak)
    await assembly.on_event(_event("", kind=EventKind.ENTRY))
    assert intents == []
    assert store.viewer("uid:1") is not None


# ------------------------------------------------------------ context push


async def test_growth_injects_on_on_and_stays_out_on_collect(tmp_path: Path) -> None:
    """THE collect acceptance: files grow, the prompt does not."""
    for mode, expect_injected in (("on", True), ("collect", False), ("off", False)):
        growth = GrowthSwitches.model_validate({"voice": mode, "relationship": mode})
        assembly, _store, persona, _intents, _pushed, _clock = _assembly(tmp_path, growth=growth)
        persona.write_growth("voice", ["这把稳了"])
        persona.write_growth("relationship", ["2026-08-12 观众起了外号"])

        text = assembly.build_context()
        assert ("这把稳了" in text) is expect_injected, f"voice injection wrong for {mode}"
        assert ("起了外号" in text) is expect_injected, f"relationship injection wrong for {mode}"
        assert persona.growth_entries("voice") == ["这把稳了"], "files unaffected by the mode"


async def test_context_carries_anchors_rules_and_memory(tmp_path: Path) -> None:
    assembly, store, _persona, _intents, _pushed, _clock = _assembly(tmp_path)
    (tmp_path / "live").mkdir(exist_ok=True)
    (tmp_path / "live" / "pinned.md").write_text("今晚不聊工作", encoding="utf-8")
    store.replace_facts("streamer", "", [("主播在写编译器", "")])

    text = assembly.build_context()
    assert "伴播" in text, "identity anchor"
    assert "直播规则" in text
    assert "今晚不聊工作" in text, "pinned memory"
    assert "编译器" in text
    assert "开播" in text, "the clock line"


async def test_refresh_pushes_only_when_the_text_changed(tmp_path: Path) -> None:
    assembly, store, _persona, _intents, pushed, _clock = _assembly(tmp_path)
    assert await assembly.refresh_context() is True
    assert await assembly.refresh_context() is False
    assert len(pushed) == 1, "an unchanged tail is not re-pushed — prefix cache economics"

    store.replace_facts("streamer", "", [("换了个话题", "")])
    assert await assembly.refresh_context() is True
    assert len(pushed) == 2


async def test_only_a_real_push_gets_a_line_and_it_says_how_big(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """「她到底收到了什么」要在日志页有答案，而且不能每 10 秒喊一次。

    上下文每个刻度都重建，只有文本变了才推。记在重建上，面板一分钟多出六条
    「什么都没发生」；记在推送上，一次推送一条。逐段的明细在 debug 那条上，
    两条给的尾部字数必须是同一个数。
    """
    assembly, store, _persona, _intents, pushed, _clock = _assembly(tmp_path)

    with (
        caplog.at_level("INFO", logger="bilisama.app"),
        caplog.at_level("DEBUG", logger="bilisama.persona.prompt"),
    ):
        assert await assembly.refresh_context() is True
        assert await assembly.refresh_context() is False, "没变就不推"
        pushes = _fields(caplog, "assembly.context_pushed")
        assert len(pushes) == 1, "推了一次记一条；没推的那次一个字都不该有"
        assert pushes[0]["total_chars"] == len(pushed[0])
        assert pushes[0]["prefix_chars"] > 0, "静态前缀的字数"
        assert pushes[0]["growth_voice"] == "off", "生长层开关的实际取值随推送走"

        built = _fields(caplog, "persona.prompt_assembled")
        assert len(built) == 2, "重建两次，debug 上就该有两条"
        assert built[0]["tail_chars"] == pushes[0]["tail_chars"], "两条说的得是同一个尾部"
        assert "clock_line" in built[0]["sections"], "时间段在，说明哪几段在是能看出来的"

        store.replace_facts("streamer", "", [("主播在写编译器", "")])
        assert await assembly.refresh_context() is True
        assert len(_fields(caplog, "assembly.context_pushed")) == 2


async def test_the_stream_opens_with_the_switches_it_actually_runs_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """开关状态要从 speak_enabled 这个可调用里读，不是从配置里抄一份。

    面板改一次、profile 改一次、命令行改一次，最后都汇到这个可调用上；抄配置
    的那份会在三者不一致时说谎。
    """
    speak = SpeakSwitches(danmaku=True, gift=False, entry=False)
    assembly, _store, _persona, _intents, _pushed, _clock = _assembly(tmp_path, speak=speak)
    source = QueueSource("测试源")

    with caplog.at_level("INFO", logger="bilisama.app"):
        task = asyncio.create_task(assembly.run([source]))
        await asyncio.sleep(0.05)
        await source.stop()
        await asyncio.wait_for(task, timeout=2.0)

    started = _fields(caplog, "assembly.started")
    assert len(started) == 1, "每场一条，不是每个源一条"
    assert started[0]["source_names"] == "测试源"
    on = started[0]["speak_on"].split(",")
    assert "danmaku" in on
    assert "gift" not in on and "entry" not in on
    assert started[0]["growth_relationship"] == "off"


def test_a_ported_persona_leaks_no_placeholder_through_the_fallback(tmp_path: Path) -> None:
    """Assembly's built-in variable pair is the floor for callers that pass
    none. hanako's identity titles itself with {{agentName}}, so a one-key
    fallback would put that literal text in the system prompt."""
    templates = TEMPLATE_ROOT.parent / "hanako"
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", templates)
    growth = GrowthSwitches()

    async def push(text: str) -> None:
        return None

    assembly = Assembly(
        store=store,
        distiller=Distiller(None, store, persona, growth, clock),
        proactive=ProactiveTopicLoop(
            None,
            store,
            SpeakingFloor(clock),
            clock,
            submit=lambda i: None,
            prompt="",
            idle_threshold_s=90.0,
        ),
        persona=persona,
        growth=growth,
        speak_enabled=lambda source: False,
        submit=lambda intent: None,
        push_context=push,
        clock=clock,
    )
    context = assembly.build_context()
    assert "{{" not in context, context[:200]
    store.close()


# ------------------------------------------------------------ supervision


class _Crashing:
    """Fails N times, then serves one event and exits cleanly."""

    name = "crashing"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    async def start(self, emit: object) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError(f"炸了第 {self.attempts} 次")
        await emit(_event("活过来了"))  # type: ignore[operator]

    async def stop(self) -> None:
        return None


async def test_supervised_source_restarts_with_backoff(tmp_path: Path) -> None:
    clock = FakeClock()
    crasher = _Crashing(failures=2)
    supervised = SupervisedSource(crasher, clock, max_restarts=3, backoff_s=1.0)
    got: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        got.append(event)

    task = asyncio.create_task(supervised.start(sink))
    await clock.advance(1.0 + 2.0)  # two backoffs: 1s then 2s
    await asyncio.wait_for(task, timeout=2.0)

    assert crasher.attempts == 3
    assert [e.text for e in got] == ["活过来了"]
    assert supervised.gave_up is False


class _HiccupsAfterAnHour:
    """Burns the whole restart budget, then runs healthily past HEALTHY_RUN_S,
    crashes once more, and finally serves. Only a replenished budget survives."""

    name = "hiccup"

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.attempts = 0

    async def start(self, emit: object) -> None:
        self.attempts += 1
        if self.attempts <= 3:
            raise RuntimeError(f"启动即炸第 {self.attempts} 次")
        if self.attempts == 4:
            await self._clock.sleep(SupervisedSource.HEALTHY_RUN_S + 1.0)
            raise RuntimeError("跑了一个多小时后打了个嗝")
        await emit(_event("活过来了"))  # type: ignore[operator]

    async def stop(self) -> None:
        return None


async def test_a_healthy_run_refills_the_restart_budget(tmp_path: Path) -> None:
    """D2: the cap is for crash LOOPS. A source that hiccups once a day must
    not die permanently on day four just because its lifetime total hit the
    cap — a run past HEALTHY_RUN_S resets the count."""
    clock = FakeClock()
    source = _HiccupsAfterAnHour(clock)
    supervised = SupervisedSource(source, clock, max_restarts=3, backoff_s=1.0)
    got: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        got.append(event)

    task = asyncio.create_task(supervised.start(sink))
    for step in (1.0, 2.0, 4.0):  # the three crash-loop backoffs
        await clock.advance(step)
    await clock.advance(SupervisedSource.HEALTHY_RUN_S + 1.0)  # the healthy run
    await clock.advance(1.0)  # backoff after the post-healthy hiccup
    await asyncio.wait_for(task, timeout=2.0)

    assert source.attempts == 5
    assert [e.text for e in got] == ["活过来了"]
    assert supervised.gave_up is False, "the healthy run must have refilled the budget"


async def test_a_gave_up_source_does_not_kill_its_siblings(tmp_path: Path) -> None:
    """The whole point of backlog item 9: merge survives a dead source."""
    clock = FakeClock()
    crasher = SupervisedSource(_Crashing(failures=99), clock, max_restarts=1, backoff_s=1.0)
    healthy = QueueSource("healthy")
    got: list[str] = []

    async def sink(event: LiveEvent) -> None:
        got.append(event.text)

    task = asyncio.create_task(merge([crasher, healthy], sink))
    await healthy.push(_event("第一条"))
    await clock.advance(1.0)  # crasher burns its restart and gives up
    await healthy.push(_event("第二条"))
    await asyncio.sleep(0.05)

    assert crasher.gave_up is True
    assert got == ["第一条", "第二条"], "the healthy source outlived the dead one"
    assert not task.done(), "merge itself keeps running"

    await healthy.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_cancellation_passes_straight_through_supervision(tmp_path: Path) -> None:
    clock = FakeClock()
    supervised = SupervisedSource(QueueSource("q"), clock)

    async def sink(event: LiveEvent) -> None:
        return None

    task = asyncio.create_task(supervised.start(sink))
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled(), "supervision must not swallow a cancel"


async def test_one_danmaku_leaves_a_readable_trail_in_the_log(tmp_path: Path) -> None:
    """The point of the whole logging pass, asserted end to end.

    Not "some events were logged" — that a person reading the file afterwards
    can follow one danmaku from arriving to being answered or refused. Before
    this the same journey was fifteen `print()` calls to a terminal nobody kept
    (measured: three real sessions carried one JSON line each against fourteen
    to ninety-nine printed ones), so the panel's log pane was empty and the
    file did not exist.

    Deliberately loose about which events: naming an exact sequence would make
    every future addition a red test, and the vocabulary gate
    (test_log_vocabulary.py) already stops renames. What is pinned is that the
    trail has a beginning, a middle and an end, and that the viewer's words are
    not in it.
    """
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    previous_level = root.level
    sink = _Sink()
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        assembly, _store, _persona, intents, _pushed, _clock = _assembly(tmp_path)
        await assembly.on_event(_event("主播今天玩什么啊"))
    finally:
        root.removeHandler(sink)
        root.setLevel(previous_level)

    assert len(intents) == 1, "前提没成立：这一条本该产出一个 Intent"
    events = [r.getMessage() for r in records]
    assert events, "整条链路一行日志都没有——面板的日志页会是空的"

    subsystems = {name.split(".")[0] for name in events}
    assert (
        "intents" in subsystems or "assembly" in subsystems
    ), f"看不出这条弹幕怎么变成 Intent 的：{sorted(subsystems)}"

    # The audience's words never enter the record. `_scrub` folds fields named
    # like content, but a field named something else would walk straight past
    # it, so this asserts the outcome rather than the mechanism.
    formatter = obs_logging._JsonFormatter(log_viewer_content=False)
    rendered = "\n".join(formatter.format(r) for r in records)
    assert "主播今天玩什么啊" not in rendered, f"弹幕正文漏进日志了：\n{rendered}"
