"""The assembly loop and source supervision: backlog items 18 and 9.

The switch-matrix acceptance from plan section 10.3 lives here: speak off
means memory still grows and zero intents come out. So does the collect-mode
acceptance: growth files on disk, zero of their text in the pushed context.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.schema import GrowthSwitches, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.ingest.sources import QueueSource, SupervisedSource, merge
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "mia"


def _event(text: str = "你好", kind: EventKind = EventKind.DANMAKU, uid: int = 1) -> LiveEvent:
    return LiveEvent(kind=kind, viewer=Viewer(uid=uid, name="观众"), text=text, event_id=text)


def _assembly(
    tmp_path: Path,
    *,
    growth: GrowthSwitches | None = None,
    speak: SpeakSwitches | None = None,
) -> tuple[Assembly, MemoryStore, PersonaStore, list[Intent], list[str], FakeClock]:
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    growth = growth or GrowthSwitches()
    speak_switches = speak or SpeakSwitches()
    distiller = Distiller(None, store, persona, growth, clock)
    intents: list[Intent] = []
    pushed: list[str] = []
    proactive = ProactiveTopicLoop(
        None,
        store,
        SpeakingFloor(clock),
        clock,
        submit=intents.append,
        prompt="",
        idle_threshold_s=90.0,
    )

    async def push(text: str) -> None:
        pushed.append(text)

    assembly = Assembly(
        store=store,
        distiller=distiller,
        proactive=proactive,
        persona=persona,
        growth=growth,
        speak_enabled=lambda source: bool(getattr(speak_switches, source, False)),
        submit=intents.append,
        push_context=push,
        clock=clock,
    )
    return assembly, store, persona, intents, pushed, clock


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
    assembly, store, persona, _intents, _pushed, _clock = _assembly(tmp_path)
    (tmp_path / "live").mkdir(exist_ok=True)
    (tmp_path / "live" / "pinned.md").write_text("今晚不聊工作", encoding="utf-8")
    store.replace_facts("streamer", "", [("主播在写编译器", "")])

    text = assembly.build_context()
    assert "米娅" in text, "identity anchor"
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
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled(), "supervision must not swallow a cancel"
