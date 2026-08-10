"""Stage 3 acceptance, composed end to end (plan section 9, stage 3 row).

Two closing criteria that no single-module test can claim:

1. A proactive intent flows through the REAL scheduler and the real S2SLink
   against MockRealtimeServer, gets spoken, and the spoken text lands in the
   distiller's voice-exemplar box — the L1 speaking path, assembled.
2. Three replayed streams of the same crowd, with growth ON and a fake side
   model, leave `第 3 次来` in the built context, growth files within
   budget, and the anchor files byte-identical.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from bilisama.app import Assembly
from bilisama.clock import FakeClock, SystemClock
from bilisama.config.schema import GrowthSwitches, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.scheduler import Scheduler
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.obs.outcome import Outcome
from bilisama.persona.growth import VOICE_MAX_LINES
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime.link import ReplySpec
from bilisama.realtime.providers.s2s import S2SLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script
from tests.fakes.replay import fixture, read_fixture
from tests.unit.test_distill import FakeSide, _batch_reply

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "mia"


@contextlib.asynccontextmanager
async def _speaking_stack(
    script: Script,
) -> AsyncIterator[tuple[Scheduler, Distiller, SpeakingFloor]]:
    """Real scheduler + real S2SLink + mock server + distiller sink."""
    clock = SystemClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona_dir = Path("/nonexistent")  # the distiller only collects lines here
    distiller = Distiller(
        None, store, PersonaStore(persona_dir, TEMPLATE_ROOT), GrowthSwitches(), clock
    )
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        link = S2SLink(server.url)
        await link.connect()
        floor = SpeakingFloor(clock)
        scheduler = Scheduler(link, floor, clock, spoken_sink=distiller.note_assistant_line)
        runner = asyncio.create_task(scheduler.run())
        try:
            yield scheduler, distiller, floor
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            await link.aclose()
            store.close()


async def _wait(predicate: Callable[[], bool], *, timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等超时了")


async def test_a_proactive_topic_is_spoken_and_feeds_the_voice_box() -> None:
    """L1's speaking path: PROACTIVE intent → real scheduler → mock provider
    → SPOKEN verdict → the completed text reaches the distiller."""
    script = Script(delta_chunks=2, reply_text="说起来，今天这个键盘手感真不错。")
    async with _speaking_stack(script) as (scheduler, distiller, _floor):
        scheduler.submit(
            Intent(
                source="proactive",
                priority=Priority.PROACTIVE,
                injection=Injection(
                    reply=ReplySpec(instructions="起个话题", max_tokens=80), item_text=None
                ),
                trusted=True,
                dedup_key="proactive:1",
            )
        )
        await _wait(lambda: len(scheduler.verdicts) >= 1)

    verdict = scheduler.verdicts[0]
    assert verdict.outcome is Outcome.SPOKEN
    assert verdict.source == "proactive"
    assert distiller._state.assistant_lines == [
        "说起来，今天这个键盘手感真不错。"
    ], "the cleanly spoken line is voice-exemplar raw material"


async def test_three_streams_grow_memory_and_growth_but_never_the_anchors(
    tmp_path: Path,
) -> None:
    """The full stage-3 loop, three times over, against every closing number."""
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    side = FakeSide(
        [
            _batch_reply(voice=["第一场的口癖"], relationship=["观众第一次起哄"]),
            _batch_reply(voice=["第二场的口癖"], relationship=["外号定下来了"]),
            _batch_reply(voice=["第三场的口癖"], relationship=["成了固定节目"]),
        ]
    )
    distiller = Distiller(side, store, persona, growth, clock, every_n_events=999)
    intents: list[Intent] = []
    proactive = ProactiveTopicLoop(
        None,
        store,
        SpeakingFloor(clock),
        clock,
        submit=intents.append,
        prompt="",
        idle_threshold_s=999.0,
    )

    async def push(_text: str) -> None:
        return None

    anchors_before = {p.name: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")}

    assembly = Assembly(
        store=store,
        distiller=distiller,
        proactive=proactive,
        persona=persona,
        growth=growth,
        speak_enabled=lambda s: bool(getattr(SpeakSwitches(), s, False)),
        submit=intents.append,
        push_context=push,
        clock=clock,
    )

    for _ in range(3):
        store.begin_stream()
        for _at, event in read_fixture(fixture("returning_viewer.jsonl")):
            await assembly.on_event(event)
        assert (await distiller.end_of_stream()).ran
        store.end_stream()
        await clock.advance(86400.0)  # next stream, next day

    store.begin_stream()
    for _at, event in read_fixture(fixture("returning_viewer.jsonl")):
        await assembly.on_event(event)

    context = assembly.build_context()
    assert "第 4 次来" in context, "three replays plus tonight reads four visits"
    viewer = store.viewer("uid:7001")
    assert viewer is not None and viewer.streams_seen == 4

    voice = persona.growth_entries("voice")
    assert voice == ["第一场的口癖", "第二场的口癖", "第三场的口癖"]
    assert len(voice) <= VOICE_MAX_LINES
    assert "第三场的口癖" in context and "成了固定节目" in context, "ON injects"

    assert {p.name: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")} == anchors_before
    assert not (tmp_path / "live" / "identity.md").exists()
    assert not (tmp_path / "live" / "personality.md").exists()
