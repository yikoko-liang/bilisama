"""The Intent vocabulary: one shape for everything that wants to speak.

Plan section 4.11 defines these on purpose before any scheduler exists: seven
concurrent sources funnel into one type, and turning a level on adds data,
never a branch — the scheduler reads priority and trusted, not source names.

expires_at and requeue_on_interrupt are not scheduler conveniences. A danmaku
answered late is worse than unanswered, and a paid Super Chat thank-you that an
interruption silently swallowed is a revenue bug (section 4.2): the two fields
carry those product rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from bilisama.ingest.events import LiveEvent
from bilisama.realtime.link import ReplySpec

__all__ = ["Injection", "Intent", "Priority"]


class Priority(IntEnum):
    """Bigger pre-empts smaller. STREAMER never loses and is never an Intent —
    the streamer's own turn is the provider's implicit one; the value exists so
    comparisons have a ceiling."""

    STREAMER = 100
    SUPERCHAT = 80
    BIG_GIFT = 70
    GUARD_BUY = 65
    VIP_ENTER = 50
    BACKGROUND_RESULT = 40
    DANMAKU = 30
    PROACTIVE = 10


@dataclass(frozen=True, slots=True)
class Injection:
    """What reaches the model: an optional history item plus the reply intent.

    item_text is the in-band half (written into history), reply is the
    out-of-band half — the two-step from plan section 4.5. None means the
    reply stands alone.
    """

    reply: ReplySpec
    item_text: str | None = None


@dataclass(frozen=True, slots=True)
class Intent:
    """One candidate utterance, from any source, ready to be scheduled."""

    source: str
    priority: Priority
    injection: Injection
    trusted: bool = False
    event: LiveEvent | None = None
    dedup_key: str = ""
    created_at: float = 0.0
    expires_at: float | None = None
    requeue_on_interrupt: bool = False
