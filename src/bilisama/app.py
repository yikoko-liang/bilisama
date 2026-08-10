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
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from bilisama.config.enums import GrowthMode
from bilisama.director.intents import intent_for
from bilisama.ingest.sources import EventSink, Source, SupervisedSource, merge
from bilisama.memory.context import memory_segments
from bilisama.obs.logging import get_logger
from bilisama.persona.prompt import DynamicContext, assemble, static_prefix

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.schema import GrowthSwitches
    from bilisama.director.intent import Intent
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
        # Anchors are read once: editing an anchor is a restart-level change
        # (ui_meta says so), and re-reading per push would let a mid-stream
        # edit shift the cached prefix under the provider.
        self._prefix = static_prefix(persona.anchors(variables or {"userName": "主播"}))
        self._last_pushed = ""
        self.events_seen = 0
        self.intents_submitted = 0

    # ------------------------------------------------------------ emit path

    async def on_event(self, event: LiveEvent) -> None:
        """The one sink every source feeds. Memory always; speech maybe."""
        self._store.on_event(event)
        self._distiller.note_event()
        self._proactive.note_activity()
        self.events_seen += 1
        if not self._speak_enabled(event.kind.value):
            return
        intent = intent_for(
            event.redacted(),
            now=self._clock.monotonic(),
            max_tokens=self._max_tokens,
            protect_ms=self._protect_ms,
        )
        if intent is not None:
            self.intents_submitted += 1
            self._submit(intent)

    # ------------------------------------------------------------ context

    def build_context(self) -> str:
        """Prefix plus current tail. Growth injects on ON only — collect
        grows files silently, off contributes nothing at all."""
        segments = memory_segments(self._store, self._clock)
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
        supervised: list[Source] = [SupervisedSource(s, self._clock) for s in sources]
        ticker = asyncio.create_task(self._context_ticker(), name="assembly:context")
        try:
            await merge(supervised, self._sink())
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    def _sink(self) -> EventSink:
        return self.on_event

    async def _context_ticker(self) -> None:
        while True:
            try:
                await self.refresh_context()
            except Exception as exc:
                log.warning("assembly.context_push_failed", error=str(exc))
            await self._clock.sleep(self._refresh_s)

    # ------------------------------------------------------------ health

    def status(self) -> dict[str, object]:
        return {
            "events_seen": self.events_seen,
            "intents_submitted": self.intents_submitted,
            "context_chars": len(self._last_pushed),
        }
