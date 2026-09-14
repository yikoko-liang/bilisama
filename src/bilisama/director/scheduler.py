"""The scheduler: one reply slot, seven claimants, no exceptions.

Every provider turned out single-slot (capabilities.py, all three verified),
so this module is load-bearing for the whole product: without it, concurrent
sources race the slot and the audience watches a paid Super Chat go
unanswered. The reference repos have nothing like it — qwen-audio-agent faces
one person and a low-rate notifier; we face a gift storm (plan section 4.2).

What it guarantees, and where each promise is tested:

- At most one reply in flight, ever. The funnel (section 2.7) means the queue
  sees tens per minute, not per second; the heap orders by priority then age.
- Pre-emption: a strictly higher priority cancels the active reply. The victim
  requeues when its intent asks for that (paid events do), otherwise its
  verdict says preempted.
- Barge-in: the streamer speaking cancels any active reply, immediately asks
  L1 to stop playback (the clear goes out before anything else, section 2.5
  sequence 3), and requeues protected work. A reply inside its protection
  window survives barge-in (section 2.7: only panic may kill it).
- Protection lifecycle: a protected reply disarms the provider's own barge-in
  on dispatch (adapter policy); the scheduler re-arms it on settle AND on the
  protect_ms hard cap, whichever lands first — forgetting either half was the
  audit's worst finding (A4).
- panic mute: the red button. Kills the active reply even when protected —
  the only thing allowed to — drains the queue with verdicts, and refuses new
  dispatch until released. A panic landing inside the dispatch window is
  honoured the moment the dispatch completes (A1).
- Every intent ends in exactly one Verdict (section 4.12): the machine-readable
  answer to "why did it not speak just now". Dispatch failures included (A8),
  shutdown included, and the verdict names the gate that held it rather than a
  generic "expired".
- A send that keeps failing is bounded by a deadline, not retried forever
  (section 4.11). Paid intents requeue, and they are also the ones with no
  expires_at of their own, so the retry has to bring its own.
- spoken@played when a playback receipt says the audience heard it out,
  spoken@generating when nobody is reporting playback at all.
- The provider's own VAD turn — the streamer's voice, never an Intent — is
  booked while it lives (_Implicit) and has exactly one kill path,
  skip_implicit: the voice gate, the output guard and panic all go through
  it, so a turn dies once, with one cancel and one verdict (source "voice").
  A completed turn's text reaches implicit_spoken_sink so her own side of the
  microphone conversation is not invisible to memory.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, cast

from bilisama.clock import Clock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent
from bilisama.director.intents import event_context_line, wrap_events
from bilisama.director.interaction_state import InteractionState
from bilisama.ingest.events import EventKind
from bilisama.obs.logging import bind, get_logger
from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict
from bilisama.realtime import link

__all__ = ["PlaybackClear", "Scheduler"]

log = get_logger(__name__)

# How long a provider-initiated cancel may promise its speech edge (the
# done(cancelled)→speech_started frame gap is single-digit milliseconds on a
# healthy link; 0.3s is the safety bound for a shape that never sends it).
_SPEECH_EDGE_GRACE_S = 0.3

# Section 4.11's rule for a send that keeps failing: the bound is a DEADLINE,
# not a retry counter — "a reply retried eight times across ten seconds is
# worse than no reply". Paid intents are the only ones that requeue and they
# carry no expires_at of their own (intents.py:160, deliberately: paid work
# must not go stale in the queue), so the first failure gives them one.
_DISPATCH_RETRY_WINDOW_S = 6.0
# How many dispatched replies the handle->intent reverse index remembers for
# the panel. Well past anything in flight; eviction is dispatch order.
_REPLY_INTENTS_CAP = 128
# And the attempts inside that window are spaced. A failed send means the
# socket is sick, and redispatching into it as fast as the loop allows is not
# a retry, it is a spin: probed 2026-08-25 at roughly 65000 attempts a second
# with zero verdicts, and the loop starved outright when the send raised
# without yielding.
_DISPATCH_RETRY_BACKOFF_S = 1.0

# How long a settled reply waits for the playback receipt that turns
# spoken@generating into spoken@played. Whoever owns the speaker reports
# through floor.on_playback + notify (ui/audio.py's PlaybackTally); a page that
# goes away mid-playback never reports again, so the wait is bounded and the
# verdict falls back to what we actually know.
_PLAYBACK_RECEIPT_GRACE_S = 15.0


class StreamGuard(Protocol):
    """What the scheduler needs from an output guard: OutputGuard fits."""

    def reset(self) -> None: ...

    def hit(self, delta: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PlaybackClear:
    """Ask L1 to stop everything queued and roll the avatar back. Emitted the
    moment an active reply dies; the Electron side (stage 6) consumes it."""

    reason: str


@dataclass(slots=True)
class _Active:
    intent: Intent
    handle: link.ReplyHandle
    # When the sends landed: the verdict's spoken_ms counts from here.
    started_at: float = 0.0
    # Inside this window only panic may kill the reply (paid protection).
    protected_until: float | None = None
    protection_ended: bool = False
    # A PlaybackClear already went out for this reply; the late done(cancelled)
    # must not send a second one under a made-up reason (A10).
    cleared: bool = False
    # The first audio or non-empty text delta flips this. From then on a
    # higher-priority live event QUEUES instead of preempting: cutting a
    # sentence the room already hears mid-word is worse than any priority
    # gap. The streamer's own barge-in and panic are separate hard paths.
    output_started: bool = False
    text: str = ""
    host_interrupted: bool = False


@dataclass(slots=True)
class _Parked:
    """A reply that finished generating while L1 still held its audio.

    Its verdict is written when the playback drains (spoken@played) or when the
    grace runs out (spoken@generating) — see _flush_played.
    """

    intent: Intent
    handle: link.ReplyHandle
    text: str
    started_at: float
    deadline: float


@dataclass(slots=True)
class _Implicit:
    """The provider's own VAD turn while it lives.

    Opened on its ReplyStarted, closed on its ReplyDone. `killed` is what makes
    skip_implicit idempotent across its three callers — the gate decides in
    the fan-out's task and the guard in this one, so both can land for the
    same reply in either order. `cleared` guards the PlaybackClear the same
    way _Active.cleared does (A10).
    """

    handle: link.ReplyHandle
    started_at: float
    killed: bool = False
    cleared: bool = False
    cancel_sent: bool = False
    text: str = ""
    host_interrupted: bool = False


@dataclass(slots=True)
class _Entry:
    sort_key: tuple[int, int]
    intent: Intent

    def __lt__(self, other: _Entry) -> bool:
        # Heap order by (priority, age) only — Intents are not comparable.
        return self.sort_key < other.sort_key


class Scheduler:
    """Single consumer of the link's events, single writer of the reply slot."""

    def __init__(
        self,
        speech: link.SpeechLink,
        floor: SpeakingFloor,
        clock: Clock,
        *,
        verdict_sink: Callable[[Verdict], None] | None = None,
        quiet_after_speech_s: float = 1.1,
        cooldown_s: float = 0.0,
        guard: StreamGuard | Callable[[str], bool] | None = None,
        on_hit: Literal["drop_sentence", "mute_all"] = "drop_sentence",
        spoken_sink: Callable[[str], None] | None = None,
        implicit_spoken_sink: Callable[[str], None] | None = None,
        interaction_state: InteractionState | None = None,
        on_interrupted: Callable[[Intent | None, link.ReplyHandle, str], None] | None = None,
    ) -> None:
        self._speech = speech
        self._floor = floor
        self._clock = clock
        self._verdicts: list[Verdict] = []
        self._model_skips: OrderedDict[int, tuple[str, bool]] = OrderedDict()
        self._verdict_sink = verdict_sink or self._verdicts.append
        self._quiet_after_speech_s = quiet_after_speech_s
        self._cooldown_s = cooldown_s
        self._guard = _as_stream_guard(guard)
        self._on_hit = on_hit
        # Receives every cleanly completed reply text — the distiller collects
        # them as voice-exemplar raw material (section 4.6). Interrupted or
        # guard-killed replies never reach it, which IS the quality filter.
        self._spoken_sink = spoken_sink
        # The same for her own microphone turns, completed and not killed.
        # Separate from spoken_sink so an entry point can wire one and not
        # the other; dev-talk wires both to the same line.
        self._implicit_spoken_sink = implicit_spoken_sink
        self._interaction_state = interaction_state
        self._interaction_errors: set[str] = set()
        self._interaction_inflight: Intent | None = None
        self._on_interrupted = on_interrupted
        self._interrupted_handles: OrderedDict[int, None] = OrderedDict()
        self._pending_interruptions: OrderedDict[
            int, tuple[Intent | None, link.ReplyHandle, str, float]
        ] = OrderedDict()
        self._implicit_playback: _Implicit | None = None
        self._implicit: _Implicit | None = None
        self._heap: list[_Entry] = []
        self._seq = itertools.count()
        # Dedup keys live from submit until SETTLE, not until dispatch: the
        # same gift must not be thanked twice just because the first thanks is
        # still playing (A15).
        self._queued_keys: set[str] = set()
        self._revoked: set[str] = set()
        self._active: _Active | None = None
        # Replies waiting for their playback receipt before their verdict is
        # written. At most one at a time in practice: the floor's queued_audio
        # gate holds the next dispatch until this one's audio is gone.
        self._parked: list[_Parked] = []
        self._dispatching = False
        self._panicked = False
        # Set between LinkDown and LinkUp. Work that arrives in that window is
        # skipped with a verdict, not held: danmaku expires in 20 seconds
        # anyway, and a reply that lands four minutes after its trigger reads
        # worse than no reply (user's call, 2026-08-17).
        self._link_down = False
        # Which gate we last WROTE A LINE about, so a polled loop does not
        # write it again every wake-up. See _note_gate.
        self._gate_logged: SkipReason | None = None
        self._wake = asyncio.Event()
        self.controls: asyncio.Queue[PlaybackClear] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        # handle_id -> the Intent whose reply it is; see _dispatch.
        self._reply_intents: OrderedDict[int, Intent] = OrderedDict()

    # ------------------------------------------------------------ intake

    def refresh_interactions(self) -> None:
        """Refresh waiting work only; later typed answers never kill live audio."""
        retained: list[_Entry] = []
        for entry in self._heap:
            if entry.intent.dedup_key in self._revoked:
                self._drop_queued(entry.intent, SkipReason.REVOKED, outcome=Outcome.EXPIRED)
                continue
            intent = self._filter_interactions(entry.intent)
            if intent is not None:
                retained.append(_Entry(entry.sort_key, intent))
        self._heap = retained
        heapq.heapify(self._heap)
        self._sync_interaction_retention()
        self._wake.set()

    def _sync_interaction_retention(self) -> None:
        """Protect state only while a real queued/in-flight task references it."""
        if self._interaction_state is None:
            return
        intents = [entry.intent for entry in self._heap]
        intents.extend(parked.intent for parked in self._parked)
        if self._active is not None:
            intents.append(self._active.intent)
        if self._interaction_inflight is not None:
            intents.append(self._interaction_inflight)
        self._interaction_state.retain_events(
            event
            for intent in intents
            for event in (intent.events or ((intent.event,) if intent.event is not None else ()))
        )

    def _filter_interactions(
        self, intent: Intent, *, owns_key: bool = True, phase: Phase = Phase.QUEUED
    ) -> Intent | None:
        state = self._interaction_state
        if state is None:
            return intent
        key = intent.dedup_key or intent.source
        try:
            filtered = state.filter_intent(intent)
        except ValueError as exc:
            # A malformed mixed batch stays held rather than speaking handled
            # members or losing its unanswered ones. Other work can proceed.
            if key not in self._interaction_errors:
                log.warning("scheduler.interaction_filter_failed", error_text=str(exc)[:200])
            self._interaction_errors.add(key)
            return intent
        self._interaction_errors.discard(key)
        if filtered is intent:
            return intent
        if owns_key:
            self._free_key(intent)
        self._emit(
            Verdict(
                intent_id=key,
                source=intent.source,
                outcome=Outcome.SKIPPED,
                phase=phase,
                reason=SkipReason.HOST_HANDLED,
                detail="主播已处理" if filtered is None else "已答部分移除，未答项保留",
                waited_s=self._waited_s(intent, self._clock.monotonic()),
            )
        )
        if filtered is not None and filtered.dedup_key:
            if filtered.dedup_key in self._queued_keys:
                self._emit(
                    Verdict(
                        intent_id=filtered.dedup_key,
                        source=filtered.source,
                        outcome=Outcome.SKIPPED,
                        phase=phase,
                        reason=SkipReason.DUPLICATE,
                    )
                )
                return None
            if owns_key:
                self._queued_keys.add(filtered.dedup_key)
        return filtered

    def _interaction_gate(self, intent: Intent) -> SkipReason | None:
        if (intent.dedup_key or intent.source) in self._interaction_errors:
            return SkipReason.HOST_HANDLING
        state = self._interaction_state
        if state is None or not state.blocks(intent):
            return None
        return SkipReason.INTERACTION_SILENCE if state.silenced else SkipReason.HOST_HANDLING

    def reply_intent(self, handle_id: int) -> Intent | None:
        """The intent behind one reply handle, for the UI's reference blocks.

        None for the provider's implicit microphone turns — those have no
        intent, which is itself the answer (source "voice").
        """
        return self._reply_intents.get(handle_id)

    def set_cooldown(self, seconds: float) -> None:
        """Retune the post-reply cooldown live. 0 disables it — the event
        pacer's budget owns ordinary-speech rate then."""
        self._cooldown_s = max(0.0, seconds)
        self._wake.set()

    async def _write_history(self, text: str) -> None:
        try:
            await self._speech.add_context_item(text, role="assistant")
        except Exception as exc:
            # A clean spoken reply must not be re-booked as a failure because
            # the history write missed; she just may repeat herself sooner.
            log.warning("scheduler.history_write_failed", error_text=str(exc)[:200])

    async def _write_delivery_status(self, intent: Intent, status: str) -> None:
        """Write factual delivery state, never a semantic handled-event guess."""
        events = intent.events or ((intent.event,) if intent.event is not None else ())
        if not events:
            return
        text = wrap_events(
            [
                f"回复状态记录：{status}。不是新事件，不要求立即回复。",
                *(event_context_line(event) for event in events),
            ]
        )
        try:
            async with asyncio.timeout(5):
                await self._speech.add_context_item(text)
        except (OSError, RuntimeError, ValueError) as exc:
            log.warning("scheduler.delivery_status_failed", error_text=str(exc)[:200])

    def submit(self, intent: Intent) -> None:
        """Queue an intent. Duplicates (same dedup_key while queued or active)
        are skipped with a verdict rather than silently dropped."""
        if self._link_down:
            self._emit(
                Verdict(
                    intent_id=intent.dedup_key or intent.source,
                    source=intent.source,
                    outcome=Outcome.SKIPPED,
                    phase=Phase.SELECTED,
                    reason=SkipReason.LINK_DOWN,
                )
            )
            return
        if self._panicked:
            self._emit(
                Verdict(
                    intent_id=intent.dedup_key or intent.source,
                    source=intent.source,
                    outcome=Outcome.SKIPPED,
                    phase=Phase.SELECTED,
                    reason=SkipReason.PANIC_MUTE,
                )
            )
            return
        if intent.dedup_key and intent.dedup_key in self._queued_keys:
            active = self._active
            with bind(intent_id=intent.dedup_key):
                # The verdict says "duplicate"; this says what it collided
                # WITH. A copy still in the heap and a copy still being spoken
                # read the same on the panel, and only the second is A15 — the
                # gift that must not be thanked twice while the first thanks
                # is still playing.
                log.debug(
                    "scheduler.dedup_dropped",
                    source=intent.source,
                    queue_depth=len(self._heap),
                    active_source=active.intent.source if active is not None else "",
                )
            self._emit(
                Verdict(
                    intent_id=intent.dedup_key,
                    source=intent.source,
                    outcome=Outcome.SKIPPED,
                    phase=Phase.SELECTED,
                    reason=SkipReason.DUPLICATE,
                )
            )
            return
        filtered = self._filter_interactions(intent, owns_key=False, phase=Phase.SELECTED)
        if filtered is None:
            return
        intent = filtered
        heapq.heappush(self._heap, _Entry((-int(intent.priority), next(self._seq)), intent))
        self._sync_interaction_retention()
        if intent.dedup_key:
            self._queued_keys.add(intent.dedup_key)
        with bind(intent_id=intent.dedup_key or intent.source):
            # debug, not info: this fires once per intent that survived the
            # funnel, which is tens per minute in a busy room. It is the line
            # that turns 「这条弹幕根本没进来吧」 into a yes or a no, and the
            # depth beside it says how long the line ahead of it was.
            log.debug(
                "scheduler.submitted",
                source=intent.source,
                priority=int(intent.priority),
                queue_depth=len(self._heap),
                has_item=intent.injection.item_text is not None,
                requeue_on_interrupt=intent.requeue_on_interrupt,
                events=intent.events,
            )
        self._maybe_preempt(intent)
        self._wake.set()

    def revoke(self, dedup_key: str) -> bool:
        """Withdraw a queued intent — the super-chat-delete path.

        Only queued work is pulled: an answer already being spoken finishes,
        because cutting a thank-you mid-sentence sounds worse on stream than
        thanking a withdrawn SC.
        """
        if not dedup_key or dedup_key in self._revoked:
            return False
        if not any(entry.intent.dedup_key == dedup_key for entry in self._heap):
            return False
        self._revoked.add(dedup_key)
        self._wake.set()
        return True

    async def drain_pending_io(self) -> None:
        """Finish owned writes/cancels before replacing the model session."""
        if not self._panicked:
            raise RuntimeError("清理测试会话前必须暂停调度")
        while self._dispatching:
            await asyncio.sleep(0.01)
        self._settle_active(Outcome.CANCELLED, Phase.GENERATING, reason=SkipReason.PANIC_MUTE)
        self._implicit = None
        self._implicit_playback = None
        self._pending_interruptions.clear()
        self._interrupted_handles.clear()
        self._link_down = False
        while self._tasks:
            pending = tuple(self._tasks)
            for task in pending:
                if task.get_name() in ("scheduler:protect-cap", "scheduler:playback-grace"):
                    task.cancel()
            results = await asyncio.gather(*pending, return_exceptions=True)
            # gather() can return synchronously for already-finished tasks,
            # before their discard callbacks run. Do not spin on that set.
            self._tasks.difference_update(task for task in pending if task.done())
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise RuntimeError("上一轮测试尚未清理完成，请重试") from result

    def panic_mute(self) -> None:
        """Kill everything, protected included — the one switch allowed to."""
        victim = self._active
        # Before the drain, or the counts all read zero and the line stops
        # answering the question the red button raises afterwards: 「刚才那一下
        # 扔掉了什么」. Every dropped intent still gets its own verdict below;
        # this is the one line that says they went together and why.
        log.info(
            "scheduler.panic_muted",
            queued=len(self._heap),
            parked=len(self._parked),
            active_source=victim.intent.source if victim is not None else "",
            active_protected=victim is not None and self._protection_active(victim),
            implicit_active=self._implicit is not None,
        )
        self._panicked = True
        if not self._dispatching:
            self._interaction_inflight = None
        self._pending_interruptions.clear()
        self._implicit_playback = None
        self.controls.put_nowait(PlaybackClear(reason="panic_mute"))
        # The clear above takes the parked replies' audio with it, so no
        # receipt is coming: write what we know rather than wait for it.
        self._flush_played(played=False)
        self._drain_queue(SkipReason.PANIC_MUTE)
        active = self._active
        if active is not None:
            active.cleared = True
            self._spawn(self._speech.cancel(active.handle), name="scheduler:panic-cancel")
        implicit = self._implicit
        if implicit is not None:
            # Her own turn dies with everything else — the red button used to
            # leave it talking to the end. The clear above already emptied
            # both speakers, so this one needs only the cancel and a verdict.
            implicit.cleared = True
            self.skip_implicit(
                implicit.handle,
                reason=SkipReason.PANIC_MUTE,
                outcome=Outcome.CANCELLED,
                phase=Phase.SPEAKING,
            )
        # An in-flight dispatch (self._dispatching) has no handle to cancel
        # yet; the post-dispatch recheck honours the flag the moment it lands.
        self._wake.set()

    def notify(self) -> None:
        """Re-examine the queue: something a gate depends on has changed.

        The dispatch loop sleeps on state gates until an event wakes it, and
        playback finishing is not an event the link ever sends — whoever owns
        the speaker has to say so.
        """
        self._wake.set()

    def release_panic(self) -> None:
        log.info("scheduler.panic_released", queued=len(self._heap))
        self._panicked = False
        self._wake.set()

    def skip_reply(
        self,
        handle: link.ReplyHandle,
        *,
        detail: str = "",
        clear_playback: bool = False,
        preserve_generation: bool = False,
    ) -> None:
        """Apply the same model SKIP contract to voice and dispatched replies.

        The fanout can decide before request_reply returns. Remember that
        decision by handle, then settle it as soon as dispatch books the slot.
        A model decline is terminal, never an interruption to requeue.
        """
        if handle.implicit:
            self.skip_implicit(
                handle,
                reason=SkipReason.VOICE_NOT_ADDRESSED,
                detail=detail,
                clear_playback=clear_playback,
                preserve_generation=preserve_generation,
            )
            return
        self._model_skips[handle.handle_id] = (detail, clear_playback)
        while len(self._model_skips) > 64:
            self._model_skips.popitem(last=False)
        self._apply_model_skip(handle)

    def _apply_model_skip(self, handle: link.ReplyHandle) -> bool:
        active = self._active
        decision = self._model_skips.get(handle.handle_id)
        if active is None or active.handle is not handle or decision is None:
            return False
        detail, clear_playback = decision
        if clear_playback and not active.cleared:
            active.cleared = True
            self.controls.put_nowait(PlaybackClear(reason="model_declined"))
        self._spawn(self._speech.cancel(handle), name="scheduler:model-decline")
        self._settle_active(
            Outcome.SKIPPED, Phase.GENERATING, reason=SkipReason.MODEL_DECLINED, detail=detail
        )
        return True

    def skip_implicit(
        self,
        handle: link.ReplyHandle,
        *,
        reason: SkipReason,
        detail: str = "",
        clear_playback: bool = False,
        outcome: Outcome = Outcome.SKIPPED,
        phase: Phase = Phase.GENERATING,
        preserve_generation: bool = False,
    ) -> None:
        """Kill the provider's own VAD turn — the one path that does.

        Three callers: the voice gate (the streamer is not talking to her),
        the output guard (a blocked word in her own turn) and panic. A handle
        that is not the provider's own turn, or one already finished (stale),
        is ignored, so a late call cannot cancel whoever holds the slot by
        then — the link's cancel() only sends for a live record of this very
        handle anyway. Idempotent on the record — the second caller finds
        `killed` set and does nothing — and the link's own bookkeeping makes a
        second cancel frame impossible regardless.

        Args:
            handle: The turn's handle, as seen on its ReplyStarted.
            reason: Which caller, in the shared vocabulary.
            detail: What the panel shows next to the reason (the gate's
                ruling); empty for the guard on purpose — the blocked word
                stays out of the record, as it does for dispatched replies.
            clear_playback: The audience may already be hearing this turn
                (the gate's hold timed out, the guard fired mid-sentence), so
                ask L1 to flush. The gate's normal path passes False: nothing
                of a held turn ever reached a speaker.
            outcome, phase: The verdict's pair. The gate's default reads
                skipped@generating; the guard and panic write their own.
            preserve_generation: Audio is already gated; let this same reply
                finish its nonspoken state report. Panic still cancels it.
        """
        implicit = self._implicit
        if implicit is None or implicit.handle is not handle:
            if not handle.implicit or handle.stale:
                return
            # The gate decides inside the fan-out's pump, and the pump can
            # read a turn's ReplyStarted and its first delta out of one queue
            # without yielding — so the kill can land here before this task
            # has seen the turn start. Book it now, already killed; the start
            # that arrives later keeps this record (_open_implicit), and the
            # done closes it as usual. A stale handle is a finished turn: the
            # link would not cancel it and there is nothing left to book.
            implicit = _Implicit(handle=handle, started_at=self._clock.monotonic())
            self._implicit = implicit
        if implicit.killed:
            if not preserve_generation and not implicit.cancel_sent and not handle.stale:
                implicit.cancel_sent = True
                self._spawn(self._speech.cancel(handle), name="scheduler:implicit-cancel")
            return
        implicit.killed = True
        if clear_playback and not implicit.cleared:
            implicit.cleared = True
            self.controls.put_nowait(PlaybackClear(reason=_clear_reason(reason)))
        if not preserve_generation:
            implicit.cancel_sent = True
            self._spawn(self._speech.cancel(handle), name="scheduler:implicit-cancel")
        spoken_ms = self._spoken_ms(implicit.started_at)
        intent_id = f"voice:{handle.handle_id}"
        with bind(intent_id=intent_id):
            log.info(
                "scheduler.implicit_killed",
                reason=str(reason),
                detail=detail,
                spoken_ms=spoken_ms,
                cleared=implicit.cleared,
            )
        self._emit(
            Verdict(
                intent_id=intent_id,
                source="voice",
                outcome=outcome,
                phase=phase,
                reason=reason,
                detail=detail,
                spoken_ms=spoken_ms,
            )
        )

    def _emit(self, verdict: Verdict) -> None:
        """Every intent's one terminal record, to the sink AND to the log.

        The sink is whatever the entry point wired — dev-talk prints the
        exceptions and pushes the lot to the panel's timeline. The log line is
        the half that outlives the session, and it is written here rather than
        in the sink so it does not depend on which entry point is running:
        section 4.12's promise is that the answer to 「为什么刚才没说话」 is
        recorded, not that one particular front end chose to record it.

        Bound to intent_id so every line this dispatch produced — the sends,
        the gate, the barge-in — can be pulled out together afterwards. The
        contextvar covers only what runs inside this call; the id is on the
        line either way because Verdict carries it.
        """
        with bind(intent_id=verdict.intent_id):
            log.info(
                "scheduler.verdict",
                source=verdict.source,
                outcome=str(verdict.outcome),
                phase=str(verdict.phase),
                reason=str(verdict.reason) if verdict.reason else "",
                waited_s=round(verdict.waited_s, 2),
                spoken_ms=verdict.spoken_ms,
                detail=verdict.detail,
            )
        self._verdict_sink(verdict)

    @property
    def verdicts(self) -> list[Verdict]:
        """Verdicts collected by the default sink (tests read these)."""
        return self._verdicts

    def status(self) -> dict[str, Any]:
        """The health probe's view (plan section 4.12)."""
        active = self._active
        return {
            "panicked": self._panicked,
            "queued": len(self._heap),
            "active_source": active.intent.source if active else None,
            "dispatching": self._dispatching,
            "implicit_active": self._implicit is not None,
        }

    # ------------------------------------------------------------ the loop

    async def run(self) -> None:
        """Consume link events and dispatch queued intents. Cancel to stop."""
        event_task = asyncio.create_task(self._event_loop(), name="scheduler:events")
        try:
            await self._dispatch_loop()
        finally:
            # Shutdown is not an exemption from section 4.12: whatever is still
            # in the heap when Ctrl-C lands would otherwise end nowhere, and the
            # panel's counts stop adding up. No SkipReason names "the process
            # left" — skipped@queued says it without inventing one.
            self._flush_played(played=False)
            self._drain_queue(None)
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    async def _event_loop(self) -> None:
        async for event in self._speech.events():
            try:
                self._handle_event(event)
            except Exception as exc:
                # This task is the only consumer of link events, so one bad
                # frame must cost that frame and nothing else. When it died
                # here instead, the LinkDown behind it was never read: the
                # floor stayed shut and every later intent sat in the heap
                # without a verdict, against the module docstring's contract.
                #
                # `frame` rather than `event` because that is what it is — the
                # link frame being handled, not an event name. A field called
                # `event` used to raise TypeError right here, inside the very
                # handler meant to keep this task alive; EventLogger takes the
                # event name positional-only now, so the trap is gone and this
                # name is a choice again.
                log.exception(
                    "scheduler.event_failed",
                    frame=type(event).__name__,
                    error_text=str(exc)[:200],
                )
            self._wake.set()

    def _handle_event(self, event: link.LinkEvent) -> None:
        """React to one link event.

        Synchronous on purpose: every reply-killing send below is spawned, so
        nothing here can stall — or take down — the stream's only consumer.
        """
        if isinstance(event, link.SpeechStarted):
            if not self._floor.queued_audio:
                # A prior drained receipt belongs to the finished utterance,
                # not to this later speech edge. Do not recover it again.
                self._flush_played(played=True)
            self._floor.on_speech_started()
            self._barge_in()
        elif isinstance(event, link.SpeechStopped):
            self._floor.on_speech_stopped(quiet_s=self._quiet_after_speech_s)
        elif isinstance(event, link.ReplyStarted):
            # A reply we never dispatched is the provider's implicit turn:
            # hold the floor for its whole life, or a queued intent lands
            # in the rule-5 shared-response-id trap (A2).
            if self._active is None or event.handle is not self._active.handle:
                self._floor.on_implicit(True)
            if event.handle.implicit:
                self._open_implicit(event.handle)
        elif isinstance(event, link.ReplyAudioDelta):
            self._mark_output_started(event.handle)
        elif isinstance(event, link.ReplyTextDelta):
            self._on_delta(event)
        elif isinstance(event, link.ReplyDone):
            if (self._active is None or event.handle is not self._active.handle) and (
                self._implicit is None or self._implicit.handle is event.handle
            ):
                self._floor.on_implicit(False)
            if event.handle.implicit:
                self._close_implicit(event)
            self._on_done(event)
        elif isinstance(event, link.LinkDown):
            # The transport already settled every record (FAILED dones are
            # on their way). Reset every flag this link was feeding, or the
            # floor stays shut forever, and drain the queue: holding work
            # for a link that may be gone for good just produces stale
            # replies later.
            self._link_down = True
            self._pending_interruptions.clear()
            self._implicit_playback = None
            self._floor.on_link_lost()
            self._drain_queue(SkipReason.LINK_DOWN)
        elif isinstance(event, link.LinkUp):
            self._link_down = False

    async def _dispatch_loop(self) -> None:
        while True:
            self._wake.clear()
            self._flush_played()
            intent = self._next_dispatchable()
            if intent is None:
                wait = self._floor.blocked_for() if self._heap and self._active is None else 0.0
                if wait > 0:
                    # Blocked purely by a time gate: sleep to its release on the
                    # injected clock, so FakeClock tests stay deterministic.
                    sleep = asyncio.create_task(self._clock.sleep(wait))
                    wake = asyncio.create_task(self._wake.wait())
                    await asyncio.wait({sleep, wake}, return_when=asyncio.FIRST_COMPLETED)
                    for t in (sleep, wake):
                        t.cancel()
                    await asyncio.gather(sleep, wake, return_exceptions=True)
                else:
                    await self._wake.wait()
                continue
            await self._dispatch(intent)

    def _next_dispatchable(self) -> Intent | None:
        if self._panicked or self._active is not None or self._dispatching:
            return None
        if self._link_down:
            return None
        held: list[_Entry] = []
        try:
            while self._heap:
                entry = heapq.heappop(self._heap)
                intent = entry.intent
                if intent.dedup_key and intent.dedup_key in self._revoked:
                    self._revoked.discard(intent.dedup_key)
                    self._drop_queued(intent, SkipReason.REVOKED, outcome=Outcome.EXPIRED)
                    continue
                filtered = self._filter_interactions(intent)
                if filtered is None:
                    continue
                intent = filtered
                entry = _Entry(entry.sort_key, intent)
                if self._expired(intent):
                    gate = self._interaction_gate(intent) or self._floor.blocking_reason()
                    self._drop_queued(
                        intent,
                        gate,
                        outcome=Outcome.EXPIRED,
                        phase=Phase.GATED if gate is not None else Phase.QUEUED,
                    )
                    continue
                gate = self._floor.blocking_reason()
                if gate is not None:
                    held.append(entry)
                    self._note_gate(gate, intent)
                    return None
                interaction_gate = self._interaction_gate(intent)
                if interaction_gate is not None:
                    held.append(entry)
                    self._note_gate(interaction_gate, intent)
                    continue
                self._gate_logged = None
                self._interaction_inflight = intent
                return intent
        finally:
            # Processing one event must not hold unrelated, ready work behind
            # it; retain each blocked entry's original priority and age.
            for entry in held:
                heapq.heappush(self._heap, entry)
            self._sync_interaction_retention()
        if held:
            return None
        # Nothing waiting means nothing is being held back — whatever gate was
        # reported has stopped costing anyone their turn.
        self._gate_logged = None
        return None

    def _note_gate(self, gate: SkipReason, top: Intent) -> None:
        """Say which gate is sitting on the queue — once per change, not per poll.

        This loop re-runs on every link event, text deltas included, so an
        unlatched line here would be the busiest thing in the process and would
        repeat one unchanging fact a hundred times. What answers 「为什么刚才没
        说话」 is the TRANSITION: the gate that closed, and who it closed on.
        The latch clears when the floor opens or the queue empties, so a second
        stall for the same reason still gets its own line.

        Args:
            gate: The reason `SpeakingFloor.blocking_reason` just returned.
            top: The intent at the head of the heap — the one paying for it.
        """
        if gate is self._gate_logged:
            return
        self._gate_logged = gate
        log.info(
            "scheduler.gate_blocked",
            reason=str(gate),
            queue_depth=len(self._heap),
            top_source=top.source,
            top_priority=int(top.priority),
            # 0.0 for the state gates: those release on an event, not a clock,
            # and promising a wait that no timer will honour reads worse than
            # admitting we cannot say.
            blocked_for_s=round(self._floor.blocked_for(), 2),
        )

    async def _dispatch(self, intent: Intent) -> None:
        """Keep event state alive throughout both provider writes."""
        self._interaction_inflight = intent
        self._sync_interaction_retention()
        try:
            await self._dispatch_intent(intent)
        finally:
            self._interaction_inflight = None
            self._sync_interaction_retention()

    async def _dispatch_intent(self, intent: Intent) -> None:
        """Send one intent to the provider, honouring everything that fired
        while the sends were in flight (A1) and turning a failed send into a
        verdict instead of a dead scheduler (A8)."""
        ready = self._prepare_interaction_dispatch(intent, item_written=False)
        if ready is None:
            return
        intent = ready
        self._dispatching = True
        wrote_item = False
        try:
            if intent.injection.item_text is not None:
                await self._speech.add_context_item(intent.injection.item_text)
                wrote_item = True
            ready = self._prepare_interaction_dispatch(intent, item_written=wrote_item)
            if ready is None:
                return
            intent = ready
            handle = await self._speech.request_reply(intent.injection.reply)
        except Exception as exc:
            log.warning(
                "scheduler.dispatch_failed", source=intent.source, error_text=str(exc)[:200]
            )
            self._free_key(intent)
            if intent.requeue_on_interrupt:
                # Paid work does not evaporate because one send failed — the
                # audience watched them pay. Same rule as barge-in (section
                # 4.2): back in the queue, and the key was freed above so the
                # resubmission is not eaten as a duplicate.
                #
                # Bounded, though: the first failure hands the intent the
                # deadline it never had, later ones inherit it, and once it
                # passes _next_dispatchable drops the intent with a verdict.
                # The backoff below spaces the attempts inside that window;
                # _dispatching stays set through it on purpose, because this
                # attempt still holds the slot.
                deadline = intent.expires_at
                if deadline is None:
                    deadline = self._clock.monotonic() + _DISPATCH_RETRY_WINDOW_S
                with bind(intent_id=intent.dedup_key or intent.source):
                    # scheduler.dispatch_failed above says the send broke; this
                    # says what happens next, which is the part a repeating
                    # failure makes urgent — how much runway is left before
                    # section 4.11's deadline retires the paid intent.
                    log.debug(
                        "scheduler.dispatch_retry",
                        source=intent.source,
                        item_written=wrote_item,
                        deadline_in_s=round(deadline - self._clock.monotonic(), 2),
                    )
                await self._clock.sleep(_DISPATCH_RETRY_BACKOFF_S)
                self._requeue(intent, item_written=wrote_item, deadline=deadline)
                return
            self._emit(
                Verdict(
                    intent_id=intent.dedup_key or intent.source,
                    source=intent.source,
                    outcome=Outcome.FAILED,
                    phase=Phase.DISPATCHED,
                    detail=str(exc)[:120],
                )
            )
            return
        finally:
            self._dispatching = False

        active = _Active(intent=intent, handle=handle, started_at=self._clock.monotonic())
        reply = intent.injection.reply
        if reply.protected:
            active.protected_until = self._clock.monotonic() + reply.protect_ms / 1000.0
            self._spawn(self._protection_cap(active), name="scheduler:protect-cap")
        self._active = active
        # The UI's reverse index: reply.delta frames only carry the handle,
        # and the fanout consumer reads them AFTER this slot may already be
        # cleared — the map is what lets the panel say which danmaku a reply
        # answered. Bounded; eviction order is insertion, which is dispatch
        # order.
        self._reply_intents[handle.handle_id] = intent
        while len(self._reply_intents) > _REPLY_INTENTS_CAP:
            self._reply_intents.popitem(last=False)
        self._floor.on_reply_active(True)
        if self._apply_model_skip(handle):
            return
        if self._guard is not None:
            self._guard.reset()

        with bind(intent_id=intent.dedup_key or intent.source):
            # The 「她开口了」 line, and the counterpart to every gate_blocked
            # above it: whoever won the slot, what it outranked, and whether
            # the two-step actually wrote its history half. Written after the
            # state is committed, so a recheck below that kills it lands
            # AFTER it in the file and the order reads true.
            log.info(
                "scheduler.dispatched",
                source=intent.source,
                priority=int(intent.priority),
                protected=reply.protected,
                protect_ms=reply.protect_ms if reply.protected else 0,
                has_item=wrote_item,
                queue_depth=len(self._heap),
            )

        # ---- post-dispatch rechecks: what fired during the await window ----
        if self._panicked:
            active.cleared = True
            self._spawn(self._speech.cancel(handle), name="scheduler:panic-cancel")
            self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED, reason=SkipReason.PANIC_MUTE)
            return
        if self._floor.streamer_speaking and not self._protection_active(active):
            self._note_barge(active, where="post_dispatch")
            active.cleared = True
            self.controls.put_nowait(PlaybackClear(reason="barge_in"))
            self._spawn(self._speech.cancel(handle), name="scheduler:barge-cancel")
            self._record_interruption(intent, handle, active.text, confirmed=True)
            if intent.requeue_on_interrupt:
                self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED, requeue=True)
            else:
                self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED)
            return
        if self._heap:
            top = self._heap[0].intent
            if int(top.priority) > int(intent.priority):
                self._maybe_preempt(top)

    def _prepare_interaction_dispatch(self, intent: Intent, *, item_written: bool) -> Intent | None:
        """Recheck only before generation; later typed context is not a cancel."""
        filtered = self._filter_interactions(intent)
        if filtered is None:
            return None
        gate = self._interaction_gate(filtered)
        if gate is None:
            return filtered
        self._note_gate(gate, filtered)
        self._free_key(filtered)
        self._requeue(filtered, item_written=item_written)
        return None

    # ------------------------------------------------------------ events

    def _mark_output_started(self, handle: link.ReplyHandle) -> None:
        active = self._active
        if active is not None and handle is active.handle:
            active.output_started = True

    def _open_implicit(self, handle: link.ReplyHandle) -> None:
        # Every adapter emits ReplyStarted ahead of the turn's first delta
        # (client.py's three first-frame emits, volcano's _handle_for), so
        # this is where a turn is booked — unless skip_implicit got here
        # first from the gate, in which case the killed record stands. A
        # delta whose handle was never booked is a late frame and
        # _guard_implicit ignores it.
        if self._implicit is not None and self._implicit.handle is handle:
            return
        self._implicit = _Implicit(handle=handle, started_at=self._clock.monotonic())
        if self._guard is not None:
            self._guard.reset()

    def _close_implicit(self, event: link.ReplyDone) -> None:
        implicit = self._implicit
        if implicit is None or implicit.handle is not event.handle:
            return
        self._implicit = None
        if event.text:
            implicit.text = event.text[:4000]
        if not implicit.killed and event.status is link.ReplyStatus.CANCELLED:
            self._record_interruption(
                None,
                event.handle,
                implicit.text,
                confirmed=implicit.host_interrupted or self._floor.streamer_speaking,
            )
        if implicit.killed or event.status is not link.ReplyStatus.COMPLETED:
            # Killed turns already have their verdict; a turn the provider
            # itself cut (the streamer spoke over her) gets none — the panel
            # shows the barge-in, and there is no intent to account for.
            return
        if implicit.host_interrupted:
            return
        if self._floor.queued_audio:
            self._implicit_playback = implicit
        if self._implicit_spoken_sink is not None and event.text:
            self._implicit_spoken_sink(event.text)

    def _guard_implicit(self, event: link.ReplyTextDelta) -> None:
        implicit = self._implicit
        if implicit is None or implicit.handle is not event.handle or implicit.killed:
            return
        if self._guard is None:
            return
        if self._guard.hit(event.text) is None:
            return
        # Same treatment as a dispatched reply: kill the sentence, claw back
        # what played, escalate when configured. The audience may be hearing
        # this one already — the gate only holds a turn for its first frames.
        self.skip_implicit(
            event.handle,
            reason=SkipReason.OUTPUT_BLOCKED,
            clear_playback=True,
            outcome=Outcome.FAILED,
            phase=Phase.SPEAKING,
        )
        if self._on_hit == "mute_all":
            self.panic_mute()

    def _on_delta(self, event: link.ReplyTextDelta) -> None:
        if event.handle.implicit:
            implicit = self._implicit
            if implicit is not None and implicit.handle is event.handle and not implicit.killed:
                implicit.text = (implicit.text + event.text)[:4000]
            self._guard_implicit(event)
            return
        active = self._active
        if active is None or event.handle is not active.handle:
            return
        if event.text:
            active.output_started = True
            active.text = (active.text + event.text)[:4000]
        if self._guard is None:
            return
        word = self._guard.hit(event.text)
        if word is None:
            return
        # A hit mid-stream: kill the sentence and claw back what played.
        active.cleared = True
        self.controls.put_nowait(PlaybackClear(reason="output_blocked"))
        self._spawn(self._speech.cancel(active.handle), name="scheduler:guard-cancel")
        self._settle_active(Outcome.FAILED, Phase.SPEAKING, reason=SkipReason.OUTPUT_BLOCKED)
        if self._on_hit == "mute_all":
            # The configured escalation: one hit shuts the whole mouth until a
            # human releases it ([safety].on_hit, plan section 7.2).
            self.panic_mute()

    def _on_done(self, event: link.ReplyDone) -> None:
        if self._apply_model_skip(event.handle):
            return
        active = self._active
        if active is None or event.handle is not active.handle:
            return
        if event.text:
            active.text = event.text[:4000]
        if event.status is link.ReplyStatus.COMPLETED:
            if self._spoken_sink is not None and event.text:
                self._spoken_sink(event.text)
            if active.intent.injection.reply.write_history and event.text:
                # Out-of-band replies never enter the provider's own history,
                # so without this she cannot remember what she just said — the
                # root of every "same opening three welcomes in a row".
                # Spawned like every other send here (the event loop must not
                # stall), and a failure is one warning, not a dead loop.
                self._spawn(self._write_history(event.text), name="scheduler:write-history")
            self._settle_active(Outcome.SPOKEN, Phase.GENERATING)
            if self._cooldown_s > 0:
                self._floor.start_cooldown(self._cooldown_s)
        elif event.status is link.ReplyStatus.TIMED_OUT:
            self._settle_active(Outcome.TIMED_OUT, Phase.DISPATCHED)
        elif event.status is link.ReplyStatus.CANCELLED:
            # A cancelled done reaching a still-active reply usually means the
            # PROVIDER initiated it — barge-in, where done arrives before
            # speech_started. Under panic the reason must say so, and a clear
            # already sent for this reply is not sent again (A10).
            if not active.cleared:
                if not self._panicked and not self._floor.streamer_speaking:
                    # The speech_started for this barge-in is one frame behind;
                    # hold the floor for it or the requeue below redispatches
                    # into the gap and is cancelled right back (ledger #29).
                    # Not when it ALREADY arrived (hosted shapes send it first):
                    # the latch promises a future edge, and one armed after its
                    # edge has nothing left to clear it but the timeout.
                    self._floor.expect_speech_edge(_SPEECH_EDGE_GRACE_S)
                self.controls.put_nowait(
                    PlaybackClear(reason="panic_mute" if self._panicked else "barge_in")
                )
                active.cleared = True
            reason = SkipReason.PANIC_MUTE if self._panicked else None
            self._record_interruption(
                active.intent,
                active.handle,
                active.text,
                confirmed=active.host_interrupted or self._floor.streamer_speaking,
            )
            if active.intent.requeue_on_interrupt and not self._panicked:
                self._settle_active(Outcome.CANCELLED, Phase.SPEAKING, requeue=True)
            else:
                self._settle_active(Outcome.CANCELLED, Phase.SPEAKING, reason=reason)
        else:
            # A reply that died mid-generation (the link dropped, the server
            # failed it): L1 still holds whatever already streamed, so ask it
            # to stop the way every other death does — otherwise a dead
            # session's half sentence plays to the end.
            if not active.cleared:
                self.controls.put_nowait(PlaybackClear(reason="link_failed"))
                active.cleared = True
            if active.intent.requeue_on_interrupt and not self._panicked:
                self._settle_active(Outcome.FAILED, Phase.GENERATING, requeue=True)
            else:
                self._settle_active(Outcome.FAILED, Phase.GENERATING)

    def _barge_in(self) -> None:
        """The streamer opened their mouth while a reply is still booked.

        On s2s this rarely runs with an active reply: the provider sends
        done(cancelled) BEFORE speech_started, so _on_done has already cleared
        and requeued by the time we get here. It does run for a PROTECTED
        reply, whose dispatch disarmed the server's own axe — then this is the
        only code that cancels. This path also covers shapes that send started
        first: cancel now, and let the done settle the books. A reply inside
        its protection window is left alone; only panic outranks paid
        protection (section 2.7).

        The cancel is spawned like the other five in this file: a send that
        raises — the socket may be halfway through closing, which is why the
        client's watchdog suppresses the same one (client.py:294-295) — costs
        one task, not the event loop.
        """
        pending = tuple(self._pending_interruptions.values())
        self._pending_interruptions.clear()
        for intent, handle, text, deadline in pending:
            if self._clock.monotonic() <= deadline:
                self._record_interruption(intent, handle, text, confirmed=True)
        implicit = self._implicit
        if implicit is not None and not implicit.killed:
            implicit.host_interrupted = True
            self._record_interruption(None, implicit.handle, implicit.text, confirmed=True)
        playback = self._implicit_playback
        self._implicit_playback = None
        if playback is not None and self._floor.queued_audio:
            self._record_interruption(None, playback.handle, playback.text, confirmed=True)
        # A new speech edge is conclusive even if the audio-drained receipt
        # arrives after the streamer has stopped talking again.
        self._flush_played(played=False)
        active = self._active
        if active is None:
            return
        if self._protection_active(active):
            assert active.protected_until is not None  # _protection_active said so
            with bind(intent_id=active.intent.dedup_key or active.intent.source):
                # The other half of the barge-in question, and the one that
                # looks like a bug from the outside: the streamer talked and
                # she kept going. Section 2.7 says paid protection outranks
                # everything but panic, so this is the line that proves the
                # rule fired rather than the cancel getting lost.
                log.info(
                    "scheduler.barge_survived",
                    source=active.intent.source,
                    priority=int(active.intent.priority),
                    protected_for_ms=max(
                        0, int((active.protected_until - self._clock.monotonic()) * 1000)
                    ),
                )
            return
        self._note_barge(active, where="speech_started")
        active.host_interrupted = True
        self._record_interruption(active.intent, active.handle, active.text, confirmed=True)
        active.cleared = True
        self.controls.put_nowait(PlaybackClear(reason="barge_in"))
        self._spawn(self._speech.cancel(active.handle), name="scheduler:barge-cancel")

    def _record_interruption(
        self,
        intent: Intent | None,
        handle: link.ReplyHandle,
        text: str,
        *,
        confirmed: bool,
    ) -> None:
        """Only a genuine speech edge may offer non-requeued work for recovery."""
        state = self._interaction_state
        if self._on_interrupted is None or self._panicked or (state is not None and state.silenced):
            return
        if intent is not None:
            if intent.requeue_on_interrupt:
                return
            events = intent.events or ((intent.event,) if intent.event is not None else ())
            if state is not None and events and all(state.is_handled(event) for event in events):
                return
        if handle.handle_id in self._interrupted_handles:
            return
        if not confirmed:
            self._pending_interruptions[handle.handle_id] = (
                intent,
                handle,
                text,
                self._clock.monotonic() + _SPEECH_EDGE_GRACE_S,
            )
            while len(self._pending_interruptions) > _REPLY_INTENTS_CAP:
                self._pending_interruptions.popitem(last=False)
            return
        self._interrupted_handles[handle.handle_id] = None
        while len(self._interrupted_handles) > _REPLY_INTENTS_CAP:
            self._interrupted_handles.popitem(last=False)
        self._on_interrupted(intent, handle, text)

    def _note_barge(self, active: _Active, *, where: str) -> None:
        """Name the reply the streamer just talked over.

        Two callers because the barge-in has two catch points, and telling them
        apart matters when the ordering is under suspicion: `speech_started` is
        the event handler, `post_dispatch` is the recheck for a speech edge
        that landed while the sends were still in the air (A1). The verdict
        that follows says the same reply died; only this says who cut it off
        and how much of it the room had already heard.

        Args:
            active: The reply being cut off.
            where: Which catch point saw it — "speech_started" or "post_dispatch".
        """
        intent = active.intent
        with bind(intent_id=intent.dedup_key or intent.source):
            log.info(
                "scheduler.barged_in",
                source=intent.source,
                priority=int(intent.priority),
                where=where,
                requeue=intent.requeue_on_interrupt,
                spoken_ms=self._spoken_ms(active.started_at),
            )

    # ------------------------------------------------------------ protection

    def _protection_active(self, active: _Active) -> bool:
        return (
            active.protected_until is not None and self._clock.monotonic() < active.protected_until
        )

    async def _protection_cap(self, active: _Active) -> None:
        """The protect_ms hard cap: re-arm provider barge-in even when the
        reply outlives its window (the forgotten half of A4)."""
        assert active.protected_until is not None
        delay = max(0.0, active.protected_until - self._clock.monotonic())
        await self._clock.sleep(delay)
        self._end_protection(active, via="cap")

    def _end_protection(self, active: _Active, *, via: str) -> None:
        """Re-arm the provider's own barge-in, once, whichever half gets here first.

        Args:
            active: The protected reply whose window is closing.
            via: Which half closed it — "settle" (the reply ended) or "cap"
                (protect_ms ran out while it was still going). Forgetting
                either was audit finding A4, and the pair being named on the
                line is what makes 「保护段有没有正常收尾」 answerable without
                a debugger.
        """
        if active.protection_ended or active.protected_until is None:
            return
        active.protection_ended = True
        with bind(intent_id=active.intent.dedup_key or active.intent.source):
            log.info(
                "scheduler.protection_ended",
                source=active.intent.source,
                via=via,
                remaining_ms=max(0, int((active.protected_until - self._clock.monotonic()) * 1000)),
            )

        async def rearm() -> None:
            try:
                await self._speech.end_protection()
            except Exception as exc:
                log.warning("scheduler.end_protection_failed", error_text=str(exc)[:200])

        self._spawn(rearm(), name="scheduler:end-protection")

    # ------------------------------------------------------------ bookkeeping

    def _maybe_preempt(self, incoming: Intent) -> None:
        active = self._active
        if active is None or self._interaction_gate(incoming) is not None:
            return
        if active.output_started:
            # The room already hears this sentence: live events queue behind
            # it no matter how they rank. Only the streamer's barge-in and
            # panic — the hard paths — may still cut it.
            return
        if int(incoming.priority) <= int(active.intent.priority):
            return
        with bind(intent_id=active.intent.dedup_key or active.intent.source):
            # Bound to the VICTIM: this is the answer to 「我那条怎么说到一半没了」,
            # and the winner rides along as a field. The verdict right behind it
            # carries scheduler.preempted as its reason but cannot name who won.
            log.info(
                "scheduler.preempted",
                source=active.intent.source,
                priority=int(active.intent.priority),
                winner_source=incoming.source,
                winner_priority=int(incoming.priority),
                requeue=active.intent.requeue_on_interrupt,
                spoken_ms=self._spoken_ms(active.started_at),
            )
        # The victim may have audio queued at L1; claw it back like any death.
        active.cleared = True
        self.controls.put_nowait(PlaybackClear(reason="preempted"))
        self._spawn(self._speech.cancel(active.handle), name="scheduler:preempt-cancel")
        intent = active.intent
        if intent.requeue_on_interrupt:
            self._settle_active(
                Outcome.CANCELLED, Phase.SPEAKING, reason=SkipReason.PREEMPTED, requeue=True
            )
        else:
            self._settle_active(Outcome.CANCELLED, Phase.SPEAKING, reason=SkipReason.PREEMPTED)

    def _drain_queue(self, reason: SkipReason | None) -> None:
        """Empty the heap, giving every waiting intent a verdict.

        Silence here would leave intents that never end anywhere, which is the
        one thing section 4.12 forbids.
        """
        while self._heap:
            entry = heapq.heappop(self._heap)
            self._drop_queued(entry.intent, reason)
        self._sync_interaction_retention()

    def _requeue(
        self, intent: Intent, *, item_written: bool, deadline: float | None = None
    ) -> None:
        """Put one intent back at the end of its priority band.

        A fresh Intent rather than the original so nothing downstream can hold
        a stale reference; the dedup key must already be freed by the caller,
        or submit() eats this as a duplicate.

        `deadline` overrides expires_at — the dispatch-failure path uses it to
        bound its own retries (section 4.11). An interruption requeue passes
        nothing: being talked over is not a reason to start a countdown.

        `item_written` drops the history half. That line is already in the
        conversation and add_context_item dedups nothing (s2s.py:126-142), so
        rewriting it on every requeue turned one Super Chat the streamer talked
        over three times into four identical lines — the model then reads a
        viewer spamming the same paid message. It stays when the write itself
        is what failed. A reconnect needs no special case: LinkDown drains the
        queue, so nothing requeued here is ever dispatched into the empty
        history of a fresh session.
        """
        injection = intent.injection
        if item_written and injection.item_text is not None:
            injection = Injection(reply=injection.reply)
        self.submit(
            replace(
                intent,
                injection=injection,
                expires_at=intent.expires_at if deadline is None else deadline,
            )
        )
        self._wake.set()

    def _settle_active(
        self,
        outcome: Outcome,
        phase: Phase,
        *,
        reason: SkipReason | None = None,
        requeue: bool = False,
        detail: str = "",
    ) -> None:
        active = self._active
        if active is None:
            return
        self._active = None
        self._floor.on_reply_active(False)
        self._end_protection(active, via="settle")
        intent = active.intent
        self._free_key(intent)
        if outcome is Outcome.CANCELLED:
            self._spawn(
                self._write_delivery_status(intent, "回复被打断，未完整播完"),
                name="scheduler:delivery-status",
            )
        elif outcome is Outcome.SPOKEN:
            self._spawn(
                self._write_delivery_status(intent, "生成已完成，尚不能确认完整播完"),
                name="scheduler:delivery-status",
            )
        parking = not requeue and outcome is Outcome.SPOKEN and self._floor.queued_audio
        with bind(intent_id=intent.dedup_key or intent.source):
            # debug and deliberately overlapping scheduler.verdict: this is the
            # LINK side of the same moment — whether a PlaybackClear already
            # went out, whether protection was in play, and whether the reply
            # is leaving without a verdict for now. That last one is why the
            # line exists: a settle with no verdict behind it looks like a lost
            # intent until you can see it went to the playback park.
            log.debug(
                "scheduler.settled",
                source=intent.source,
                outcome=str(outcome),
                phase=str(phase),
                reason=str(reason) if reason else "",
                requeue=requeue,
                parked=parking,
                cleared=active.cleared,
                protected=active.protected_until is not None,
                spoken_ms=self._spoken_ms(active.started_at),
            )
        if requeue:
            # Paid messages must not vanish silently (section 4.2): back into
            # the queue they go, same priority, new place in line. The dedup
            # key was freed above, so the resubmission is not eaten (A15).
            # An active reply means dispatch got past both sends, so its
            # history line is already written — carrying it again would say
            # the same paid message arrived twice.
            self._requeue(intent, item_written=True)
            self._sync_interaction_retention()
            return
        if parking:
            # The model stopped producing tokens; the audience has not heard it
            # yet. Hold the verdict for the playback receipt so it can say
            # spoken@played — the 106-second playback backlog is what taught us
            # the two are different facts.
            self._park_for_playback(active)
            self._sync_interaction_retention()
            self._wake.set()
            return
        self._emit(
            Verdict(
                intent_id=intent.dedup_key or intent.source,
                source=intent.source,
                outcome=outcome,
                phase=phase,
                reason=reason,
                waited_s=self._waited_s(intent, active.started_at),
                detail=detail,
                spoken_ms=self._spoken_ms(active.started_at),
            )
        )
        self._sync_interaction_retention()
        self._wake.set()

    def _park_for_playback(self, active: _Active) -> None:
        self._parked.append(
            _Parked(
                intent=active.intent,
                handle=active.handle,
                text=active.text,
                started_at=active.started_at,
                deadline=self._clock.monotonic() + _PLAYBACK_RECEIPT_GRACE_S,
            )
        )

        async def deadline_wake() -> None:
            # The receipt arrives as notify(); nothing else would wake the loop
            # to notice that the grace ran out.
            await self._clock.sleep(_PLAYBACK_RECEIPT_GRACE_S)
            self._wake.set()

        self._spawn(deadline_wake(), name="scheduler:playback-grace")

    def _flush_played(self, *, played: bool | None = None) -> None:
        """Write the verdicts held for a playback receipt.

        `played` forces the answer for callers who already know it — panic and
        shutdown both take the audio with them. Left None it is read off the
        floor: audio gone and nobody talking over it means the audience heard
        the whole thing.
        """
        if played is False or not self._floor.queued_audio:
            self._implicit_playback = None
        if not self._parked:
            return
        now = self._clock.monotonic()
        drained = not self._floor.queued_audio
        # Talked over mid-playback: the audio is gone because someone cut it,
        # not because the room heard it out.
        heard = played if played is not None else (drained and not self._floor.streamer_speaking)
        still_waiting: list[_Parked] = []
        for parked in self._parked:
            if played is None and not drained and now < parked.deadline:
                still_waiting.append(parked)
                continue
            self._spawn(
                self._write_delivery_status(
                    parked.intent,
                    "已完整播完" if heard else "未确认完整播完，不代表观众已听到完整回答",
                ),
                name="scheduler:delivery-status",
            )
            if (
                not heard
                and self._floor.streamer_speaking
                and not self._panicked
                and parked.intent.event is not None
                and parked.intent.event.kind is EventKind.VIP_ENTER
                and parked.intent.requeue_on_interrupt
            ):
                self._requeue(parked.intent, item_written=True)
                continue
            if not heard and self._floor.streamer_speaking:
                self._record_interruption(parked.intent, parked.handle, parked.text, confirmed=True)
            self._emit(
                Verdict(
                    intent_id=parked.intent.dedup_key or parked.intent.source,
                    source=parked.intent.source,
                    outcome=Outcome.SPOKEN,
                    phase=Phase.PLAYED if heard else Phase.GENERATING,
                    waited_s=self._waited_s(parked.intent, parked.started_at),
                    spoken_ms=self._spoken_ms(parked.started_at),
                )
            )
        self._parked = still_waiting
        self._sync_interaction_retention()

    def _waited_s(self, intent: Intent, until: float) -> float:
        """How long this intent waited for its turn, 0 when nobody said when it
        arrived — created_at defaults to 0.0, and subtracting that from a
        monotonic clock would put "waited 6 days" on the panel."""
        if intent.created_at <= 0.0:
            return 0.0
        return max(0.0, until - intent.created_at)

    def _spoken_ms(self, started_at: float) -> int:
        if started_at <= 0.0:
            return 0
        return max(0, int((self._clock.monotonic() - started_at) * 1000))

    def _drop_queued(
        self,
        intent: Intent,
        reason: SkipReason | None,
        *,
        outcome: Outcome = Outcome.SKIPPED,
        phase: Phase = Phase.QUEUED,
    ) -> None:
        self._free_key(intent)
        self._emit(
            Verdict(
                intent_id=intent.dedup_key or intent.source,
                source=intent.source,
                outcome=outcome,
                phase=phase,
                reason=reason,
                waited_s=self._waited_s(intent, self._clock.monotonic()),
            )
        )

    def _free_key(self, intent: Intent) -> None:
        self._interaction_errors.discard(intent.dedup_key or intent.source)
        if intent.dedup_key:
            self._queued_keys.discard(intent.dedup_key)
            # A revoke that raced the active reply (the "let it finish" case)
            # must not outlive the settle: a stranded entry would silently
            # expire any future intent that reuses the key.
            self._revoked.discard(intent.dedup_key)

    def _expired(self, intent: Intent) -> bool:
        return intent.expires_at is not None and self._clock.monotonic() >= intent.expires_at

    def _spawn(self, coro: Coroutine[Any, Any, Any] | Awaitable[Any], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_report_failure)


# PlaybackClear reasons are short words L1 logs and the panel shows; the
# existing ones ("barge_in", "panic_mute", "output_blocked", "link_failed")
# are not the verdict vocabulary, so the kill path maps rather than reuses.
_CLEAR_REASONS: dict[SkipReason, str] = {
    SkipReason.OUTPUT_BLOCKED: "output_blocked",
    SkipReason.PANIC_MUTE: "panic_mute",
    SkipReason.VOICE_NOT_ADDRESSED: "voice_not_addressed",
}


def _clear_reason(reason: SkipReason) -> str:
    return _CLEAR_REASONS.get(reason, reason.value.partition(".")[2])


def _report_failure(task: asyncio.Task[Any]) -> None:
    """Say what a spawned task died of.

    Nobody awaits these, so without this the reason surfaces — if at all — as
    asyncio's "exception was never retrieved" whenever the task is collected,
    with no event name anyone can search the log for.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("scheduler.task_failed", task=task.get_name(), error_text=str(exc)[:200])


def _as_stream_guard(
    guard: StreamGuard | Callable[[str], bool] | None,
) -> StreamGuard | None:
    """Accept both the real OutputGuard and the plain bool callables tests use."""
    if guard is None:
        return None
    if hasattr(guard, "hit") and hasattr(guard, "reset"):
        return cast(StreamGuard, guard)
    fn = guard

    class _Wrapped:
        def reset(self) -> None:
            return None

        def hit(self, delta: str) -> str | None:
            return "blocked" if fn(delta) else None

    return _Wrapped()
