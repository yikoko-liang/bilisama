"""The danmaku funnel: dedup → cooldown → score → one winner per window.

This is the layer that turns "fifty events a second" into "at most one
danmaku intent per window" (plan section 2.7's funnel). Paid kinds never
come here — the assembly routes SC / guard / VIP straight to intent_for —
but EVERY gift does, paid or free: the combo aggregator is what keeps a
50-hit combo from becoming fifty thank-yous, and a settled aggregate goes
out immediately rather than waiting for a window.

O(1) by construction: the window holds only the current best candidate.
Every offered danmaku ends in exactly one account — chosen, or a stable
skip reason (`selection.duplicate` / `selection.uid_cooldown` /
`selection.low_value_danmaku` / `selection.lost_window` /
`selection.breaker_open`) — so "why didn't it answer that one" is a status
query, not a log dig. Windows that close with no survivor count under
`selection.window_empty`.

Chattiness owns the window length and the score bar (derive.py); they are
read through a callable per window, so a future panel slider takes effect
on the next window with no rewiring.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bilisama.ingest.bilibili.safety import (
    CircuitBreaker,
    DedupRing,
    GiftComboAggregator,
    PerUidCooldown,
)
from bilisama.ingest.bilibili.scoring import danmaku_score
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.derive import DerivedThresholds

__all__ = ["SELECTOR_KINDS", "DanmakuSelector", "PresenceWelcomer"]

log = get_logger(__name__)

# What the assembly routes here. Everything else keeps the direct path.
SELECTOR_KINDS = frozenset({EventKind.DANMAKU, EventKind.GIFT})

_TICK_S = 0.25  # combo settle precision; the window check rides along

Deliver = Callable[[LiveEvent], Awaitable[None]]


class DanmakuSelector:
    """One winner per window, gifts aggregated, everything else accounted."""

    def __init__(
        self,
        clock: Clock,
        *,
        thresholds: Callable[[], DerivedThresholds],
        per_uid_cooldown_s: float = 60.0,
    ) -> None:
        self._clock = clock
        self._thresholds = thresholds
        self._ring = DedupRing()
        self._uid_cooldown = PerUidCooldown(per_uid_cooldown_s)
        self._combos = GiftComboAggregator()
        self._breaker = CircuitBreaker()
        self._best: LiveEvent | None = None
        self._best_score = 0.0
        self._window_opened: float | None = None
        self._offered = 0
        self._delivered = 0
        self._skips: dict[str, int] = {}

    # ------------------------------------------------------------ intake

    def offer(self, event: LiveEvent) -> None:
        """Take one event from the emit path. Synchronous, never blocks."""
        now = self._clock.monotonic()
        self._offered += 1
        if self._breaker.is_open:
            self._skip("selection.breaker_open")
            return
        if self._ring.seen(event.dedup_key, now):
            self._skip("selection.duplicate")
            return
        if event.kind is EventKind.GIFT:
            self._combos.add(event, now)
            return
        if self._uid_cooldown.blocked(event.viewer.identity, now):
            self._skip("selection.uid_cooldown")
            return
        score = danmaku_score(event)
        if self._window_opened is None:
            # The window opens on danmaku activity, not on a fixed cadence —
            # an idle room produces no windows and no window_empty noise.
            self._window_opened = now
        if score < self._thresholds().score_threshold:
            self._skip("selection.low_value_danmaku")
            return
        if self._best is None or score > self._best_score:
            if self._best is not None:
                self._skip("selection.lost_window")
            self._best = event
            self._best_score = score
        else:
            self._skip("selection.lost_window")

    # ------------------------------------------------------------ loop

    async def run(self, deliver: Deliver) -> None:
        """Settle combos and close windows. Cancel to stop."""
        while True:
            await self._clock.sleep(_TICK_S)
            now = self._clock.monotonic()
            try:
                await self._advance(now, deliver)
            except Exception as exc:
                # The breaker counts caught failures; SupervisedSource would
                # only ever see this loop die, which is the other book.
                self._breaker.record_failure(now, str(exc))
                log.warning("selector.advance_failed", error_text=str(exc)[:200])

    async def _advance(self, now: float, deliver: Deliver) -> None:
        if self._breaker.is_open:
            # The lane is stopped, not just the intake: pending state freezes
            # until reset_breaker(), so nothing half-settled leaks out.
            return
        for aggregate in self._combos.due(now):
            self._delivered += 1
            await deliver(aggregate)
        if self._window_opened is None or now - self._window_opened < (
            self._thresholds().danmaku_window_s
        ):
            return
        best, self._best = self._best, None
        self._best_score = 0.0
        self._window_opened = None
        if best is None:
            self._skip("selection.window_empty")
            return
        self._uid_cooldown.mark(best.viewer.identity, now)
        self._delivered += 1
        await deliver(best)

    # ------------------------------------------------------------ accounting

    def _skip(self, reason: str) -> None:
        self._skips[reason] = self._skips.get(reason, 0) + 1

    def reset_breaker(self) -> None:
        self._breaker.reset()

    def status(self) -> dict[str, object]:
        return {
            "offered": self._offered,
            "delivered": self._delivered,
            "skips": dict(self._skips),
            "window_open": self._window_opened is not None,
            "breaker_open": self._breaker.is_open,
            "breaker_reason": self._breaker.reason,
            "combos_suppressed": self._combos.suppressed_events,
        }


class PresenceWelcomer:
    """New-arrival burst detector: 5 first-time uids inside 45s buys ONE hello.

    Each identity counts once per stream, so a viewer bouncing in and out is
    not five people. The cooldown keeps an opening-minute crowd from turning
    the co-host into a greeting machine; when it ends, only arrivals still
    inside the window count — nobody gets welcomed for walking in a minute
    and a half ago.
    """

    def __init__(self, *, uniques: int = 5, window_s: float = 45.0, cooldown_s: float = 90.0):
        self._uniques = uniques
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._seen: set[str] = set()
        self._arrivals: deque[float] = deque()
        self._last_fired: float | None = None

    def note(self, identity: str, now: float) -> int | None:
        """Count one arrival; returns the burst size when a welcome is due."""
        if identity in self._seen:
            return None
        self._seen.add(identity)
        self._arrivals.append(now)
        while self._arrivals and now - self._arrivals[0] > self._window_s:
            self._arrivals.popleft()
        if self._last_fired is not None and now - self._last_fired < self._cooldown_s:
            return None
        if len(self._arrivals) < self._uniques:
            return None
        count = len(self._arrivals)
        self._arrivals.clear()
        self._last_fired = now
        return count
