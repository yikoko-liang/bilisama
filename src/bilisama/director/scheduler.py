"""The scheduler: one reply slot, seven claimants, no exceptions.

Every provider turned out single-slot (capabilities.py, all three verified),
so this module is load-bearing for the whole product: without it, concurrent
sources race the slot and the audience watches a paid Super Chat go
unanswered. The reference repos have nothing like it — qwen-audio-agent faces
one человек and a low-rate notifier; we face a gift storm (plan section 4.2).

What it guarantees, and where each promise is tested:

- At most one reply in flight, ever. The funnel (section 2.7) means the queue
  sees tens per minute, not per second; the heap orders by priority then age.
- Pre-emption: a strictly higher priority cancels the active reply. The victim
  requeues when its intent asks for that (paid events do), otherwise its
  verdict says preempted.
- Barge-in: the streamer speaking cancels any active reply, immediately asks
  L1 to stop playback (the clear goes out before anything else, section 2.5
  sequence 3), and requeues protected work.
- panic mute: the red button. Kills the active reply even when protected —
  the only thing allowed to — drains the queue with verdicts, and refuses new
  dispatch until released.
- Every intent ends in exactly one Verdict (section 4.12): the machine-readable
  answer to "why did it not speak just now".
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

from bilisama.clock import Clock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict
from bilisama.realtime import link

__all__ = ["PlaybackClear", "Scheduler"]


@dataclass(frozen=True, slots=True)
class PlaybackClear:
    """Ask L1 to stop everything queued and roll the avatar back. Emitted the
    moment an active reply dies; the Electron side (stage 6) consumes it."""

    reason: str


@dataclass(slots=True)
class _Active:
    intent: Intent
    handle: link.ReplyHandle
    got_text: bool = False


@dataclass(order=True)
class _Entry:
    sort_key: tuple[int, int]
    intent: Intent = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


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
        guard: Callable[[str], bool] | None = None,
    ) -> None:
        self._speech = speech
        self._floor = floor
        self._clock = clock
        self._verdicts: list[Verdict] = []
        self._verdict_sink = verdict_sink or self._verdicts.append
        self._quiet_after_speech_s = quiet_after_speech_s
        self._cooldown_s = cooldown_s
        # Returns True when the text must not go out (output guard, section 4.5).
        self._guard = guard
        self._heap: list[_Entry] = []
        self._entries: dict[int, _Entry] = {}  # id(intent) -> entry, for pre-emption
        self._seq = itertools.count()
        self._queued_keys: set[str] = set()
        self._active: _Active | None = None
        self._panicked = False
        self._wake = asyncio.Event()
        self.controls: asyncio.Queue[PlaybackClear] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ intake

    def submit(self, intent: Intent) -> None:
        """Queue an intent. Duplicates (same dedup_key while queued) are skipped
        with a verdict rather than silently dropped."""
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
        entry = _Entry(sort_key=(-int(intent.priority), next(self._seq)), intent=intent)
        heapq.heappush(self._heap, entry)
        self._entries[id(intent)] = entry
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
            if entry.cancelled:
                continue
            self._drop_queued(entry.intent, SkipReason.PANIC_MUTE)
        active = self._active
        if active is not None:
            task = asyncio.create_task(self._speech.cancel(active.handle))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self._wake.set()

    def release_panic(self) -> None:
        self._panicked = False
        self._wake.set()

    @property
    def verdicts(self) -> list[Verdict]:
        """Verdicts collected by the default sink (tests read these)."""
        return self._verdicts

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
            elif isinstance(event, link.ReplyTextDelta):
                self._on_delta(event)
            elif isinstance(event, link.ReplyDone):
                self._on_done(event)
            self._wake.set()

    async def _dispatch_loop(self) -> None:
        while True:
            self._wake.clear()
            entry = self._next_dispatchable()
            if entry is None:
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
            await self._dispatch(entry.intent)

    def _next_dispatchable(self) -> _Entry | None:
        if self._panicked or self._active is not None:
            return None
        while self._heap:
            entry = self._heap[0]
            if entry.cancelled:
                heapq.heappop(self._heap)
                continue
            intent = entry.intent
            if self._expired(intent):
                heapq.heappop(self._heap)
                self._drop_queued(intent, SkipReason.RESULT_EXPIRED, outcome=Outcome.EXPIRED)
                continue
            if self._floor.is_blocked():
                return None
            heapq.heappop(self._heap)
            self._forget(intent)
            return entry
        return None

    async def _dispatch(self, intent: Intent) -> None:
        if intent.injection.item_text is not None:
            await self._speech.add_context_item(intent.injection.item_text)
        handle = await self._speech.request_reply(intent.injection.reply)
        self._active = _Active(intent=intent, handle=handle)
        self._floor.on_reply_active(True)

    # ------------------------------------------------------------ events

    def _on_delta(self, event: link.ReplyTextDelta) -> None:
        active = self._active
        if active is None or event.handle is not active.handle:
            return
        active.got_text = True
        if self._guard is not None and self._guard(event.text):
            # A hit mid-stream: kill the sentence and claw back what played.
            self.controls.put_nowait(PlaybackClear(reason="output_blocked"))
            task = asyncio.create_task(self._speech.cancel(active.handle))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._settle_active(Outcome.FAILED, Phase.SPEAKING, reason=SkipReason.OUTPUT_BLOCKED)

    def _on_done(self, event: link.ReplyDone) -> None:
        active = self._active
        if active is None or event.handle is not active.handle:
            return
        if event.status is link.ReplyStatus.COMPLETED:
            self._settle_active(Outcome.SPOKEN, Phase.GENERATING)
            if self._cooldown_s > 0:
                self._floor.start_cooldown(self._cooldown_s)
        elif event.status is link.ReplyStatus.TIMED_OUT:
            self._settle_active(Outcome.TIMED_OUT, Phase.DISPATCHED)
        elif event.status is link.ReplyStatus.CANCELLED:
            # A cancelled done reaching a still-active reply means the PROVIDER
            # initiated it — barge-in, where done arrives before speech_started
            # (the backwards order upstream really uses). Pre-emption, panic and
            # the guard all settle the active before their done lands, so they
            # never reach this branch. Clear playback now — with done-first
            # ordering this IS the earliest moment — and requeue paid work.
            self.controls.put_nowait(PlaybackClear(reason="barge_in"))
            if active.intent.requeue_on_interrupt and not self._panicked:
                self._settle_active(Outcome.CANCELLED, Phase.SPEAKING, requeue=True)
            else:
                self._settle_active(Outcome.CANCELLED, Phase.SPEAKING)
        else:
            self._settle_active(Outcome.FAILED, Phase.GENERATING)

    async def _barge_in(self) -> None:
        """The streamer opened their mouth while a reply is still booked.

        On s2s this rarely runs with an active reply: the provider sends
        done(cancelled) BEFORE speech_started, so _on_done has already cleared
        and requeued by the time we get here. This path covers shapes that send
        started first — cancel now, and let the done settle the books.
        """
        active = self._active
        if active is None:
            return
        self.controls.put_nowait(PlaybackClear(reason="barge_in"))
        await self._speech.cancel(active.handle)

    # ------------------------------------------------------------ bookkeeping

    def _maybe_preempt(self, incoming: Intent) -> None:
        active = self._active
        if active is None:
            return
        if int(incoming.priority) <= int(active.intent.priority):
            return
        # The victim may have audio queued at L1; claw it back like any death.
        self.controls.put_nowait(PlaybackClear(reason="preempted"))
        task = asyncio.create_task(self._speech.cancel(active.handle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
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
        intent = active.intent
        if requeue:
            # Paid messages must not vanish silently (section 4.2): back into
            # the queue they go, same priority, new place in line.
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
        self._forget(intent)
        phase = Phase.QUEUED
        self._verdict_sink(
            Verdict(
                intent_id=intent.dedup_key or intent.source,
                source=intent.source,
                outcome=outcome,
                phase=phase,
                reason=reason,
            )
        )

    def _forget(self, intent: Intent) -> None:
        self._entries.pop(id(intent), None)
        if intent.dedup_key:
            self._queued_keys.discard(intent.dedup_key)

    def _expired(self, intent: Intent) -> bool:
        return intent.expires_at is not None and self._clock.monotonic() >= intent.expires_at
