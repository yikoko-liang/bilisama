"""Shared assembly harness for the unit suites.

Three suites (assembly, selector, peripherals) used to keep line-for-line
copies of the same wiring; the Assembly constructor grows a parameter almost
every stage, and three copies meant three edits — or worse, a copy someone
forgot, green-lighting wiring the product no longer uses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.schema import GrowthSwitches, InteractionConfig, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.director.interaction_state import InteractionState
from bilisama.event_pacing import EventPacer
from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer, PresenceWelcomer
from bilisama.ingest.events import LiveEvent
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "tofu"


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unit test gets a private data home.

    The config loader now reads a user profile layer from
    `$XDG_DATA_HOME/bilisama/profiles/` — on a developer machine that file
    exists the moment the panel persists an edit, and a suite that read it
    would go green or red depending on what the developer clicked last
    night. Tests that care about the data home still monkeypatch their own
    value on top of this one.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@dataclass
class AssemblyKit:
    """Everything a test needs to drive one wired Assembly."""

    assembly: Assembly
    store: MemoryStore
    persona: PersonaStore
    clock: FakeClock
    speak: SpeakSwitches
    intents: list[Intent] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    proactive: ProactiveTopicLoop | None = None


def build_assembly_kit(
    tmp_path: Path,
    *,
    speak: SpeakSwitches | None = None,
    growth: GrowthSwitches | None = None,
    selector: DanmakuSelector | None = None,
    presence: PresenceWelcomer | None = None,
    entries: EntryCoalescer | None = None,
    event_pacer: EventPacer | None = None,
    interaction_state: InteractionState | None = None,
    entry_group_enabled: Callable[[str], bool] | None = None,
    event_observer: Callable[[LiveEvent], None] | None = None,
    observe_context_item: Callable[[str], Awaitable[None]] | None = None,
    voice_rules: str = "",
    event_rules: str = "",
    variables: Mapping[str, str] | None = None,
) -> AssemblyKit:
    """One Assembly, wired the way dev-talk wires it, on a FakeClock.

    Gift tier thresholds come from the schema defaults — the same source
    production reads — so the suites cannot drift onto a private policy.
    """
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    growth = growth or GrowthSwitches()
    speak = speak or SpeakSwitches()
    interaction = InteractionConfig()
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
        distiller=Distiller(None, store, persona, growth, clock),
        proactive=proactive,
        persona=persona,
        growth=growth,
        speak_enabled=lambda source: bool(getattr(speak, source, False)),
        submit=intents.append,
        push_context=push,
        clock=clock,
        selector=selector,
        presence=presence,
        entries=entries,
        event_pacer=event_pacer,
        interaction_state=interaction_state,
        entry_group_enabled=entry_group_enabled,
        event_observer=event_observer,
        observe_context_item=observe_context_item,
        gift_battery_high=interaction.gift_battery_high,
        gift_battery_medium=interaction.gift_battery_medium,
        voice_rules=voice_rules,
        event_rules=event_rules,
        variables=variables,
    )
    return AssemblyKit(
        assembly=assembly,
        store=store,
        persona=persona,
        clock=clock,
        speak=speak,
        intents=intents,
        pushed=pushed,
        proactive=proactive,
    )
