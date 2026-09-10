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
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from bilisama.config.enums import GrowthMode
from bilisama.config.schema import InteractionConfig
from bilisama.director.intents import (
    anchor_danmaku_context_item,
    burst_welcome_intent,
    entry_welcome_intent,
    intent_for,
)
from bilisama.ingest.bilibili.safety import DedupRing
from bilisama.ingest.bilibili.selector import SELECTOR_KINDS
from bilisama.ingest.events import EventKind, GuardLevel, is_vip_entry
from bilisama.ingest.sources import EventSink, Source, SupervisedSource, merge
from bilisama.memory.context import memory_segments
from bilisama.obs.logging import get_logger
from bilisama.persona.prompt import DynamicContext, assemble, assemble_scoped, static_prefix

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.schema import GrowthSwitches
    from bilisama.director.intent import Intent
    from bilisama.event_pacing import EventPacer
    from bilisama.ingest.bilibili.selector import (
        DanmakuSelector,
        EntryCoalescer,
        PresenceWelcomer,
    )
    from bilisama.ingest.events import LiveEvent
    from bilisama.memory.distill import Distiller
    from bilisama.memory.store import MemoryStore
    from bilisama.persona.loader import PersonaStore
    from bilisama.proactive import ProactiveTopicLoop

__all__ = ["Assembly"]

log = get_logger(__name__)

_TIER_DEFAULTS = InteractionConfig()

# Event kinds that mean "an audience member did something" for the dead-air
# clock. Follows/likes/shares and room-state churn are ambience, not company;
# resetting the proactive idle timer on them starves the icebreaker exactly
# when the room feels dead.
_AUDIENCE_ACTIVITY_KINDS = frozenset(
    {
        EventKind.DANMAKU,
        EventKind.GIFT,
        EventKind.SUPER_CHAT,
        EventKind.GUARD_BUY,
        EventKind.VIP_ENTER,
        EventKind.ENTRY,
    }
)


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
        protect_paid: bool = False,
        variables: Mapping[str, str] | None = None,
        context_refresh_s: float = 10.0,
        clock_granularity_min: int = 1,
        selector: DanmakuSelector | None = None,
        presence: PresenceWelcomer | None = None,
        entries: EntryCoalescer | None = None,
        event_pacer: EventPacer | None = None,
        entry_group_enabled: Callable[[str], bool] | None = None,
        event_observer: Callable[[LiveEvent], None] | None = None,
        observe_context_item: Callable[[str], Awaitable[None]] | None = None,
        # The two turn contracts (config/personas/live/). Empty strings keep
        # the pre-scoping behaviour, which is what every test harness that
        # does not care gets.
        voice_rules: str = "",
        event_rules: str = "",
        stream_intro: Callable[[], str] | None = None,
        # capabilities.per_reply_base_instructions. False (volcano) means the
        # session context is the only carrier: event turns keep their per-kind
        # instructions but no per-reply base rides the wire.
        per_reply_scope: bool = True,
        # Defaults READ the schema rather than repeating its numbers: retuning
        # the tier ladder in one place must not leave shadow defaults behind.
        gift_battery_high: int = _TIER_DEFAULTS.gift_battery_high,
        gift_battery_medium: int = _TIER_DEFAULTS.gift_battery_medium,
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
        self._protect_paid = protect_paid
        self._refresh_s = context_refresh_s
        self._clock_granularity_min = clock_granularity_min
        self._selector = selector
        self._presence = presence
        self._entries = entries
        self._event_pacer = event_pacer
        # None means every group is welcome — the shipped default. The
        # callable reads [interaction.entry_welcome] live, so a panel flip
        # applies to the next arrival without a rebuild.
        self._entry_group_enabled = entry_group_enabled or (lambda _group: True)
        self._event_observer = event_observer
        self._observe_context_item = observe_context_item
        # The pause gate. Events keep landing in memory and the panel while
        # it is off — pausing is not amnesia — but nothing speech-ward runs.
        self._event_input_enabled = True
        self._voice_rules = voice_rules
        self._event_rules = event_rules
        self._stream_intro = stream_intro or (lambda: "")
        self._per_reply_scope = per_reply_scope
        self._gift_battery_high = gift_battery_high
        self._gift_battery_medium = gift_battery_medium
        # Anchors are read once: editing an anchor is a restart-level change
        # (ui_meta says so), and re-reading per push would let a mid-stream
        # edit shift the cached prefix under the provider.
        # Both keys, always: a partial mapping leaves the raw {{agentName}} in
        # the prompt (most shipped personas use it in their title).
        # Callers pass persona.template_variables(cfg); this is only the floor.
        # One greeting per VIP per stream (plan section 2.7's acceptance):
        # the fixture's captain walks in twice and gets named once. Assembly
        # lives for one stream, so it needs no reset hook; bounded because a
        # marathon mega-room stream must not grow it forever.
        self._vip_greeted: OrderedDict[str, None] = OrderedDict()
        # Replay shield for the direct lane (SC / guard / VIP and selector
        # winners): blivedm's inner reconnect re-delivers recent packets, and
        # the paid kinds never pass the selector's 0.35s ring. 30s covers the
        # supervised-restart backoff too; event ids are per-event unique, so
        # the wide window cannot eat genuine reposts.
        self._direct_ring = DedupRing(window_s=30.0, capacity=2048)
        self.events_deduped = 0
        self._prefix = static_prefix(
            persona.anchors(variables or {"userName": "主播", "agentName": "助手"})
        )
        self._last_pushed = ""
        self._supervised: list[SupervisedSource] = []
        self.events_seen = 0
        self.intents_submitted = 0
        self.anchor_context_written = 0

    # ------------------------------------------------------------ emit path

    async def on_event(self, event: LiveEvent) -> None:
        """The one sink every source feeds. Memory always; speech maybe."""
        self._store.on_event(event)
        self._distiller.note_event()
        if (
            event.room_id > 0
            and event.kind in _AUDIENCE_ACTIVITY_KINDS
            and not event.viewer.is_anchor
        ):
            # A danmaku or SC also counts as answering a standing proactive
            # topic, so the unanswered counter resets on audience response,
            # not only on streamer speech.
            self._proactive.note_activity(
                responds_to_topic=event.kind in (EventKind.DANMAKU, EventKind.SUPER_CHAT)
            )
        self.events_seen += 1
        if self._event_pacer is not None:
            self._event_pacer.note_event(event)
        if self._event_observer is not None:
            # Adopted platform events, straight to the panel's feed. Before
            # every gate below on purpose: a paused or silenced room must
            # still SHOW its events — visibility is not speech.
            try:
                self._event_observer(event)
            except Exception as exc:
                log.warning("assembly.event_observer_failed", error_text=str(exc)[:200])
        if event.kind is EventKind.DANMAKU and event.viewer.is_anchor:
            # The streamer typing in their own room: shared context, never a
            # reply. Ahead of the speak switches — this is observation.
            await self._write_anchor_context(event)
            return
        if event.kind is EventKind.DANMAKU and self._entries is not None:
            # An arrival who speaks earns the reply path; a welcome on top
            # would greet them twice. Ahead of the speak check on purpose —
            # danmaku speech being off must not resurrect the double hello.
            self._entries.note_danmaku(event.viewer.identity)
        if not self._event_input_enabled:
            return
        if event.kind is EventKind.ENTRY:
            event = self._promote_entry(event)
        if not self._speak_enabled(event.kind.value):
            return
        if event.kind is EventKind.ENTRY:
            if self._entries is not None:
                if self._entry_group_enabled("ordinary"):
                    self._entries.offer(event)
            elif self._presence is not None and self._entry_group_enabled("ordinary"):
                # The legacy batch-of-5 lane, kept only for wiring that has
                # not adopted the coalescer; the ordinary-entry switch gates
                # it the same as the coalescer lane.
                burst = self._presence.note(event.viewer.identity, self._clock.monotonic())
                if burst is not None:
                    self.intents_submitted += 1
                    self._submit(
                        burst_welcome_intent(
                            burst,
                            now=self._clock.monotonic(),
                            max_tokens=self._max_tokens,
                            base_instructions=self.build_event_context() or None,
                        )
                    )
            return
        if event.kind is EventKind.VIP_ENTER:
            group = "naval" if event.viewer.guard_level is not GuardLevel.NONE else "ranking"
            if not self._entry_group_enabled(group):
                return
            if event.viewer.identity in self._vip_greeted:
                return
            self._vip_greeted[event.viewer.identity] = None
            if len(self._vip_greeted) > 4096:
                self._vip_greeted.popitem(last=False)
        if self._selector is not None and event.kind in SELECTOR_KINDS and event.room_id:
            # The funnel lane: danmaku compete for one window slot, gifts
            # aggregate. Winners re-enter through deliver_selected. Events
            # without a real room (the dev console, direct-fed fixtures) skip
            # the crowd funnel — a keyboard is not a crowd.
            self._selector.offer(event)
            return
        self._submit_event(event)

    async def deliver_selected(self, event: LiveEvent) -> None:
        """Selector winners re-enter here — memory already saw the raw hits.

        The ordinary budget is charged HERE, not when the window closed: a
        deferred winner held through a monologue pays on release, and the
        selector's delivery_blocked already guaranteed a token is waiting.
        Gift and paid lanes pass the bucket untouched.

        The pause gate is re-checked at release: a winner that entered the
        deferred pool before the pause must not speak (or spend budget) after
        it — on_event's gate only covers events that arrive DURING the pause.
        """
        if not self._event_input_enabled:
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume(
            "danmaku" if event.kind is EventKind.DANMAKU else "gift"
        ):
            return
        self._submit_event(event)

    async def deliver_entries(self, events: tuple[LiveEvent, ...]) -> None:
        """Coalesced arrivals from the EntryCoalescer become one welcome."""
        if not events or not self._speak_enabled(EventKind.ENTRY.value):
            return
        if not self._event_input_enabled:
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume("entry"):
            return
        self.intents_submitted += 1
        self._submit(
            entry_welcome_intent(
                events,
                now=self._clock.monotonic(),
                max_tokens=self._max_tokens,
                base_instructions=self.build_event_context() or None,
            )
        )

    async def _write_anchor_context(self, event: LiveEvent) -> None:
        if self._observe_context_item is None:
            return
        try:
            await self._observe_context_item(anchor_danmaku_context_item(event.redacted()))
            self.anchor_context_written += 1
        except Exception as exc:
            # Losing one context line must not take the emit path down; the
            # danmaku is already in memory either way.
            log.warning("assembly.anchor_context_write_failed", error_text=str(exc)[:200])

    def set_event_input_enabled(self, enabled: bool) -> None:
        """The pause gate. Off: events still land in memory, the pacer and
        the panel; nothing reaches the selector, the coalescer or the
        scheduler until it is back on."""
        self._event_input_enabled = enabled

    def _submit_event(self, event: LiveEvent) -> None:
        # Re-checked here because selector winners arrive up to a window
        # later: a switch flipped mid-window must silence the delivery too.
        if not self._speak_enabled(event.kind.value):
            return
        now = self._clock.monotonic()
        if event.dedup_key and self._direct_ring.contains(event.dedup_key, now):
            self.events_deduped += 1
            return
        intent = intent_for(
            event.redacted(),
            now=now,
            max_tokens=self._max_tokens,
            protect_ms=self._protect_ms,
            gift_battery_high=self._gift_battery_high,
            gift_battery_medium=self._gift_battery_medium,
            base_instructions=self.build_event_context() or None,
            protect_paid=self._protect_paid,
        )
        if intent is not None:
            self.intents_submitted += 1
            self._submit(intent)
        # Marked only once the delivery is through. Marking on the ATTEMPT meant
        # a raise from _submit left the key burned: the selector's retry (its
        # deliver-then-commit contract exists for exactly that) came back to a
        # ring that now said "already seen", the paid thank-you was dropped, and
        # the books recorded it as delivered.
        if event.dedup_key:
            self._direct_ring.mark(event.dedup_key, now)

    def _promote_entry(self, event: LiveEvent) -> LiveEvent:
        """ENTRY → VIP_ENTER for current guards and high local-medal wearers.

        Wire identity only: the locally extended InteractWordV2 model
        (VENDOR.md) carries guard level and fan medal, so a first-time
        captain is greeted THIS stream and no arrival costs a store read.
        The old store-backed past-spender lane is gone with its cache — it
        promoted a first-ever gift one stream late, flushed the write batch
        per arrival, and made spending an identity signal, which it is not.
        """
        if is_vip_entry(event.viewer, room_id=event.room_id):
            return dataclasses.replace(event, kind=EventKind.VIP_ENTER)
        return event

    # ------------------------------------------------------------ context

    def build_public_context(self) -> str:
        """The source-neutral half: persona prefix plus the shared dynamic
        tail. Growth injects on ON only — collect grows files silently, off
        contributes nothing at all. Voice and event turns both read exactly
        this material; only the input-rules block differs."""
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
            stream_intro=self._stream_intro().strip(),
            session_progress=segments.session_progress,
            regulars=segments.regulars,
            clock_line=segments.clock_line,
        )
        return assemble(self._prefix, ctx)

    def build_context(self) -> str:
        """What the SESSION carries — the implicit microphone turn's rules.

        The session's instructions govern whatever the provider generates on
        its own VAD turn, and that input is always the streamer's voice, so
        the voice contract rides here.
        """
        return assemble_scoped(self.build_public_context(), self._voice_rules)

    def build_event_context(self) -> str:
        """What one event reply carries as its scoped base, or "" when the
        provider has no per-reply channel (volcano) — the per-kind
        instructions still travel, only the full event contract stays home."""
        if not self._per_reply_scope or not self._event_rules:
            return ""
        return assemble_scoped(self.build_public_context(), self._event_rules)

    async def refresh_context(self) -> bool:
        """Push the instructions when they changed. Returns whether it pushed."""
        text = self.build_context()
        if text == self._last_pushed:
            return False
        await self._push_context(text)
        self._last_pushed = text
        # lstrip rather than a minus-two: the separator assemble() puts between
        # the halves belongs to neither, and this number has to mean the same
        # thing as persona.prompt_assembled's tail_chars or comparing the two
        # lines teaches a two-character lie.
        tail = text[len(self._prefix) :].lstrip("\n")
        # One line per PUSH, not per build: the ticker rebuilds every
        # _refresh_s and most builds are byte-identical, so logging in
        # build_context() would put six lines a minute on the panel that say
        # nothing happened. The per-segment breakdown is the debug line in
        # persona/prompt.py. Growth modes ride along because "did the growth
        # layers reach her this time" is the first question asked of a push
        # that looks too short.
        log.info(
            "assembly.context_pushed",
            total_chars=len(text),
            prefix_chars=len(self._prefix),
            tail_chars=len(tail),
            growth_voice=self._growth.voice.value,
            growth_relationship=self._growth.relationship.value,
        )
        return True

    # ------------------------------------------------------------ running

    async def run(self, sources: list[Source]) -> None:
        """Supervise every source, keep the context fresh. Cancel to stop."""
        supervised = [SupervisedSource(s, self._clock) for s in sources]
        # Kept on self so status() can answer "which source gave up" (D3) —
        # the whole point of supervision is that an outage stays visible.
        self._supervised = supervised
        # Once per stream. The switches are READ through the same callable the
        # emit path uses, not copied off the config: a profile, a panel edit
        # and a --flag all end up here, and only the callable knows the answer
        # that actually silences a lane. ROOM_STATE has no switch of its own
        # and so reads as off, which is what it is — it never speaks.
        log.info(
            "assembly.started",
            source_names=",".join(s.name for s in supervised),
            speak_on=",".join(k.value for k in EventKind if self._speak_enabled(k.value)),
            speak_off=",".join(k.value for k in EventKind if not self._speak_enabled(k.value)),
            growth_voice=self._growth.voice.value,
            growth_relationship=self._growth.relationship.value,
            refresh_s=self._refresh_s,
        )
        ticker = asyncio.create_task(self._context_ticker(), name="assembly:context")
        tasks = [ticker]
        if self._selector is not None:
            tasks.append(
                asyncio.create_task(
                    self._selector.run(self.deliver_selected), name="assembly:selector"
                )
            )
        if self._entries is not None:
            tasks.append(
                asyncio.create_task(
                    self._entries.run(self.deliver_entries), name="assembly:entries"
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

    # ------------------------------------------------------------ live edits

    def replace_persona(
        self,
        persona: PersonaStore,
        growth: GrowthSwitches,
        variables: Mapping[str, str],
        *,
        voice_rules: str | None = None,
        event_rules: str | None = None,
    ) -> None:
        """Swap the persona live: new anchors, new growth wiring, new rules.

        The static prefix is rebuilt (it was snapshotted at construction) and
        the last-pushed cache cleared, so the next refresh_context pushes
        unconditionally — a persona change must never be deduplicated away.
        """
        self._persona = persona
        self._growth = growth
        self._prefix = static_prefix(persona.anchors(variables))
        if voice_rules is not None:
            self._voice_rules = voice_rules
        if event_rules is not None:
            self._event_rules = event_rules
        self._last_pushed = ""

    def configure_interaction(
        self,
        *,
        max_tokens: int | None = None,
        protect_ms: int | None = None,
        gift_battery_high: int | None = None,
        gift_battery_medium: int | None = None,
        protect_paid: bool | None = None,
    ) -> None:
        """Apply panel edits to the knobs this assembly snapshotted at build.

        Only the named argument changes; None leaves a knob alone, so the
        caller can forward exactly what the panel touched.
        """
        if max_tokens is not None:
            self._max_tokens = max_tokens
        if protect_ms is not None:
            self._protect_ms = protect_ms
        if gift_battery_high is not None:
            self._gift_battery_high = gift_battery_high
        if gift_battery_medium is not None:
            self._gift_battery_medium = gift_battery_medium
        if protect_paid is not None:
            self._protect_paid = protect_paid

    # ------------------------------------------------------------ health

    def status(self) -> dict[str, object]:
        return {
            "events_seen": self.events_seen,
            "intents_submitted": self.intents_submitted,
            "events_deduped": self.events_deduped,
            "anchor_context_written": self.anchor_context_written,
            "event_input_enabled": self._event_input_enabled,
            "context_chars": len(self._last_pushed),
            "sources": {s.name: ("gave_up" if s.gave_up else "ok") for s in self._supervised},
        }
