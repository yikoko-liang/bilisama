"""Terminal verdict for every attempt to speak.

"Why didn't the assistant say anything just now?" is the number one support
question for a live product. Guessing from logs does not scale, so every Intent
ends in exactly one (outcome, phase) pair that the control panel can display
directly.

Read them as a pair: `skipped@gated` means the speaking floor held it back,
`cancelled@speaking` means the streamer talked over it mid-sentence,
`expired@queued` means it waited too long and stopped being worth saying.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    SPOKEN = "spoken"
    SKIPPED = "skipped"  # dropped before dispatch
    CANCELLED = "cancelled"  # dispatched, then interrupted or preempted
    FAILED = "failed"  # provider or tool error
    EXPIRED = "expired"  # its turn never came
    TIMED_OUT = "timed_out"  # watchdog fired


class Phase(StrEnum):
    """Where the verdict happened. Only meaningful paired with an Outcome."""

    SELECTED = "selected"  # picked by ingest, not yet scheduled
    QUEUED = "queued"  # in the priority heap
    GATED = "gated"  # held by the speaking floor
    DISPATCHED = "dispatched"  # sent to the provider, awaiting first delta
    GENERATING = "generating"  # model is producing tokens
    SPEAKING = "speaking"  # audio is playing
    PLAYED = "played"  # audience heard all of it


class SkipReason(StrEnum):
    """Stable reason strings for skipped and expired intents.

    These surface in the control panel and get aggregated into stats, so treat
    them as an append-only vocabulary: add new ones, never rename old ones.
    """

    LOW_VALUE = "selection.low_value"
    DUPLICATE = "selection.duplicate"
    RATE_LIMITED = "selection.rate_limited"
    QUEUE_FULL = "selection.queue_full"
    SPEAK_DISABLED = "policy.speak_disabled"
    HOST_SPEAKING = "gate.host_speaking"
    TURN_PENDING = "gate.turn_pending"
    AUDIO_QUEUED = "gate.audio_queued"
    INJECTION_GATE = "gate.injection_window"
    COOLDOWN = "gate.cooldown"
    PREEMPTED = "scheduler.preempted"
    RESULT_EXPIRED = "background.result_expired"
    LINK_DOWN = "link.down"
    """Nothing could be sent: the speech link was down when this came up."""
    PANIC_MUTE = "policy.panic_mute"
    OUTPUT_BLOCKED = "safety.output_blocked"
    VOICE_NOT_ADDRESSED = "voice.not_addressed"
    MODEL_DECLINED = "event.model_declined"
    HOST_HANDLED = "event.host_handled"
    HOST_HANDLING = "gate.host_handling"
    INTERACTION_SILENCE = "gate.interaction_silence"
    """The provider's own microphone turn was cut: the streamer was not
    talking to her (the voice gate's ruling, director/voice_turn.py)."""
    REVOKED = "platform.revoked"  # the platform withdrew it, e.g. a deleted super chat
    EMPTY_REPLY = "model.empty_reply"  # completed with no text and no audio (a report-only turn)
    REPLAY_LIMIT = "scheduler.replay_limit"  # talked over on every replay it was allowed
    # The danmaku funnel's accounts (selector.py). LOW_VALUE and DUPLICATE
    # above serve the funnel too — one vocabulary, not a parallel one.
    UID_COOLDOWN = (
        "selection.uid_cooldown"  # legacy: the cooldown is gone, old records keep reading
    )
    LOST_WINDOW = "selection.lost_window"
    WINDOW_EMPTY = "selection.window_empty"
    BREAKER_OPEN = "selection.breaker_open"
    DELIVER_FAILED = "selection.deliver_failed"
    LOW_INFORMATION = "selection.low_information"  # "666"-grade text, rejected before scoring
    REPEATED_CONTENT = "selection.repeated_content"  # cross-viewer copy spam
    LOST_DEFERRED = "selection.lost_deferred"  # held during host speech, beaten on release


@dataclass(frozen=True, slots=True)
class Verdict:
    """How one Intent ended. The scheduler emits exactly one per Intent."""

    intent_id: str
    source: str
    outcome: Outcome
    phase: Phase
    reason: SkipReason | None = None
    detail: str = ""
    waited_s: float = 0.0
    spoken_ms: int = 0

    def __str__(self) -> str:
        base = f"{self.outcome}@{self.phase}"
        return f"{base}({self.reason})" if self.reason else base


class OutcomeWindow:
    """The last N verdicts, aggregated for the health snapshot.

    Plan §4.12 asks health for "the last N outcomes, aggregated", and the
    question that actually gets asked on the panel is 「这十分钟里被闸门挡了几条」.
    One verdict per line answers it only for someone willing to count, which
    during a stream is nobody.

    Deliberately not time-based: the panel polls, the counts have to be cheap and
    they have to mean the same thing whether the room is dead or flooding. A
    running total sits beside the window so "since when" does not get lost.
    """

    __slots__ = ("_recent", "_seen")

    def __init__(self, size: int = 50) -> None:
        self._recent: deque[Verdict] = deque(maxlen=size)
        self._seen = 0

    def note(self, verdict: Verdict) -> None:
        """Record one terminal verdict. Cheap enough for the scheduler's sink."""
        self._recent.append(verdict)
        self._seen += 1

    def status(self) -> dict[str, Any]:
        """The probe body. Every key is present even before the first verdict —
        a probe that returns an empty dict reads as broken rather than as idle."""
        by_outcome: Counter[str] = Counter(str(v.outcome) for v in self._recent)
        by_reason: Counter[str] = Counter(str(v.reason) for v in self._recent if v.reason)
        return {
            "window": self._recent.maxlen,
            "seen": self._seen,
            "recent": len(self._recent),
            "by_outcome": dict(by_outcome),
            "by_reason": dict(by_reason),
            "last": str(self._recent[-1]) if self._recent else "",
        }
