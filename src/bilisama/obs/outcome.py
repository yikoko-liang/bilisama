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

from dataclasses import dataclass
from enum import StrEnum


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
    PANIC_MUTE = "policy.panic_mute"
    OUTPUT_BLOCKED = "safety.output_blocked"
    REVOKED = "platform.revoked"  # the platform withdrew it, e.g. a deleted super chat


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
