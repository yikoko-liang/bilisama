"""Regression of both shipped test sets through the production backend path.

The UI runner is only the clock and fixture reader. These tests connect its
QueueSource to the real Assembly, dynamic event pacing, selector, entry
coalescing and, for two representative collision/pressure cases, the real
Scheduler and SpeechLink.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config import DerivedThresholds, effective_thresholds
from bilisama.config.schema import GrowthSwitches, InteractionConfig, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.director.scheduler import Scheduler
from bilisama.event_pacing import EventPacer, RoomActivity
from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.ingest.sources import QueueSource
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime.providers.s2s import S2SLink
from bilisama.ui.test_runner import MockEvent, MockTestCase, MockTestRunner, load_test_catalog
from tests.fakes.mock_realtime import MockRealtimeServer, Script

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _ROOT / "config" / "personas" / "tofu"
_CATALOG = load_test_catalog(_ROOT / "config" / "testsets")
_CASES = [case for test_set in _CATALOG.sets for case in test_set.cases]


def _interaction() -> InteractionConfig:
    return InteractionConfig(
        speak=SpeakSwitches(share=True, background_result=True),
    )


def _thresholds(
    interaction: InteractionConfig, event_pacer: EventPacer
) -> Callable[[], DerivedThresholds]:
    def read() -> DerivedThresholds:
        # The base window is immediately overridden by the pacer below; this
        # branch dropped the legacy [interaction.danmaku] section entirely.
        base = effective_thresholds(
            interaction.chattiness,
            reply_length=interaction.reply_length,
            danmaku_window_s=20,
        )
        pacing = event_pacer.snapshot()
        return base.model_copy(
            update={
                "danmaku_window_s": pacing.danmaku_window_s,
                "score_threshold": pacing.score_threshold,
            }
        )

    return read


def _assembly(
    tmp_path: Path,
    clock: FakeClock,
    *,
    interaction: InteractionConfig,
    submit: Callable[[Intent], None],
    floor: SpeakingFloor,
    observed: list[LiveEvent],
) -> tuple[Assembly, DanmakuSelector, EntryCoalescer, EventPacer, MemoryStore]:
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", _TEMPLATE)
    growth = GrowthSwitches()
    event_pacer = EventPacer(clock, chattiness=lambda: interaction.chattiness)
    selector = DanmakuSelector(
        clock,
        thresholds=_thresholds(interaction, event_pacer),
    )
    entries = EntryCoalescer(clock, policy=event_pacer.snapshot)
    proactive = ProactiveTopicLoop(
        None,
        store,
        floor,
        clock,
        submit=submit,
        prompt="",
        idle_threshold_s=90,
        event_pacer=event_pacer,
        ordinary_pending=lambda: bool(
            selector.status()["window_open"] or entries.status()["pending"]
        ),
    )

    async def push_context(_text: str) -> None:
        return None

    assembly = Assembly(
        store=store,
        distiller=Distiller(None, store, persona, growth, clock),
        proactive=proactive,
        persona=persona,
        growth=growth,
        speak_enabled=lambda source: bool(getattr(interaction.speak, source, False)),
        submit=submit,
        push_context=push_context,
        clock=clock,
        max_tokens=120,
        protect_ms=interaction.sc_protect_ms,
        selector=selector,
        entries=entries,
        event_pacer=event_pacer,
        event_observer=observed.append,
        gift_battery_high=interaction.gift_battery_high,
        gift_battery_medium=interaction.gift_battery_medium,
    )
    return assembly, selector, entries, event_pacer, store


async def _wait_until(predicate: Callable[[], bool], *, turns: int = 500) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("后端链路没有在预期时间内收敛")


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
async def test_every_case_enters_the_real_assembly_and_selector(
    case: MockTestCase, tmp_path: Path
) -> None:
    """Every shipped card executes against the same components dev-talk wires."""
    clock = FakeClock(wall=datetime(2026, 8, 22, 20, 0, tzinfo=UTC))
    interaction = _interaction()
    intents: list[Intent] = []
    observed: list[LiveEvent] = []
    floor = SpeakingFloor(clock)
    assembly, selector, entries, event_pacer, store = _assembly(
        tmp_path,
        clock,
        interaction=interaction,
        submit=intents.append,
        floor=floor,
        observed=observed,
    )
    source = QueueSource("test-backend", maxsize=2048)
    statuses: list[dict[str, object]] = []
    revoked: list[str] = []
    runner = MockTestRunner(_CATALOG, source, clock, statuses.append, revoke=revoked.append)
    source_task = asyncio.create_task(source.start(assembly.on_event))
    selector_task = asyncio.create_task(selector.run(assembly.deliver_selected))
    entries_task = asyncio.create_task(entries.run(assembly.deliver_entries))
    expected_events = [item for _at_s, item in case.timeline() if isinstance(item, MockEvent)]
    try:
        await runner.start(case.id)
        await clock.advance(case.duration_s + 0.01)
        await _wait_until(lambda: assembly.events_seen == len(expected_events))
        # Close the current dynamic danmaku/entry window and settle gift combos.
        await clock.advance(event_pacer.snapshot().danmaku_window_s + 2)
        await asyncio.sleep(0)

        assert runner.state()["status"] == "completed"
        assert len(observed) == len(expected_events)
        assert all(event.raw == {"mock_test": case.id} for event in observed)
        assert selector.status()["breaker_open"] is False
        assert all(intent.injection.item_text for intent in intents)

        kinds = {event.kind for event in observed}
        sources = {intent.source for intent in intents}
        if EventKind.SUPER_CHAT in kinds:
            assert EventKind.SUPER_CHAT.value in sources
        if EventKind.GUARD_BUY in kinds:
            assert EventKind.GUARD_BUY.value in sources
        if EventKind.GIFT in kinds:
            assert EventKind.GIFT.value in sources
    finally:
        await runner.stop()
        await source.stop()
        source_task.cancel()
        selector_task.cancel()
        entries_task.cancel()
        await asyncio.gather(source_task, selector_task, entries_task, return_exceptions=True)
        store.close()


async def test_busy_room_card_downshifts_ordinary_events_but_keeps_sc(
    tmp_path: Path,
) -> None:
    clock = FakeClock(wall=datetime(2026, 8, 27, 20, 0, tzinfo=UTC))
    interaction = _interaction()
    intents: list[Intent] = []
    observed: list[LiveEvent] = []
    floor = SpeakingFloor(clock)
    assembly, selector, entries, event_pacer, store = _assembly(
        tmp_path,
        clock,
        interaction=interaction,
        submit=intents.append,
        floor=floor,
        observed=observed,
    )
    source = QueueSource("test-busy-room", maxsize=2048)
    runner = MockTestRunner(_CATALOG, source, clock, lambda _status: None)
    source_task = asyncio.create_task(source.start(assembly.on_event))
    selector_task = asyncio.create_task(selector.run(assembly.deliver_selected))
    entries_task = asyncio.create_task(entries.run(assembly.deliver_entries))
    case = _CATALOG.case("func.pacing.busy-room")
    try:
        await runner.start(case.id)
        await clock.advance(case.duration_s + 10)
        await _wait_until(lambda: assembly.events_seen == len(case.timeline()))
        await asyncio.sleep(0)

        policy = event_pacer.snapshot()
        danmaku_replies = sum(intent.source == EventKind.DANMAKU.value for intent in intents)
        assert policy.activity is RoomActivity.BUSY
        assert policy.danmaku_window_s == 8.0
        assert policy.entry_enabled is False
        assert policy.proactive_enabled is False
        assert entries.status()["suppressed_busy"] == 1
        assert danmaku_replies < 31
        assert EventKind.SUPER_CHAT.value in {intent.source for intent in intents}
    finally:
        await runner.stop()
        await source.stop()
        for task in (source_task, selector_task, entries_task):
            task.cancel()
        await asyncio.gather(source_task, selector_task, entries_task, return_exceptions=True)
        store.close()


@pytest.mark.parametrize(
    ("case_id", "expected_sources"),
    [
        (
            "func.priority.collision",
            {"danmaku", "gift", "guard_buy", "super_chat"},
        ),
        (
            "biz.bettergi.maintainer-flood",
            {"danmaku", "super_chat"},
        ),
    ],
)
async def test_representative_function_and_business_cases_reach_the_real_scheduler(
    case_id: str, expected_sources: set[str], tmp_path: Path
) -> None:
    """Runner → Assembly/selector → Scheduler → real SpeechLink/mock server."""
    clock = FakeClock(wall=datetime(2026, 8, 22, 20, 0, tzinfo=UTC))
    interaction = _interaction()
    observed: list[LiveEvent] = []
    statuses: list[dict[str, object]] = []
    async with MockRealtimeServer(
        caps=caps_mod.S2S,
        script=Script(delta_chunks=1, delta_interval_s=0),
    ) as server:
        speech = S2SLink(server.url)
        await speech.connect()
        floor = SpeakingFloor(clock)
        scheduler = Scheduler(speech, floor, clock, cooldown_s=0, quiet_after_speech_s=0)
        assembly, selector, entries, event_pacer, store = _assembly(
            tmp_path,
            clock,
            interaction=interaction,
            submit=scheduler.submit,
            floor=floor,
            observed=observed,
        )
        source = QueueSource("test-full-stack", maxsize=2048)
        runner = MockTestRunner(
            _CATALOG,
            source,
            clock,
            statuses.append,
            revoke=scheduler.revoke,
        )
        source_task = asyncio.create_task(source.start(assembly.on_event))
        selector_task = asyncio.create_task(selector.run(assembly.deliver_selected))
        entries_task = asyncio.create_task(entries.run(assembly.deliver_entries))
        scheduler_task = asyncio.create_task(scheduler.run())
        case = _CATALOG.case(case_id)
        try:
            await runner.start(case_id)
            await clock.advance(case.duration_s + event_pacer.snapshot().danmaku_window_s + 2)
            await _wait_until(
                lambda: expected_sources <= {verdict.source for verdict in scheduler.verdicts},
                turns=1000,
            )

            assert runner.state()["status"] == "completed"
            assert expected_sources <= {verdict.source for verdict in scheduler.verdicts}
            assert server.recorded.count("error") == 0
            assert server.recorded.count("response.create") >= len(expected_sources)
        finally:
            await runner.stop()
            await source.stop()
            for task in (source_task, selector_task, entries_task, scheduler_task):
                task.cancel()
            await asyncio.gather(
                source_task,
                selector_task,
                entries_task,
                scheduler_task,
                return_exceptions=True,
            )
            with contextlib.suppress(Exception):
                await speech.aclose()
            store.close()
