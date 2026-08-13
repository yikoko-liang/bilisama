"""SpeakingFloor: may the assistant open its mouth right now.

The scheduler decides who speaks; this decides whether now is a moment anyone
below STREAMER may. Five conditions, each with its own updater, ported almost
line for line from qwen-audio-agent's announcement-window.mjs (89 lines, none
spare) plus the two we add: the speculative quiet window and the chattiness
cooldown (plan section 4.3).

The floor never reads provider knobs. The quiet window arrives as a duration
from whoever knows the turn's real branch value — handing smart_turn field
names to L3 would leak the engine into the orchestration layer.
"""

from __future__ import annotations

from bilisama.clock import Clock

__all__ = ["SpeakingFloor"]


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
        self.streamer_speaking = True
        self._speech_edge_until = 0.0  # the promised edge arrived

    def on_speech_stopped(self, *, quiet_s: float) -> None:
        """The streamer stopped; injections stay unsafe for quiet_s more.

        The caller passes the CURRENT turn's real grace (complete 0.8s versus
        incomplete 2.0s plus margin) — taking the max of both branches would
        make every turn wait for the worst case (plan section 2.8).
        """
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
        self._speech_edge_until = self._clock.monotonic() + grace_s

    def on_reply_active(self, active: bool) -> None:
        self.turn_pending = active

    def on_implicit(self, active: bool) -> None:
        self.implicit_active = active

    def on_playback(self, queued: bool) -> None:
        self.queued_audio = queued

    def start_cooldown(self, seconds: float) -> None:
        """Chattiness throttle: after speaking, hold the floor a while so the
        assistant does not become a greeting machine."""
        self._cooldown_until = self._clock.monotonic() + seconds

    # ------------------------------------------------------------ the gate

    def is_blocked(self) -> bool:
        now = self._clock.monotonic()
        return (
            self.streamer_speaking
            or self.turn_pending
            or self.implicit_active
            or self.queued_audio
            or now < self._quiet_until
            or now < self._cooldown_until
            or now < self._speech_edge_until
        )

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
