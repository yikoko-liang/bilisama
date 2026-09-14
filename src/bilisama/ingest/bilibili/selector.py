"""Bounded danmaku batches and gift combos; semantics belong to the model.

The current room pacing still chooses when a batch can speak. No local text
score, question matcher or content-similarity rule decides whether to reply.
Only transport deduplication and bounded queues run before model judgment.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bilisama.ingest.bilibili.safety import CircuitBreaker, DedupRing, GiftComboAggregator
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.obs.logging import get_logger
from bilisama.obs.outcome import SkipReason

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.derive import DerivedThresholds
    from bilisama.event_pacing import EventPacingSnapshot

__all__ = ["SELECTOR_KINDS", "DanmakuSelector", "EntryCoalescer", "PresenceWelcomer", "SkipSink"]
log = get_logger(__name__)
SELECTOR_KINDS = frozenset({EventKind.DANMAKU, EventKind.GIFT})
_TICK_S = 0.25
_BATCH_CAPACITY = 8
_DEFERRED_CAPACITY = 32
Deliver = Callable[[LiveEvent], Awaitable[None]]
BatchDeliver = Callable[[tuple[LiveEvent, ...]], Awaitable[None]]
SkipSink = Callable[[LiveEvent | None, SkipReason], None]


class DanmakuSelector:
    """Collect a bounded batch; never classify viewer intent locally."""

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
        # Keep the old context callables source-compatible for library callers.
        # They no longer decide admission; the model reads the shared history.
        self._clock = clock
        self._thresholds = thresholds
        self._on_skip = on_skip
        self._delivery_blocked = delivery_blocked or (lambda: False)
        self._ring = DedupRing()
        self._combos = GiftComboAggregator()
        self._breaker = CircuitBreaker()
        self._pending: list[LiveEvent] = []
        self._deferred: list[LiveEvent] = []
        self._window_opened: float | None = None
        self._window_rules: DerivedThresholds | None = None
        self._offered = 0
        self._model_candidates = 0
        self._delivered = 0
        self._skips: dict[str, int] = {}

    def reset_for_replay(self) -> None:
        """Do not carry candidates or dedup state between test cases."""
        self._ring = DedupRing()
        self._combos = GiftComboAggregator()
        self._breaker = CircuitBreaker()
        self._pending.clear()
        self._deferred.clear()
        self._window_opened = None
        self._window_rules = None

    def offer(self, event: LiveEvent) -> None:
        """Admit by event identity, not content, length or addressee."""
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
        self._model_candidates += 1
        if self._window_opened is None:
            self._window_opened = now
            self._window_rules = self._thresholds()
            log.debug("selector.window_opened", window_s=self._window_rules.danmaku_window_s)
        self._pending.append(event)
        if len(self._pending) > _BATCH_CAPACITY:
            self._skip(SkipReason.QUEUE_FULL, self._pending.pop(0))

    async def run(self, deliver: Deliver, *, deliver_batch: BatchDeliver | None = None) -> None:
        """Settle gift combos and deliver a single candidate batch per window."""

        async def fallback(events: tuple[LiveEvent, ...]) -> None:
            # Legacy library callers can observe raw candidates. Production
            # always wires the batch callback to one model request.
            for event in events:
                await deliver(event)

        batch_sink = deliver_batch or fallback
        while True:
            await self._clock.sleep(_TICK_S)
            if self._breaker.is_open:
                continue
            try:
                await self._advance(self._clock.monotonic(), deliver, batch_sink)
            except (OSError, RuntimeError, ValueError) as exc:
                if self._breaker.record_failure(self._clock.monotonic(), str(exc)[:200]):
                    log.error("selector.breaker_open", error_text=str(exc)[:200])
                else:
                    log.warning("selector.advance_failed", error_text=str(exc)[:200])

    async def _advance(self, now: float, deliver: Deliver, batch_sink: BatchDeliver) -> None:
        while (due := self._combos.peek_due(now)) is not None:
            combo_id, aggregate = due
            await deliver(aggregate)
            self._combos.commit(combo_id, now)
            self._delivered += 1
            await self._clock.sleep(0)
        rules = self._window_rules
        if (
            self._window_opened is not None
            and rules is not None
            and now - self._window_opened >= rules.danmaku_window_s
        ):
            self._deferred.extend(self._pending)
            self._pending.clear()
            self._window_opened = None
            self._window_rules = None
            while len(self._deferred) > _DEFERRED_CAPACITY:
                self._skip(SkipReason.QUEUE_FULL, self._deferred.pop(0))
        if not self._deferred or self._delivery_blocked():
            return
        # Freshness/capacity only: do not rank or group by meaning. Older
        # observations remain in shared context but not the reply backlog.
        overflow = self._deferred[:-_BATCH_CAPACITY]
        events = tuple(self._deferred[-_BATCH_CAPACITY:])
        await batch_sink(events)
        self._deferred.clear()
        for event in overflow:
            self._skip(SkipReason.LOST_DEFERRED, event)
        self._delivered += len(events)
        log.info("selector.batch_delivered", count=len(events), superseded=len(overflow))

    def _skip(self, reason: SkipReason, event: LiveEvent | None = None) -> None:
        self._skips[reason.value] = self._skips.get(reason.value, 0) + 1
        log.debug(
            "selector.skipped",
            reason=reason.value,
            identity=event.viewer.identity if event else "",
            text=event.text if event else None,
        )
        if self._on_skip is not None:
            try:
                self._on_skip(event, reason)
            except (OSError, RuntimeError, ValueError) as exc:
                log.warning(
                    "selector.skip_sink_failed", reason=reason.value, error_text=str(exc)[:200]
                )

    def status(self) -> dict[str, object]:
        return {
            "offered": self._offered,
            "delivered": self._delivered,
            "skips": dict(self._skips),
            "passes": {"model": self._model_candidates},
            "window_open": self._window_opened is not None,
            "pending_count": len(self._pending),
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

    def reset_for_replay(self) -> None:
        """Remove pending welcomes and presence dedup between replay cases."""
        self._pending.clear()
        self._seen.clear()
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
