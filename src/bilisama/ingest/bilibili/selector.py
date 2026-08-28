"""The danmaku funnel: dedup → text signal → score → one winner per window.

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

That account is kept twice over: a per-reason tally in status(), and, when a
sink is wired, one `on_skip` call per dropped event carrying the event itself.
The tally alone could only ever say how many were dropped for a reason, never
which ones — and in a live room most danmaku end here rather than at the
scheduler, where the Intent-level verdicts live (ledger item 49).

The window length and the score bar come from the thresholds callable —
EventPacer folds chattiness together with measured room load into both. They
are snapshotted when a window opens, so a mid-window change applies to the
NEXT window rather than moving the goalposts under the current one.

Two rules replaced the old 60s per-viewer cooldown (see safety.py's note):
text decides, not the sender — obvious spam is rejected by content
(scoring.danmaku_text_signal) and cross-viewer copy spam by similarity — so
an answered viewer's follow-up question competes again immediately.

While the streamer is speaking or the ordinary budget is empty
(delivery_blocked), window winners are DEFERRED rather than queued: no
intent exists yet, so no TTL is burning. The buffer is bounded and releases
only its best member when delivery unblocks — a long monologue produces one
good reply, not a backlog.

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
)
from bilisama.ingest.bilibili.scoring import (
    TextSignal,
    danmaku_content_key,
    danmaku_score,
    danmaku_text_signal,
    is_near_duplicate,
)
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.obs.logging import get_logger
from bilisama.obs.outcome import SkipReason

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.derive import DerivedThresholds
    from bilisama.event_pacing import EventPacingSnapshot

__all__ = ["SELECTOR_KINDS", "DanmakuSelector", "EntryCoalescer", "PresenceWelcomer", "SkipSink"]

log = get_logger(__name__)

# What the assembly routes here. Everything else keeps the direct path.
SELECTOR_KINDS = frozenset({EventKind.DANMAKU, EventKind.GIFT})

_TICK_S = 0.25  # combo settle precision; the window check rides along
_CONTENT_REPEAT_WINDOW_S = 120.0
_DEFERRED_CAPACITY = 5

Deliver = Callable[[LiveEvent], Awaitable[None]]

# One call per dropped event, carrying the event itself. The Intent-level
# verdicts the panel already shows only cover what REACHED the scheduler, and
# in a live room most danmaku end here instead — a per-reason tally could say
# "12 条低分" but never which twelve, which is not an answer to "为什么没回我"
# (ledger item 49). The event is None for WINDOW_EMPTY, the one account that
# belongs to a window rather than to any single danmaku.
SkipSink = Callable[[LiveEvent | None, SkipReason], None]


class DanmakuSelector:
    """One winner per window, gifts aggregated, everything else accounted."""

    def __init__(
        self,
        clock: Clock,
        *,
        thresholds: Callable[[], DerivedThresholds],
        context_lines: Callable[[], tuple[str, ...]] | None = None,
        mention_terms: Callable[[], tuple[str, ...]] | None = None,
        delivery_blocked: Callable[[], bool] | None = None,
        on_skip: SkipSink | None = None,
    ) -> None:
        self._clock = clock
        self._thresholds = thresholds
        self._on_skip = on_skip
        self._ring = DedupRing()
        self._context_lines = context_lines or (lambda: ())
        self._mention_terms = mention_terms or (lambda: ())
        self._delivery_blocked = delivery_blocked or (lambda: False)
        self._recent_content: deque[tuple[float, str]] = deque(maxlen=64)
        self._combos = GiftComboAggregator()
        self._breaker = CircuitBreaker()
        self._best: LiveEvent | None = None
        self._best_score = 0.0
        self._window_opened: float | None = None
        self._window_rules: DerivedThresholds | None = None
        # Each completed window may contribute one candidate while the host is
        # speaking or the ordinary-event budget is empty. The buffer is bounded
        # and releases only its best member once delivery becomes possible, so a
        # long monologue does not turn into a reply backlog.
        self._deferred: list[tuple[float, int, LiveEvent]] = []
        self._deferred_seq = 0
        self._offered = 0
        self._delivered = 0
        self._skips: dict[str, int] = {}
        self._passes: dict[str, int] = {}

    # ------------------------------------------------------------ intake

    def offer(self, event: LiveEvent) -> None:
        """Take one event from the emit path. Synchronous, never blocks."""
        now = self._clock.monotonic()
        self._offered += 1
        if self._breaker.is_open:
            self._skip(SkipReason.BREAKER_OPEN, event)
            return
        if self._ring.seen(event.dedup_key, now):
            self._skip(SkipReason.DUPLICATE, event)
            return
        if event.kind is EventKind.GIFT:
            self._combos.add(event, now)
            return
        signal = danmaku_text_signal(
            event.text,
            context_lines=self._context_lines(),
            mention_terms=self._mention_terms(),
        )
        if signal is TextSignal.REJECT:
            self._skip(SkipReason.LOW_INFORMATION, event)
            return
        while self._recent_content and now - self._recent_content[0][0] > _CONTENT_REPEAT_WINDOW_S:
            self._recent_content.popleft()
        recent = tuple(key for _stamp, key in self._recent_content)
        if is_near_duplicate(event.text, recent):
            self._skip(SkipReason.REPEATED_CONTENT, event)
            return
        self._recent_content.append((now, danmaku_content_key(event.text)))
        score = danmaku_score(event)
        if self._window_opened is None:
            # The window opens on danmaku activity, not on a fixed cadence —
            # an idle room produces no windows and no window_empty noise.
            # Rules are snapshotted here: one window, one bar, one length.
            self._window_opened = now
            self._window_rules = self._thresholds()
            # The bar is snapshotted here, so this is the one line that can say
            # what the pacing policy was worth for THIS window.
            log.debug(
                "selector.window_opened",
                window_s=self._window_rules.danmaku_window_s,
                score_bar=self._window_rules.score_threshold,
            )
        rules = self._window_rules
        assert rules is not None  # set whenever a window is open
        if signal is not TextSignal.HARD_ACCEPT and score < rules.score_threshold:
            self._skip(SkipReason.LOW_VALUE, event)
            return
        pass_kind = "rule" if signal is TextSignal.HARD_ACCEPT else "score"
        self._passes[pass_kind] = self._passes.get(pass_kind, 0) + 1
        if signal is TextSignal.HARD_ACCEPT:
            # A rule pass must be able to WIN the window too, not just enter
            # it: floor the score at the bar so a question from a plain viewer
            # is not dethroned by a mid-score statement from a captain.
            score = max(score, rules.score_threshold)
        if self._best is None or score > self._best_score:
            if self._best is not None:
                # The dethroned incumbent is the one that lost, not the
                # newcomer that just took the slot.
                self._skip(SkipReason.LOST_WINDOW, self._best)
            self._best = event
            self._best_score = score
        else:
            self._skip(SkipReason.LOST_WINDOW, event)

    # ------------------------------------------------------------ loop

    async def run(self, deliver: Deliver) -> None:
        """Settle combos, close windows, release deferred wins. Cancel to stop."""
        while True:
            await self._clock.sleep(_TICK_S)
            if self._breaker.is_open:
                continue  # latched for the run; health shows it
            if (
                self._combos.pending_count == 0
                and self._window_opened is None
                and not self._deferred
            ):
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
        if self._deferred and not self._delivery_blocked():
            await self._release_deferred(deliver)
            return
        rules = self._window_rules
        if self._window_opened is None or rules is None:
            return
        if now - self._window_opened < rules.danmaku_window_s:
            return
        best, self._best = self._best, None
        # Kept, not zeroed: the deferred buffer ranks candidates by this score.
        best_score, self._best_score = self._best_score, 0.0
        self._window_opened = None
        self._window_rules = None
        if best is None:
            self._skip(SkipReason.WINDOW_EMPTY)
            return
        if self._delivery_blocked():
            self._defer(best, best_score)
            return
        try:
            await deliver(best)
        except Exception:
            # The winner is gone either way — the window already closed — so
            # put the loss on the books before the breaker hears about it.
            self._skip(SkipReason.DELIVER_FAILED, best)
            raise
        # The one decision this layer makes, and the only side of it the panel
        # cannot already see: the scheduler's verdict says what happened to the
        # winner, never that it beat anyone. `text` folds to a length unless an
        # operator turns viewer content on (obs/logging.py).
        log.info(
            "selector.window_won",
            score=round(best_score, 4),
            score_bar=rules.score_threshold,
            identity=best.viewer.identity,
            text=best.text,
        )
        self._delivered += 1

    def _defer(self, event: LiveEvent, score: float) -> None:
        """Keep a bounded set of window winners without starting intent TTL."""
        self._deferred_seq += 1
        self._deferred.append((score, self._deferred_seq, event))
        if len(self._deferred) <= _DEFERRED_CAPACITY:
            return
        weakest = min(range(len(self._deferred)), key=lambda index: self._deferred[index][:2])
        evicted = self._deferred.pop(weakest)
        self._skip(SkipReason.LOST_DEFERRED, evicted[2])

    async def _release_deferred(self, deliver: Deliver) -> None:
        """Deliver one best candidate and account for the rest as superseded.

        Only the best: five windows closed during one monologue must not turn
        into five queued replies the moment the streamer stops talking.
        """
        winner = max(self._deferred, key=lambda candidate: candidate[:2])
        await deliver(winner[2])
        losers = [candidate for candidate in self._deferred if candidate is not winner]
        self._deferred.clear()
        for _score, _seq, event in losers:
            self._skip(SkipReason.LOST_DEFERRED, event)
        self._delivered += 1
        log.info(
            "selector.deferred_released",
            score=round(winner[0], 4),
            identity=winner[2].viewer.identity,
            superseded=len(losers),
            text=winner[2].text,
        )

    # ------------------------------------------------------------ accounting

    def _skip(self, reason: SkipReason, event: LiveEvent | None = None) -> None:
        """One account for one dropped event: the tally, then the record."""
        self._skips[reason.value] = self._skips.get(reason.value, 0) + 1
        # The sink already carries this to the panel; the log line is for the
        # session nobody was watching. `event` is None for WINDOW_EMPTY, the
        # one reason that belongs to a window rather than to a danmaku.
        log.debug(
            "selector.skipped",
            reason=reason.value,
            identity=event.viewer.identity if event is not None else "",
            text=event.text if event is not None else None,
        )
        if self._on_skip is None:
            return
        try:
            self._on_skip(event, reason)
        except Exception as exc:
            # The sink is a panel broadcast and offer() sits on the emit path,
            # so letting this out would cost the room its danmaku over a dead
            # websocket. The tally above already landed; log the loss of the
            # detailed record rather than swallowing it.
            log.warning("selector.skip_sink_failed", reason=reason.value, error_text=str(exc)[:200])

    def status(self) -> dict[str, object]:
        return {
            "offered": self._offered,
            "delivered": self._delivered,
            "skips": dict(self._skips),
            "passes": dict(self._passes),
            "window_open": self._window_opened is not None,
            "deferred_count": len(self._deferred),
            "breaker_open": self._breaker.is_open,
            "breaker_reason": self._breaker.reason,
            "combos_suppressed": self._combos.suppressed_events,
        }


class PresenceWelcomer:
    """New-arrival burst detector: 5 first-time uids inside 45s buys ONE hello.

    Superseded by EntryCoalescer (dynamic coalescing by room load) and kept
    only while the assembly still accepts the legacy wiring; the burst_*
    config keys that feed it are already marked legacy. Each identity counts
    once per stream, so a viewer bouncing in and out is not five people.
    """

    def __init__(self, *, uniques: int = 5, window_s: float = 45.0, cooldown_s: float = 90.0):
        self._uniques = uniques
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        # Bounded so a marathon mega-room stream does not retain every
        # identity it ever saw (~144k/hour at the presence parse budget).
        # Evicting the oldest costs at most a rare double-count.
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


EntryDeliver = Callable[[tuple[LiveEvent, ...]], Awaitable[None]]


class EntryCoalescer:
    """Briefly coalesce ordinary arrivals and suppress a greeting if they speak.

    Replaces the batch-of-5 PresenceWelcomer: in a quiet room a single
    arrival is welcomed after ~1s instead of never, and the wait stretches
    with room load (EventPacingSnapshot.entry_coalesce_s) until BUSY turns
    ordinary welcomes off entirely. An arrival who posts a danmaku before the
    window closes is dropped from it — their message earns the reply, and a
    welcome on top would greet them twice.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        policy: Callable[[], EventPacingSnapshot],
    ) -> None:
        self._clock = clock
        self._policy = policy
        # Once per stream per identity, same bound and reasoning as
        # PresenceWelcomer's book.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = 8192
        self._pending: OrderedDict[str, LiveEvent] = OrderedDict()
        self._window_opened: float | None = None
        self._delivered = 0
        self._cancelled_by_danmaku = 0
        self._suppressed_busy = 0

    def offer(self, event: LiveEvent) -> None:
        identity = event.viewer.identity
        if identity in self._seen:
            return
        self._seen[identity] = None
        if len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
        if not self._policy().entry_enabled:
            self._suppressed_busy += 1
            return
        self._pending[identity] = event
        if self._window_opened is None:
            self._window_opened = self._clock.monotonic()

    def note_danmaku(self, identity: str) -> None:
        if self._pending.pop(identity, None) is not None:
            self._cancelled_by_danmaku += 1
        if not self._pending:
            self._window_opened = None

    async def run(self, deliver: EntryDeliver) -> None:
        while True:
            await self._clock.sleep(_TICK_S)
            await self._advance_entries(deliver)

    async def _advance_entries(self, deliver: EntryDeliver) -> None:
        if self._window_opened is None:
            return
        policy = self._policy()
        if not policy.entry_enabled:
            # The room got busy while a window was open: record the arrivals
            # (they are already in _seen) and say nothing.
            self._suppressed_busy += len(self._pending)
            self._pending.clear()
            self._window_opened = None
            return
        if self._clock.monotonic() - self._window_opened < policy.entry_coalesce_s:
            return
        events = tuple(self._pending.values())
        self._pending.clear()
        self._window_opened = None
        if not events:
            return
        await deliver(events)
        self._delivered += 1

    def status(self) -> dict[str, int]:
        return {
            "pending": len(self._pending),
            "delivered": self._delivered,
            "cancelled_by_danmaku": self._cancelled_by_danmaku,
            "suppressed_busy": self._suppressed_busy,
        }
