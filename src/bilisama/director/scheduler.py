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
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from bilisama.clock import Clock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent
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


@dataclass(slots=True)
class _Parked:
    """A reply that finished generating while L1 still held its audio.

    Its verdict is written when the playback drains (spoken@played) or when the
    grace runs out (spoken@generating) — see _flush_played.
    """

    intent: Intent
    started_at: float
    deadline: float


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
    ) -> None:
        self._speech = speech
        self._floor = floor
        self._clock = clock
        self._verdicts: list[Verdict] = []
        self._verdict_sink = verdict_sink or self._verdicts.append
        self._quiet_after_speech_s = quiet_after_speech_s
        self._cooldown_s = cooldown_s
        self._guard = _as_stream_guard(guard)
        self._on_hit = on_hit
        # Receives every cleanly completed reply text — the distiller collects
        # them as voice-exemplar raw material (section 4.6). Interrupted or
        # guard-killed replies never reach it, which IS the quality filter.
        self._spoken_sink = spoken_sink
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
        self._wake = asyncio.Event()
        self.controls: asyncio.Queue[PlaybackClear] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ intake

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
        heapq.heappush(self._heap, _Entry((-int(intent.priority), next(self._seq)), intent))
        if intent.dedup_key:
            self._queued_keys.add(intent.dedup_key)
        self._maybe_preempt(intent)
        self._wake.set()

    def revoke(self, dedup_key: str) -> None:
        """Withdraw a queued intent — the super-chat-delete path.

        Only queued work is pulled: an answer already being spoken finishes,
        because cutting a thank-you mid-sentence sounds worse on stream than
        thanking a withdrawn SC.
        """
        if dedup_key in self._queued_keys:
            self._revoked.add(dedup_key)
            self._wake.set()

    def panic_mute(self) -> None:
        """Kill everything, protected included — the one switch allowed to."""
        self._panicked = True
        self.controls.put_nowait(PlaybackClear(reason="panic_mute"))
        # The clear above takes the parked replies' audio with it, so no
        # receipt is coming: write what we know rather than wait for it.
        self._flush_played(played=False)
        self._drain_queue(SkipReason.PANIC_MUTE)
        active = self._active
        if active is not None:
            active.cleared = True
            self._spawn(self._speech.cancel(active.handle), name="scheduler:panic-cancel")
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
        self._panicked = False
        self._wake.set()

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
        elif isinstance(event, link.ReplyTextDelta):
            self._on_delta(event)
        elif isinstance(event, link.ReplyDone):
            if self._active is None or event.handle is not self._active.handle:
                self._floor.on_implicit(False)
            self._on_done(event)
        elif isinstance(event, link.LinkDown):
            # The transport already settled every record (FAILED dones are
            # on their way). Reset every flag this link was feeding, or the
            # floor stays shut forever, and drain the queue: holding work
            # for a link that may be gone for good just produces stale
            # replies later.
            self._link_down = True
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
        while self._heap:
            entry = self._heap[0]
            intent = entry.intent
            if intent.dedup_key and intent.dedup_key in self._revoked:
                heapq.heappop(self._heap)
                self._revoked.discard(intent.dedup_key)
                self._drop_queued(intent, SkipReason.REVOKED, outcome=Outcome.EXPIRED)
                continue
            if self._expired(intent):
                heapq.heappop(self._heap)
                # Name the gate that ate the runway. Every expiry used to read
                # background.result_expired — a lane that does not exist yet —
                # so the panel answered "the background result went stale" for
                # a danmaku that simply waited out the streamer (ledger #35).
                gate = self._floor.blocking_reason()
                self._drop_queued(
                    intent,
                    gate,
                    outcome=Outcome.EXPIRED,
                    phase=Phase.GATED if gate is not None else Phase.QUEUED,
                )
                continue
            if self._floor.is_blocked():
                return None
            heapq.heappop(self._heap)
            return intent
        return None

    async def _dispatch(self, intent: Intent) -> None:
        """Send one intent to the provider, honouring everything that fired
        while the sends were in flight (A1) and turning a failed send into a
        verdict instead of a dead scheduler (A8)."""
        self._dispatching = True
        wrote_item = False
        try:
            if intent.injection.item_text is not None:
                await self._speech.add_context_item(intent.injection.item_text)
                wrote_item = True
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
        self._floor.on_reply_active(True)
        if self._guard is not None:
            self._guard.reset()

        # ---- post-dispatch rechecks: what fired during the await window ----
        if self._panicked:
            active.cleared = True
            self._spawn(self._speech.cancel(handle), name="scheduler:panic-cancel")
            self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED, reason=SkipReason.PANIC_MUTE)
            return
        if self._floor.streamer_speaking and not self._protection_active(active):
            active.cleared = True
            self.controls.put_nowait(PlaybackClear(reason="barge_in"))
            self._spawn(self._speech.cancel(handle), name="scheduler:barge-cancel")
            if intent.requeue_on_interrupt:
                self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED, requeue=True)
            else:
                self._settle_active(Outcome.CANCELLED, Phase.DISPATCHED)
            return
        if self._heap:
            top = self._heap[0].intent
            if int(top.priority) > int(intent.priority):
                self._maybe_preempt(top)

    # ------------------------------------------------------------ events

    def _on_delta(self, event: link.ReplyTextDelta) -> None:
        active = self._active
        if active is None or event.handle is not active.handle:
            return
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
        active = self._active
        if active is None or event.handle is not active.handle:
            return
        if event.status is link.ReplyStatus.COMPLETED:
            if self._spoken_sink is not None and event.text:
                self._spoken_sink(event.text)
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
        active = self._active
        if active is None:
            return
        if self._protection_active(active):
            return
        active.cleared = True
        self.controls.put_nowait(PlaybackClear(reason="barge_in"))
        self._spawn(self._speech.cancel(active.handle), name="scheduler:barge-cancel")

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
        self._end_protection(active)

    def _end_protection(self, active: _Active) -> None:
        if active.protection_ended or active.protected_until is None:
            return
        active.protection_ended = True

        async def rearm() -> None:
            try:
                await self._speech.end_protection()
            except Exception as exc:
                log.warning("scheduler.end_protection_failed", error_text=str(exc)[:200])

        self._spawn(rearm(), name="scheduler:end-protection")

    # ------------------------------------------------------------ bookkeeping

    def _maybe_preempt(self, incoming: Intent) -> None:
        active = self._active
        if active is None:
            return
        if int(incoming.priority) <= int(active.intent.priority):
            return
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
            Intent(
                source=intent.source,
                priority=intent.priority,
                injection=injection,
                trusted=intent.trusted,
                event=intent.event,
                dedup_key=intent.dedup_key,
                created_at=intent.created_at,
                expires_at=intent.expires_at if deadline is None else deadline,
                requeue_on_interrupt=intent.requeue_on_interrupt,
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
    ) -> None:
        active = self._active
        if active is None:
            return
        self._active = None
        self._floor.on_reply_active(False)
        self._end_protection(active)
        intent = active.intent
        self._free_key(intent)
        if requeue:
            # Paid messages must not vanish silently (section 4.2): back into
            # the queue they go, same priority, new place in line. The dedup
            # key was freed above, so the resubmission is not eaten (A15).
            # An active reply means dispatch got past both sends, so its
            # history line is already written — carrying it again would say
            # the same paid message arrived twice.
            self._requeue(intent, item_written=True)
            return
        if outcome is Outcome.SPOKEN and self._floor.queued_audio:
            # The model stopped producing tokens; the audience has not heard it
            # yet. Hold the verdict for the playback receipt so it can say
            # spoken@played — the 106-second playback backlog is what taught us
            # the two are different facts.
            self._park_for_playback(active)
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
                spoken_ms=self._spoken_ms(active.started_at),
            )
        )
        self._wake.set()

    def _park_for_playback(self, active: _Active) -> None:
        self._parked.append(
            _Parked(
                intent=active.intent,
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
