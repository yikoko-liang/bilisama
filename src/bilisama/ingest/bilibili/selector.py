"""The danmaku funnel: dedup → cooldown → score → one winner per window.

This is the layer that turns "fifty events a second" into "at most one
danmaku intent per window" (plan section 2.7's funnel). Paid kinds never
come here — the assembly routes SC / guard / VIP straight to intent_for —
but EVERY gift does, paid or free: the combo aggregator is what keeps a
50-hit combo from becoming fifty thank-yous, and a settled aggregate goes
out immediately rather than waiting for a window.

O(1) by construction: the window holds only the current best candidate.
Every offered danmaku ends in exactly one account — chosen, or a SkipReason
from the one shared vocabulary (obs/outcome.py) — so "why didn't it answer
that one" is a status query, not a log dig. Windows that close with no
survivor count under `selection.window_empty`.

Chattiness owns the window length and the score bar (derive.py); both are
snapshotted when a window opens, so a mid-window slider change applies to
the NEXT window rather than moving the goalposts under the current one.

The delivery breaker latches for the run: the deliver callback is pure
intent construction plus a queue push, so its failures are bugs, not
weather — recovery is a restart, and health shows the latch.
"""

from __future__ import annotations

from collections import OrderedDict, deque
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
from bilisama.obs.outcome import SkipReason

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
        self._window_rules: DerivedThresholds | None = None
        self._offered = 0
        self._delivered = 0
        self._skips: dict[str, int] = {}

    # ------------------------------------------------------------ intake

    def offer(self, event: LiveEvent) -> None:
        """Take one event from the emit path. Synchronous, never blocks."""
        now = self._clock.monotonic()
        self._offered += 1
        if self._breaker.is_open:
            self._skip(SkipReason.BREAKER_OPEN)
            return
        if self._ring.seen(event.dedup_key, now):
            self._skip(SkipReason.DUPLICATE)
            return
        if event.kind is EventKind.GIFT:
            self._combos.add(event, now)
            return
        if self._uid_cooldown.blocked(event.viewer.identity, now):
            self._skip(SkipReason.UID_COOLDOWN)
            return
        score = danmaku_score(event)
        if self._window_opened is None:
            # The window opens on danmaku activity, not on a fixed cadence —
            # an idle room produces no windows and no window_empty noise.
            # Rules are snapshotted here: one window, one bar, one length.
            self._window_opened = now
            self._window_rules = self._thresholds()
        rules = self._window_rules
        assert rules is not None  # set whenever a window is open
        if score < rules.score_threshold:
            self._skip(SkipReason.LOW_VALUE)
            return
        if self._best is None or score > self._best_score:
            if self._best is not None:
                self._skip(SkipReason.LOST_WINDOW)
            self._best = event
            self._best_score = score
        else:
            self._skip(SkipReason.LOST_WINDOW)

    # ------------------------------------------------------------ loop

    async def run(self, deliver: Deliver) -> None:
        """Settle combos and close windows. Cancel to stop."""
        while True:
            await self._clock.sleep(_TICK_S)
            if self._breaker.is_open:
                continue  # latched for the run; health shows it
            if self._combos.pending_count == 0 and self._window_opened is None:
                continue  # idle room: nothing to settle, nothing to close
            now = self._clock.monotonic()
            try:
                await self._advance(now, deliver)
            except Exception as exc:
                # The breaker counts caught failures; SupervisedSource would
                # only ever see this loop die, which is the other book.
                if self._breaker.record_failure(now, str(exc)[:200]):
                    log.error("selector.breaker_open", error_text=str(exc)[:200])
                else:
                    log.warning("selector.advance_failed", error_text=str(exc)[:200])

    async def _advance(self, now: float, deliver: Deliver) -> None:
        while (due := self._combos.peek_due(now)) is not None:
            combo_id, aggregate = due
            # Deliver BEFORE any state changes: a failure leaves the combo
            # pending for the next tick instead of silently discarding a paid
            # thank-you and arming its 600s suppression.
            await deliver(aggregate)
            self._combos.commit(combo_id, now)
            self._delivered += 1
            # A mass settle (raid pause) yields between deliveries so the
            # voice pipeline on the same loop never sees one long stretch.
            await self._clock.sleep(0)
        rules = self._window_rules
        if self._window_opened is None or rules is None:
            return
        if now - self._window_opened < rules.danmaku_window_s:
            return
        best, self._best = self._best, None
        self._best_score = 0.0
        self._window_opened = None
        self._window_rules = None
        if best is None:
            self._skip(SkipReason.WINDOW_EMPTY)
            return
        try:
            await deliver(best)
        except Exception:
            # The winner is gone either way — the window already closed — so
            # put the loss on the books before the breaker hears about it.
            self._skip(SkipReason.DELIVER_FAILED)
            raise
        # Armed by the reply, not the attempt (safety.PerUidCooldown).
        self._uid_cooldown.mark(best.viewer.identity, now)
        self._delivered += 1

    # ------------------------------------------------------------ accounting

    def _skip(self, reason: SkipReason) -> None:
        self._skips[reason.value] = self._skips.get(reason.value, 0) + 1

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
        # Bounded like PerUidCooldown: a marathon mega-room stream would
        # otherwise hold every identity it ever saw (~144k/hour at the
        # presence parse budget). Evicting the oldest costs at most a rare
        # double-count in the burst tally.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = 8192
        self._arrivals: deque[float] = deque()
        self._last_fired: float | None = None

    def note(self, identity: str, now: float) -> int | None:
        """Count one arrival; returns the burst size when a welcome is due."""
        if identity in self._seen:
            return None
        self._seen[identity] = None
        if len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
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
