"""The assembly loop: sources → memory → speak switch → scheduler.

Backlog item 18, straight from plan section 2.7: registration is the entire
levelling mechanism. Every event always lands in memory and the distiller
("not speaking" is not "not knowing"); only the speak switch decides whether
an Intent is produced, and nothing downstream ever asks `if level >=`.

The context push closes the persona loop: static prefix once, dynamic tail
rebuilt and pushed only when its text actually changed, so the provider's
prefix cache survives. Growth layers inject on ON only — collect mode grows
files and puts nothing in the prompt, which is its entire meaning.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from bilisama.config.enums import GrowthMode
from bilisama.director.intents import burst_welcome_intent, intent_for
from bilisama.ingest.bilibili.selector import SELECTOR_KINDS
from bilisama.ingest.events import EventKind, GuardLevel, is_vip_entry
from bilisama.ingest.sources import EventSink, Source, SupervisedSource, merge
from bilisama.memory.context import memory_segments
from bilisama.obs.logging import get_logger
from bilisama.persona.prompt import DynamicContext, assemble, static_prefix

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.schema import GrowthSwitches
    from bilisama.director.intent import Intent
    from bilisama.ingest.bilibili.selector import DanmakuSelector, PresenceWelcomer
    from bilisama.ingest.events import LiveEvent
    from bilisama.memory.distill import Distiller
    from bilisama.memory.store import MemoryStore
    from bilisama.persona.loader import PersonaStore
    from bilisama.proactive import ProactiveTopicLoop

__all__ = ["Assembly"]

log = get_logger(__name__)


class Assembly:
    """Owns the emit path and the context push. Wire once, at startup."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        distiller: Distiller,
        proactive: ProactiveTopicLoop,
        persona: PersonaStore,
        growth: GrowthSwitches,
        speak_enabled: Callable[[str], bool],
        submit: Callable[[Intent], None],
        push_context: Callable[[str], Awaitable[None]],
        clock: Clock,
        max_tokens: int = 120,
        protect_ms: int = 4000,
        variables: Mapping[str, str] | None = None,
        context_refresh_s: float = 10.0,
        clock_granularity_min: int = 1,
        selector: DanmakuSelector | None = None,
        presence: PresenceWelcomer | None = None,
        gift_gold_high: int = 10000,
        gift_gold_medium: int = 1000,
    ) -> None:
        self._store = store
        self._distiller = distiller
        self._proactive = proactive
        self._persona = persona
        self._growth = growth
        self._speak_enabled = speak_enabled
        self._submit = submit
        self._push_context = push_context
        self._clock = clock
        self._max_tokens = max_tokens
        self._protect_ms = protect_ms
        self._refresh_s = context_refresh_s
        self._clock_granularity_min = clock_granularity_min
        self._selector = selector
        self._presence = presence
        self._gift_gold_high = gift_gold_high
        self._gift_gold_medium = gift_gold_medium
        # Anchors are read once: editing an anchor is a restart-level change
        # (ui_meta says so), and re-reading per push would let a mid-stream
        # edit shift the cached prefix under the provider.
        # Both keys, always: a partial mapping leaves the raw {{agentName}} in
        # the prompt (three of the four shipped personas use it in their title).
        # Callers pass persona.template_variables(cfg); this is only the floor.
        self._prefix = static_prefix(
            persona.anchors(variables or {"userName": "主播", "agentName": "助手"})
        )
        self._last_pushed = ""
        self._supervised: list[SupervisedSource] = []
        self.events_seen = 0
        self.intents_submitted = 0

    # ------------------------------------------------------------ emit path

    async def on_event(self, event: LiveEvent) -> None:
        """The one sink every source feeds. Memory always; speech maybe."""
        self._store.on_event(event)
        self._distiller.note_event()
        self._proactive.note_activity()
        self.events_seen += 1
        if event.kind is EventKind.ENTRY:
            event = self._promote_entry(event)
        if event.kind is EventKind.ENTRY and self._presence is not None:
            # Deliberately ahead of the speak switch: speak.entry defaults to
            # off BECAUSE this batched welcome is the designed fallback.
            burst = self._presence.note(event.viewer.identity, self._clock.monotonic())
            if burst is not None:
                self.intents_submitted += 1
                self._submit(
                    burst_welcome_intent(
                        burst, now=self._clock.monotonic(), max_tokens=self._max_tokens
                    )
                )
        if not self._speak_enabled(event.kind.value):
            return
        if self._selector is not None and event.kind in SELECTOR_KINDS:
            # The funnel lane: danmaku compete for one window slot, gifts
            # aggregate. Winners re-enter through deliver_selected.
            self._selector.offer(event)
            return
        self._submit_event(event)

    async def deliver_selected(self, event: LiveEvent) -> None:
        """Selector winners re-enter here — memory already saw the raw hits."""
        self._submit_event(event)

    def _submit_event(self, event: LiveEvent) -> None:
        intent = intent_for(
            event.redacted(),
            now=self._clock.monotonic(),
            max_tokens=self._max_tokens,
            protect_ms=self._protect_ms,
            gift_gold_high=self._gift_gold_high,
            gift_gold_medium=self._gift_gold_medium,
        )
        if intent is not None:
            self.intents_submitted += 1
            self._submit(intent)

    def _promote_entry(self, event: LiveEvent) -> LiveEvent:
        """ENTRY → VIP_ENTER when memory knows this person spent money.

        The wire model carries no guard level on InteractWordV2 (VENDOR.md),
        so the promotion is store-based: past gifts or a recorded guard tier
        earn a greeting by name.
        """
        record = self._store.viewer(event.viewer.identity)
        if record is None:
            return event
        try:
            guard = GuardLevel(record.guard_level)
        except ValueError:
            guard = GuardLevel.NONE
        if guard.is_patron or is_vip_entry(event.viewer, lifetime_gift_cny=record.gift_value_cny):
            return dataclasses.replace(event, kind=EventKind.VIP_ENTER)
        return event

    # ------------------------------------------------------------ context

    def build_context(self) -> str:
        """Prefix plus current tail. Growth injects on ON only — collect
        grows files silently, off contributes nothing at all."""
        segments = memory_segments(
            self._store, self._clock, clock_granularity_min=self._clock_granularity_min
        )
        ctx = DynamicContext(
            voice_lines=(
                tuple(self._persona.growth_entries("voice"))
                if self._growth.voice is GrowthMode.ON
                else ()
            ),
            relationship=(
                tuple(self._persona.growth_entries("relationship"))
                if self._growth.relationship is GrowthMode.ON
                else ()
            ),
            pinned=self._persona.pinned_text(),
            streamer_facts=segments.streamer_facts,
            session_progress=segments.session_progress,
            regulars=segments.regulars,
            clock_line=segments.clock_line,
        )
        return assemble(self._prefix, ctx)

    async def refresh_context(self) -> bool:
        """Push the instructions when they changed. Returns whether it pushed."""
        text = self.build_context()
        if text == self._last_pushed:
            return False
        await self._push_context(text)
        self._last_pushed = text
        return True

    # ------------------------------------------------------------ running

    async def run(self, sources: list[Source]) -> None:
        """Supervise every source, keep the context fresh. Cancel to stop."""
        supervised = [SupervisedSource(s, self._clock) for s in sources]
        # Kept on self so status() can answer "which source gave up" (D3) —
        # the whole point of supervision is that an outage stays visible.
        self._supervised = supervised
        ticker = asyncio.create_task(self._context_ticker(), name="assembly:context")
        tasks = [ticker]
        if self._selector is not None:
            tasks.append(
                asyncio.create_task(
                    self._selector.run(self.deliver_selected), name="assembly:selector"
                )
            )
        try:
            await merge(list(supervised), self._sink())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _sink(self) -> EventSink:
        return self.on_event

    async def _context_ticker(self) -> None:
        while True:
            try:
                await self.refresh_context()
            except Exception as exc:
                log.warning("assembly.context_push_failed", error_text=str(exc))
            await self._clock.sleep(self._refresh_s)

    # ------------------------------------------------------------ health

    def status(self) -> dict[str, object]:
        return {
            "events_seen": self.events_seen,
            "intents_submitted": self.intents_submitted,
            "context_chars": len(self._last_pushed),
            "sources": {s.name: ("gave_up" if s.gave_up else "ok") for s in self._supervised},
        }
