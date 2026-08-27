"""SpeakingFloor: may the assistant open its mouth right now.

The scheduler decides who speaks; this decides whether now is a moment anyone
below STREAMER may. Five conditions, each with its own updater, ported almost
line for line from qwen-audio-agent's announcement-window.mjs (89 lines, none
spare) plus the two we add: the speculative quiet window and the chattiness
cooldown (plan section 4.3).

The floor never reads provider knobs. The quiet window arrives as a duration
from whoever knows the turn's real branch value — handing smart_turn field
names to L3 would leak the engine into the orchestration layer.

Logging here covers the UPDATERS only, and only where they flip something.
`blocking_reason` is deliberately silent: the dispatch loop polls it on every
link event, deltas included, so a line there would be the busiest in the
process and would repeat one unchanging fact. Which gate closed on a waiting
queue is the scheduler's line to write (scheduler.gate_blocked), because only
the scheduler knows there was anything waiting.
"""

from __future__ import annotations

from bilisama.clock import Clock
from bilisama.obs.logging import get_logger
from bilisama.obs.outcome import SkipReason

__all__ = ["SpeakingFloor"]

log = get_logger(__name__)


class SpeakingFloor:
    """Five gates in one boolean. STREAMER traffic never consults it."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.streamer_speaking = False
        self.turn_pending = False
        # The provider's own implicit (VAD-triggered) reply is generating or
        # speaking. Tracked separately from turn_pending, which only covers
        # replies the scheduler dispatched itself — the audit's rule-5 window
        # (A2) lived exactly in that difference.
        self.implicit_active = False
        self.queued_audio = False
        self._quiet_until = 0.0
        self._cooldown_until = 0.0
        self._speech_edge_until = 0.0

    # ------------------------------------------------------------ updaters

    def on_speech_started(self) -> None:
        # Logged on the edge only. A repeated start is not news, and this is
        # the top answer to 「为什么刚才没说话」 — it should be one line per
        # utterance, so it stays readable in a log full of them.
        if not self.streamer_speaking:
            log.info("floor.speech_started", awaited_edge=self._speech_edge_until > 0.0)
        self.streamer_speaking = True
        self._speech_edge_until = 0.0  # the promised edge arrived

    def on_speech_stopped(self, *, quiet_s: float) -> None:
        """The streamer stopped; injections stay unsafe for quiet_s more.

        The caller passes the CURRENT turn's real grace (complete 0.8s versus
        incomplete 2.0s plus margin) — taking the max of both branches would
        make every turn wait for the worst case (plan section 2.8).
        """
        # Not edge-guarded like the start, because the quiet window is re-armed
        # on every call and that window IS the fact people ask about — 「她为什
        # 么等了一秒才接话」. A stop with no matching start still moves the gate.
        log.info("floor.speech_stopped", quiet_ms=int(quiet_s * 1000))
        self.streamer_speaking = False
        self._speech_edge_until = 0.0  # an edge is an edge, whichever came
        self._quiet_until = self._clock.monotonic() + quiet_s

    def expect_speech_edge(self, grace_s: float) -> None:
        """A provider-initiated cancel promises a speech edge one frame behind
        (s2s sends done(cancelled) BEFORE speech_started). Hold the floor for
        it, or a requeued reply redispatches into the gap between the two
        frames and is instantly cancelled again — ledger #29's wasted
        generation. Bounded: a shape that never sends the edge must not wedge
        the gate."""
        # debug: a 300ms latch is below the resolution of anything a streamer
        # would ask about, but it is exactly what makes ledger #29 legible when
        # someone is chasing a redispatch that got cancelled instantly.
        log.debug("floor.speech_edge_expected", grace_ms=int(grace_s * 1000))
        self._speech_edge_until = self._clock.monotonic() + grace_s

    def on_reply_active(self, active: bool) -> None:
        self.turn_pending = active

    def on_implicit(self, active: bool) -> None:
        self.implicit_active = active

    def on_link_lost(self) -> None:
        """Drop every flag this link's own events were feeding.

        streamer_speaking is the dangerous one: only on_speech_stopped clears
        it, and a socket that dies mid-utterance means that event never
        arrives. The gate would then stay shut forever — reconnect succeeds,
        the queue fills, and nothing is ever said (reproduced against the fake
        server before this existed). The same reasoning covers the implicit
        hold and any queued audio: both describe a session that is gone.
        """
        # Which flags this actually forced open is the whole question after a
        # reconnect: 「重连之后她怎么又不说话了」 versus 「她说了，是这里放开的」.
        # Reported before the reset, or every line would read all-False.
        log.info(
            "floor.link_lost",
            was_speaking=self.streamer_speaking,
            was_pending=self.turn_pending,
            was_implicit=self.implicit_active,
            had_audio=self.queued_audio,
        )
        self.streamer_speaking = False
        self.implicit_active = False
        self.turn_pending = False
        self.queued_audio = False

    def on_playback(self, queued: bool) -> None:
        # Edge-guarded twice over: both producers already report on the edge
        # (ui/audio.py:299-314, PlaybackTally counting 0↔1; and dev_talk's
        # speaker poll, which fires only on `busy != last`), and this keeps a
        # future third one from turning a gate flip into a per-chunk line.
        if queued != self.queued_audio:
            log.debug("floor.playback_edge", queued=queued)
        self.queued_audio = queued

    def start_cooldown(self, seconds: float) -> None:
        """Chattiness throttle: after speaking, hold the floor a while so the
        assistant does not become a greeting machine."""
        log.info("floor.cooldown_started", cooldown_ms=int(seconds * 1000))
        self._cooldown_until = self._clock.monotonic() + seconds

    # ------------------------------------------------------------ the gate

    def is_blocked(self) -> bool:
        return self.blocking_reason() is not None

    def blocking_reason(self) -> SkipReason | None:
        """Which gate is holding the floor right now, or None when it is open.

        "She said nothing" and "she said nothing because you were still talking"
        are different support tickets, and section 4.12's `skipped@gated` is the
        machine-readable half of the second one. is_blocked() is this method's
        boolean so the name and the gate can never disagree.
        """
        now = self._clock.monotonic()
        if self.streamer_speaking:
            return SkipReason.HOST_SPEAKING
        if self.turn_pending or self.implicit_active:
            # One reason for both: from the queue's side a dispatched reply and
            # the provider's own implicit turn are the same fact — a turn is
            # already booked. The pair only differs to the rule-5 logic (A2).
            return SkipReason.TURN_PENDING
        if self.queued_audio:
            return SkipReason.AUDIO_QUEUED
        if now < self._quiet_until or now < self._speech_edge_until:
            # Both are the speculative window around a speech edge, which is
            # exactly what gate.injection_window names.
            return SkipReason.INJECTION_GATE
        if now < self._cooldown_until:
            return SkipReason.COOLDOWN
        return None

    def blocked_for(self) -> float:
        """Seconds until the time-based gates release, 0 when only state gates
        hold (those release on events, not on the clock)."""
        now = self._clock.monotonic()
        wait = max(
            self._quiet_until - now,
            self._cooldown_until - now,
            self._speech_edge_until - now,
            0.0,
        )
        return wait
