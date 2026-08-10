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
  answer to "why did it not speak just now". Dispatch failures included (A8).
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
from bilisama.director.intent import Intent
from bilisama.obs.logging import get_logger
from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict
from bilisama.realtime import link

__all__ = ["PlaybackClear", "Scheduler"]

log = get_logger(__name__)


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
    # Inside this window only panic may kill the reply (paid protection).
    protected_until: float | None = None
    protection_ended: bool = False
    # A PlaybackClear already went out for this reply; the late done(cancelled)
    # must not send a second one under a made-up reason (A10).
    cleared: bool = False


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
        self._active: _Active | None = None
        self._dispatching = False
        self._panicked = False
        self._wake = asyncio.Event()
        self.controls: asyncio.Queue[PlaybackClear] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ intake

    def submit(self, intent: Intent) -> None:
        """Queue an intent. Duplicates (same dedup_key while queued or active)
        are skipped with a verdict rather than silently dropped."""
        if self._panicked:
            self._verdict_sink(
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
            self._verdict_sink(
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

    def panic_mute(self) -> None:
        """Kill everything, protected included — the one switch allowed to."""
        self._panicked = True
        self.controls.put_nowait(PlaybackClear(reason="panic_mute"))
        while self._heap:
            entry = heapq.heappop(self._heap)
            self._drop_queued(entry.intent, SkipReason.PANIC_MUTE)
        active = self._active
        if active is not None:
            active.cleared = True
            self._spawn(self._speech.cancel(active.handle), name="scheduler:panic-cancel")
        # An in-flight dispatch (self._dispatching) has no handle to cancel
        # yet; the post-dispatch recheck honours the flag the moment it lands.
        self._wake.set()

    def release_panic(self) -> None:
        self._panicked = False
        self._wake.set()

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
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    async def _event_loop(self) -> None:
        async for event in self._speech.events():
            if isinstance(event, link.SpeechStarted):
                self._floor.on_speech_started()
                await self._barge_in()
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
            elif isinstance(event, link.LinkError) and event.code == "connection_lost":
                # The transport already settled every record (FAILED dones are
                # on their way); release the implicit hold so the floor cannot
                # stay shut forever on a link that no longer exists.
                self._floor.on_implicit(False)
            self._wake.set()

    async def _dispatch_loop(self) -> None:
        while True:
            self._wake.clear()
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
        while self._heap:
            entry = self._heap[0]
            intent = entry.intent
            if self._expired(intent):
                heapq.heappop(self._heap)
                self._drop_queued(intent, SkipReason.RESULT_EXPIRED, outcome=Outcome.EXPIRED)
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
        try:
            if intent.injection.item_text is not None:
                await self._speech.add_context_item(intent.injection.item_text)
            handle = await self._speech.request_reply(intent.injection.reply)
        except Exception as exc:
            log.warning(
                "scheduler.dispatch_failed", source=intent.source, error_text=str(exc)[:200]
            )
            self._free_key(intent)
            self._verdict_sink(
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

        active = _Active(intent=intent, handle=handle)
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
            self._settle_active(Outcome.FAILED, Phase.GENERATING)

    async def _barge_in(self) -> None:
        """The streamer opened their mouth while a reply is still booked.

        On s2s this rarely runs with an active reply: the provider sends
        done(cancelled) BEFORE speech_started, so _on_done has already cleared
        and requeued by the time we get here. This path covers shapes that send
        started first — cancel now, and let the done settle the books. A reply
        inside its protection window is left alone: only panic outranks paid
        protection (section 2.7).
        """
        active = self._active
        if active is None:
            return
        if self._protection_active(active):
            return
        active.cleared = True
        self.controls.put_nowait(PlaybackClear(reason="barge_in"))
        await self._speech.cancel(active.handle)

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
            requeued = Intent(
                source=intent.source,
                priority=intent.priority,
                injection=intent.injection,
                trusted=intent.trusted,
                event=intent.event,
                dedup_key=intent.dedup_key,
                created_at=intent.created_at,
                expires_at=intent.expires_at,
                requeue_on_interrupt=intent.requeue_on_interrupt,
            )
            self.submit(requeued)
            self._wake.set()
            return
        self._verdict_sink(
            Verdict(
                intent_id=intent.dedup_key or intent.source,
                source=intent.source,
                outcome=outcome,
                phase=phase,
                reason=reason,
            )
        )
        self._wake.set()

    def _drop_queued(
        self,
        intent: Intent,
        reason: SkipReason,
        *,
        outcome: Outcome = Outcome.SKIPPED,
    ) -> None:
        self._free_key(intent)
        self._verdict_sink(
            Verdict(
                intent_id=intent.dedup_key or intent.source,
                source=intent.source,
                outcome=outcome,
                phase=Phase.QUEUED,
                reason=reason,
            )
        )

    def _free_key(self, intent: Intent) -> None:
        if intent.dedup_key:
            self._queued_keys.discard(intent.dedup_key)

    def _expired(self, intent: Intent) -> bool:
        return intent.expires_at is not None and self._clock.monotonic() >= intent.expires_at

    def _spawn(self, coro: Coroutine[Any, Any, Any] | Awaitable[Any], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


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
