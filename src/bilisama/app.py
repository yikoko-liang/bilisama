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
from enum import StrEnum
from typing import TYPE_CHECKING

from bilisama.config.enums import GrowthMode
from bilisama.config.schema import InteractionConfig
from bilisama.director.intents import (
    _event_ref,
    anchor_danmaku_context_item,
    burst_welcome_intent,
    danmaku_batch_intent,
    entry_welcome_intent,
    gift_combo_intent,
    intent_for,
    observed_events_context_item,
)
from bilisama.director.interaction_state import REPORT_RULES
from bilisama.director.viewer_threads import ViewerThreads, turns_to_room, viewer_chat_target
from bilisama.ingest.bilibili.safety import DedupRing, aggregate_gift_events
from bilisama.ingest.bilibili.selector import SELECTOR_KINDS
from bilisama.ingest.events import EventKind, GuardLevel, is_vip_entry
from bilisama.ingest.sources import EventSink, Source, SupervisedSource, merge
from bilisama.memory.context import memory_segments
from bilisama.obs.logging import get_logger
from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict
from bilisama.persona.prompt import DynamicContext, assemble, assemble_scoped, static_prefix

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.schema import GrowthSwitches
    from bilisama.director.intent import Intent
    from bilisama.director.interaction_state import InteractionReport, InteractionState
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

__all__ = ["Assembly", "SummaryOutcome"]

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


def _answered_by(verdict: Verdict) -> bool:
    """Spoken to, or looked at and declined: either way the audience line is
    handled. Interrupted, expired and gated turns leave it open."""
    if verdict.outcome is Outcome.SPOKEN:
        return True
    return verdict.outcome is Outcome.SKIPPED and verdict.reason is SkipReason.MODEL_DECLINED


class SummaryOutcome(StrEnum):
    """What a voice-delegated danmaku summary turned into."""

    DELIVERED = "delivered"  # a summary over the pre-voice backlog
    EMPTY = "empty"  # nothing to summarize: she says so in one line
    SILENT = "silent"  # this delegation was already served moments ago


_EMPTY_SUMMARY_GUARD_S = 30.0


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
        interaction_state: InteractionState | None = None,
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
        self._interaction_state = interaction_state
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
        self._replay_context: str | None = None
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
        self._vip_pending: dict[str, str] = {}
        # Danmaku turns in flight, by intent key, with the audience lines each
        # one is about. Settled at the verdict: a line the reply lane spoke to
        # or declined is answered for good in the proactive loop — the core
        # of the "she brought my question up again as a topic" repeat
        # (2026-09-16).
        self._danmaku_pending: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._last_summary_at: float | None = None
        self._pending_observations: OrderedDict[str, LiveEvent] = OrderedDict()
        # Replay shield for the direct lane (SC / guard / VIP and selector
        # winners): blivedm's inner reconnect re-delivers recent packets, and
        # the paid kinds never pass the selector's 0.35s ring. 30s covers the
        # supervised-restart backoff too; event ids are per-event unique, so
        # the wide window cannot eat genuine reposts.
        self._direct_ring = DedupRing(window_s=30.0, capacity=2048)
        self.events_deduped = 0
        names = variables or {"userName": "主播", "agentName": "助手"}
        self._prefix = static_prefix(persona.anchors(names))
        # The two names a typed @ may legitimately address (director/
        # viewer_threads): the host under either template key, and her.
        self._host_names = tuple(
            name for name in (names.get("userName", ""), names.get("username", "")) if name
        ) or ("主播",)
        self._assistant_names = (names.get("agentName", "") or "助手",)
        self._threads = ViewerThreads(clock)
        self.viewer_chat_skipped = 0
        self.viewer_chat_reasons: dict[str, int] = {}
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
        self._proactive.note_replay_event(event)
        self._proactive.note_event(event)
        # The feed and memory keep the platform event above; the shared
        # ledger uses the same canonical kind that the reply lane will use.
        # Thread facts first, so the ledger, the observations and the reply
        # line all carry the same note (director/viewer_threads).
        chat_target: str | None = None
        chat_reason: str | None = None
        if event.kind is EventKind.DANMAKU and not event.viewer.is_anchor:
            chat_target = viewer_chat_target(
                event, host_names=self._host_names, assistant_names=self._assistant_names
            )
            thread = self._threads.reply_context(event)
            self._threads.note(event, target=chat_target)
            if chat_target is not None:
                chat_reason = "at_viewer"
            if thread is not None:
                by_name, ago_s = thread
                event = dataclasses.replace(event, thread_note=f"{ago_s} 秒前被观众 {by_name} @过")
                # Viewer chat is a matter of fact here, not of model judgment
                # (2026-09-17): written back within the window after being
                # @'d, and not turning to the host, her or the room, it stays
                # out of the reply lane. Everything else an audience member
                # writes is an ordinary danmaku.
                if (
                    chat_reason is None
                    and event.reply_to_anchor is not True
                    and not turns_to_room(
                        event.text,
                        host_names=self._host_names,
                        assistant_names=self._assistant_names,
                    )
                ):
                    chat_reason = "thread_reply"
        observed = self._observe_interaction_event(event)
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
            # reply. An explicit platform reply target also closes the matching
            # audience item, but never cancels a reply that is already active.
            self._mark_anchor_reply(event)
            if self._event_input_enabled:
                await self._write_anchor_context(event)
            return
        if self._event_input_enabled and event.kind in _AUDIENCE_ACTIVITY_KINDS:
            self._pending_observations[observed.dedup_key] = observed.redacted()
            while len(self._pending_observations) > 32:
                self._pending_observations.popitem(last=False)
        if event.kind is EventKind.DANMAKU and self._entries is not None:
            # An arrival who speaks earns the reply path; a welcome on top
            # would greet them twice. Ahead of the speak check on purpose —
            # danmaku speech being off must not resurrect the double hello.
            self._entries.note_danmaku(event.viewer.identity)
        if not self._event_input_enabled:
            return
        event = observed
        if self._interaction_state is not None and self._interaction_state.is_handled(event):
            return
        if chat_reason is not None:
            # Addressed to another viewer (platform reply target or an
            # unambiguous typed @), or written back to one who @'d them a
            # moment ago. Seen, remembered, in the shared context — and not
            # a reply candidate. The model used to be the only judge here
            # and answered 「@白团 …」 on 2026-09-15; since 2026-09-17 it no
            # longer gets to call anything else viewer chat.
            self.viewer_chat_skipped += 1
            self.viewer_chat_reasons[chat_reason] = self.viewer_chat_reasons.get(chat_reason, 0) + 1
            log.info(
                "assembly.viewer_chat_skipped",
                identity=event.viewer.identity,
                reason=chat_reason,
                target_text=chat_target or "",
                platform_target=event.reply_to_anchor is False,
                thread_note=event.thread_note,
            )
            return
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
            if (
                event.viewer.identity in self._vip_greeted
                or event.viewer.identity in self._vip_pending.values()
            ):
                return
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
        event = self._observe_interaction_event(event)
        if self._interaction_state is not None and self._interaction_state.is_handled(event):
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume(
            "danmaku" if event.kind is EventKind.DANMAKU else "gift"
        ):
            return
        self._submit_event(event)

    async def deliver_gift_batch(self, aggregate: LiveEvent, events: tuple[LiveEvent, ...]) -> None:
        """Keep combo contributions identifiable after they enter the scheduler."""
        if not self._event_input_enabled or not self._speak_enabled(EventKind.GIFT.value):
            return
        events = self._unhandled_events(events)
        if not events:
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume("gift"):
            return
        self._submit_event(aggregate, gift_members=events)

    async def deliver_entries(self, events: tuple[LiveEvent, ...]) -> None:
        """Coalesced arrivals from the EntryCoalescer become one welcome."""
        if not events or not self._speak_enabled(EventKind.ENTRY.value):
            return
        if not self._event_input_enabled:
            return
        events = self._unhandled_events(events)
        if not events:
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

    async def deliver_danmaku_batch(self, events: tuple[LiveEvent, ...]) -> None:
        """Charge one pacing token and submit one model turn for the batch."""
        if not events or not self._event_input_enabled or not self._speak_enabled("danmaku"):
            return
        events = self._unhandled_events(events)
        if not events:
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume("danmaku"):
            raise RuntimeError("弹幕预算暂不可用，保留批次等待")
        summary = self._proactive.danmaku_summary_intent(
            tuple(event.redacted() for event in events), now=self._clock.monotonic()
        )
        if summary is not None:
            self._submit_danmaku_summary(summary)
            return
        self._submit_danmaku_turn(
            danmaku_batch_intent(
                tuple(event.redacted() for event in events),
                now=self._clock.monotonic(),
                max_tokens=self._max_tokens,
                base_instructions=self.build_event_context() or None,
            )
        )
        self.intents_submitted += 1

    def _submit_danmaku_turn(self, intent: Intent) -> None:
        """Submit one danmaku (or summary) turn and remember its audience lines
        until the verdict settles them."""
        events = intent.events or ((intent.event,) if intent.event is not None else ())
        refs = frozenset(
            ref
            for event in events
            if event.kind is EventKind.DANMAKU and event.dedup_key
            for ref in (event.dedup_key, _event_ref(event))
        )
        self._submit(intent)
        if refs and intent.dedup_key:
            self._danmaku_pending[intent.dedup_key] = refs
            while len(self._danmaku_pending) > 512:
                self._danmaku_pending.popitem(last=False)

    def _observe_interaction_event(self, event: LiveEvent) -> LiveEvent:
        """Keep report IDs aligned with the event kind used for scheduling."""
        if event.kind is EventKind.ENTRY:
            event = self._promote_entry(event)
        if self._interaction_state is not None and event.kind in _AUDIENCE_ACTIVITY_KINDS:
            self._interaction_state.observe(event)
        return event

    def _mark_anchor_reply(self, event: LiveEvent) -> None:
        """Close one explicitly targeted audience item without touching audio."""
        if self._interaction_state is None:
            return
        handled = self._interaction_state.mark_anchor_reply(event)
        if not handled:
            return
        keys = self._interaction_state.keys_for_refs(handled)
        if self._selector is not None:
            self._selector.discard_events(keys)
        if self._entries is not None:
            self._entries.discard_events(keys)
        self._proactive.discard_events(handled)

    def _unhandled_events(self, events: tuple[LiveEvent, ...]) -> tuple[LiveEvent, ...]:
        observed = tuple(self._observe_interaction_event(event) for event in events)
        if self._interaction_state is None:
            return observed
        return tuple(event for event in observed if not self._interaction_state.is_handled(event))

    def apply_interaction_report(
        self, report: InteractionReport, *, voice_started_at: float | None = None
    ) -> set[str]:
        """Apply one non-spoken report without starting another model turn."""
        if self._interaction_state is None:
            return set()
        handled = self._interaction_state.apply(report)
        if handled:
            keys = self._interaction_state.keys_for_refs(handled)
            if self._selector is not None:
                self._selector.discard_events(keys)
            if self._entries is not None:
                self._entries.discard_events(keys)
            self._proactive.discard_events(handled)
        self._proactive.set_silenced(self._interaction_state.silenced)
        if report.danmaku_summary.action == "start":
            # The report arrives in the same Realtime response as the
            # streamer turn, so that turn's start is the boundary. Kept as a
            # second producer: the shipped contract asks for the [SUMMARY]
            # head marker instead (request_danmaku_summary below), and a
            # model that still reports finds the backlog already consumed.
            self.request_danmaku_summary(voice_started_at=voice_started_at)
        elif report.danmaku_summary.action == "cancel":
            self._proactive.cancel_danmaku_summary()
        if report.discussion.action == "start":
            self._proactive.collect_opinions(report.discussion.topic, started_at=voice_started_at)
        elif report.discussion.action in ("finish", "cancel"):
            # "finish" means this voice turn already supplied the summary.
            self._proactive.cancel_collection()
        return handled

    def request_danmaku_summary(self, *, voice_started_at: float | None = None) -> SummaryOutcome:
        """Deliver the backlog summary the streamer just delegated by voice.

        Provider-neutral on purpose: the [SUMMARY] head marker reaches here
        from the voice gate on every backend, with no report channel and no
        InteractionState involved. `voice_started_at` is the upper bound of
        the material — what the streamer asked to inspect is what arrived
        BEFORE they started asking, not while they were speaking; without an
        edge the moment of the request stands in.

        The streamer asked for something, so silence is never the answer
        (2026-09-16): no eligible backlog gets one plain "没有新弹幕" line —
        unless a summary went out within the last half minute, in which case
        this is the report channel repeating the marker's delegation and the
        room already heard it.
        """
        now = self._clock.monotonic()
        boundary = voice_started_at if voice_started_at is not None else now
        self._proactive.request_danmaku_summary(before_at=boundary)
        summary = self._proactive.danmaku_summary_intent(now=now)
        if summary is not None:
            self._submit_danmaku_summary(summary)
            self._last_summary_at = now
            return SummaryOutcome.DELIVERED
        # A request with no eligible backlog has nothing to deliver; do not
        # leave it armed for a later, unrelated danmaku.
        self._proactive.cancel_danmaku_summary()
        if (
            self._last_summary_at is not None
            and now - self._last_summary_at < _EMPTY_SUMMARY_GUARD_S
        ):
            return SummaryOutcome.SILENT
        self._submit(self._proactive.empty_danmaku_summary_intent(now=now))
        self.intents_submitted += 1
        self._last_summary_at = now
        return SummaryOutcome.EMPTY

    async def flush_event_observations(self) -> None:
        """Coalesce observation writes without blocking platform intake."""
        if (
            not self._event_input_enabled
            or not self._pending_observations
            or self._observe_context_item is None
        ):
            return
        pending = tuple(self._pending_observations.items())
        async with asyncio.timeout(5):
            await self._observe_context_item(
                observed_events_context_item(tuple(e for _, e in pending))
            )
        for key, event in pending:
            if self._pending_observations.get(key) is event:
                self._pending_observations.pop(key)

    async def _observation_ticker(self) -> None:
        while True:
            await self._clock.sleep(0.5)
            try:
                await self.flush_event_observations()
            except (OSError, RuntimeError, ValueError) as exc:
                log.warning("assembly.event_observation_failed", error_text=str(exc)[:200])

    def note_verdict(self, verdict: Verdict) -> None:
        """A queued welcome is not yet a completed welcome; a danmaku turn
        that spoke, or that the model declined, settles its lines as
        answered."""
        refs = self._danmaku_pending.pop(verdict.intent_id, None)
        if refs is not None and _answered_by(verdict):
            self._proactive.mark_answered(refs)
        identity = self._vip_pending.pop(verdict.intent_id, None)
        if identity is None:
            return
        if verdict.outcome is Outcome.SPOKEN and verdict.phase is Phase.PLAYED:
            self._vip_greeted[identity] = None
            while len(self._vip_greeted) > 4096:
                self._vip_greeted.popitem(last=False)

    async def _write_anchor_context(self, event: LiveEvent) -> None:
        if self._observe_context_item is None:
            return
        try:
            async with asyncio.timeout(5):
                await self._observe_context_item(anchor_danmaku_context_item(event.redacted()))
            self.anchor_context_written += 1
        except (OSError, RuntimeError, ValueError) as exc:
            # Losing one context line must not take the emit path down; the
            # danmaku is already in memory either way.
            log.warning("assembly.anchor_context_write_failed", error_text=str(exc)[:200])

    def set_event_input_enabled(self, enabled: bool) -> None:
        """The pause gate. Off: events still land in memory, the pacer and
        the panel; nothing reaches the selector, the coalescer or the
        scheduler until it is back on."""
        self._event_input_enabled = enabled
        if not enabled:
            self._pending_observations.clear()

    def _submit_event(self, event: LiveEvent, *, gift_members: tuple[LiveEvent, ...] = ()) -> None:
        # Re-checked here because selector winners arrive up to a window
        # later: a switch flipped mid-window must silence the delivery too.
        if not self._speak_enabled(event.kind.value):
            return
        if gift_members:
            # The aggregate is only a presentation/tiering view; observing it
            # as a raw event would change what a previously supplied ref means.
            gift_members = self._unhandled_events(gift_members)
            if not gift_members:
                return
            event = aggregate_gift_events(gift_members)
        else:
            event = self._observe_interaction_event(event)
            if self._interaction_state is not None and self._interaction_state.is_handled(event):
                return
        now = self._clock.monotonic()
        if event.dedup_key and self._direct_ring.contains(event.dedup_key, now):
            self.events_deduped += 1
            return
        if event.kind is EventKind.DANMAKU:
            summary = self._proactive.danmaku_summary_intent((event.redacted(),), now=now)
            if summary is not None:
                self._submit_danmaku_summary(summary)
                if event.dedup_key:
                    self._direct_ring.mark(event.dedup_key, now)
                return
        intent: Intent | None
        if gift_members:
            intent = gift_combo_intent(
                event,
                gift_members,
                now=now,
                max_tokens=self._max_tokens,
                protect_ms=self._protect_ms,
                gift_battery_high=self._gift_battery_high,
                gift_battery_medium=self._gift_battery_medium,
                base_instructions=self.build_event_context() or None,
                protect_paid=self._protect_paid,
            )
        else:
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
            if event.kind is EventKind.VIP_ENTER:
                self._vip_pending[intent.dedup_key] = event.viewer.identity
            submitted = False
            try:
                if event.kind is EventKind.DANMAKU:
                    self._submit_danmaku_turn(intent)
                else:
                    self._submit(intent)
                submitted = True
            finally:
                if not submitted:
                    self._vip_pending.pop(intent.dedup_key, None)
        # Marked only once the delivery is through. Marking on the ATTEMPT meant
        # a raise from _submit left the key burned: the selector's retry (its
        # deliver-then-commit contract exists for exactly that) came back to a
        # ring that now said "already seen", the paid thank-you was dropped, and
        # the books recorded it as delivered.
        if event.dedup_key:
            self._direct_ring.mark(event.dedup_key, now)

    def _submit_danmaku_summary(self, summary: Intent) -> None:
        """Deliver one explicit backlog summary and consume its input batch."""
        keys = {event.dedup_key for event in summary.events if event.dedup_key}
        if self._selector is not None:
            self._selector.discard_events(keys)
        self._submit_danmaku_turn(summary)
        self._proactive.mark_danmaku_summary_used(keys)
        self._proactive.complete_danmaku_summary(summary.dedup_key)
        self.intents_submitted += 1

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

    def set_replay_context(self, context: str | None) -> None:
        """Scope replay prompts without deleting the user's persistent memory."""
        self._replay_context = context
        self._last_pushed = ""
        self._vip_greeted.clear()
        self._vip_pending.clear()
        self._pending_observations.clear()
        self._direct_ring = DedupRing(window_s=30.0, capacity=2048)
        self._threads.reset()
        if self._interaction_state is not None:
            self._interaction_state.reset()
            self._proactive.set_silenced(False)

    def build_public_context(self, *, include_report_rules: bool = True) -> str:
        """The source-neutral half: persona prefix plus the shared dynamic
        tail. Growth injects on ON only — collect grows files silently, off
        contributes nothing at all. Voice and event turns both read exactly
        this material; only the input-rules block differs."""
        if self._replay_context is not None:
            return self._with_interaction_context(
                assemble(self._prefix, DynamicContext(stream_intro=self._replay_context)),
                include_report_rules=include_report_rules,
            )
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
        return self._with_interaction_context(
            assemble(self._prefix, ctx), include_report_rules=include_report_rules
        )

    def _with_interaction_context(self, public_context: str, *, include_report_rules: bool) -> str:
        if self._interaction_state is None:
            return public_context
        return assemble_scoped(
            public_context,
            "\n\n".join(
                (self._interaction_state.context(), REPORT_RULES if include_report_rules else "")
            ).strip(),
        )

    def _with_turn_rules(self, rules: str) -> str:
        context = assemble_scoped(self.build_public_context(include_report_rules=False), rules)
        if self._interaction_state is not None:
            # Keep the message-format block intact; the independent reporting
            # contract follows it rather than competing with its final "end".
            context = assemble_scoped(context, REPORT_RULES)
        return context

    def build_context(self) -> str:
        """What the SESSION carries — the implicit microphone turn's rules.

        The session's instructions govern whatever the provider generates on
        its own VAD turn, and that input is always the streamer's voice, so
        the voice contract rides here.
        """
        return self._with_turn_rules(self._voice_rules)

    def build_event_context(self) -> str:
        """What one event reply carries as its scoped base, or "" when the
        provider has no per-reply channel (volcano) — the per-kind
        instructions still travel, only the full event contract stays home."""
        if not self._per_reply_scope or not self._event_rules:
            return ""
        return self._with_turn_rules(self._event_rules)

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
        tasks = [
            ticker,
            asyncio.create_task(self._observation_ticker(), name="assembly:observations"),
        ]
        if self._selector is not None:
            tasks.append(
                asyncio.create_task(
                    self._selector.run(
                        self.deliver_selected,
                        deliver_batch=self.deliver_danmaku_batch,
                        deliver_gift_batch=self.deliver_gift_batch,
                    ),
                    name="assembly:selector",
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
            "viewer_chat_skipped": self.viewer_chat_skipped,
            "viewer_chat_reasons": dict(self.viewer_chat_reasons),
            "anchor_context_written": self.anchor_context_written,
            "event_input_enabled": self._event_input_enabled,
            "context_chars": len(self._last_pushed),
            "sources": {s.name: ("gave_up" if s.gave_up else "ok") for s in self._supervised},
        }
