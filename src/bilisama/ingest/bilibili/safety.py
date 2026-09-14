"""Ingest-side safety pieces: dedup, breaker, combo merge.

Three small components, wired between the raw event stream and the selector
(stage 6 B4). The dedup window and the combo timers are N.E.K.O's
production-tuned values (plan section 5.3); all are constants here.

A fourth piece used to live here: PerUidCooldown, N.E.K.O's 60s
one-reply-per-viewer window. Removed with the dynamic pacing rework — an
answered viewer's follow-up question is the best danmaku a co-host can pick,
and blocking it for a minute read as being ignored. Content-level spam is now
the selector's job (text signal + cross-viewer repetition in scoring.py), and
overall rate is the event pacer's.

The breaker's numbers are not from that list, and three of section 5.3's
constants have no home in this tree at all. Spelled out because the sentence
above used to cover the whole module, which invited the opposite reading
(ledger item 39, unrecorded anywhere until this note):

- BREAKER_THRESHOLD 3 / BREAKER_WINDOW_S 60 is our own. Section 5.3's "two
  output failures" counts output attempts; this breaker counts failures our
  own code caught on the way in — mapping bugs, handler exceptions — a
  different book with a different base rate (see CircuitBreaker below).
- The 1.5s cooldown between gift responses: not landed. Gift pacing today is
  whatever the combo aggregator's merging leaves behind.
- queue_limit 5: not landed. The scheduler's heap is unbounded
  (director/scheduler.py) and `SkipReason.QUEUE_FULL` is declared but emitted
  by nobody.

All three belong to the scheduler's output side rather than to this module,
so landing them is a director-layer change, not an ingest one.

All three are synchronous and take `now` — the injected clock's monotonic
seconds — as an argument. They never sleep and never look at a wall clock,
so tests pin them with plain floats and stay deterministic.

The dedup window is a LOOK-BACK, not a wait: `seen()` answers immediately.
Plan section 2.8 calls this out — a waiting window would tax every event
350ms of latency for nothing.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from bilisama.ingest.events import EventKind, LiveEvent, cny_from_gold
from bilisama.obs.logging import get_logger

__all__ = [
    "CircuitBreaker",
    "DedupRing",
    "GiftComboAggregator",
    "aggregate_gift_events",
]

log = get_logger(__name__)

DEDUP_WINDOW_S = 0.35
DEDUP_CAPACITY = 4096
BREAKER_THRESHOLD = 3
BREAKER_WINDOW_S = 60.0
COMBO_IDLE_S = 1.0
COMBO_SUPPRESS_S = 600.0
COMBO_MEMBER_CAPACITY = 256


class DedupRing:
    """Recently-seen event keys, bounded by both age and count.

    Absorbs transport replays: blivedm's inner reconnect re-delivers the last
    few packets, and the source only bumps session_generation on an OUTER
    restart (source.py), so this ring is what keeps an inner blip from
    producing the same reaction twice.
    """

    def __init__(self, window_s: float = DEDUP_WINDOW_S, capacity: int = DEDUP_CAPACITY) -> None:
        self._window_s = window_s
        self._capacity = capacity
        self._last_seen: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float) -> bool:
        """True when `key` already passed within the window — drop the event.

        A miss records the key, so callers ask exactly once per event.
        """
        self._purge(now)
        stamp = self._last_seen.get(key)
        self._last_seen[key] = now
        self._last_seen.move_to_end(key)
        if stamp is None or now - stamp > self._window_s:
            return False
        # The key is NOT logged: LiveEvent.dedup_key falls back to embedding the
        # danmaku body (events.py:207), and this formatter scrubs by field name,
        # so any name carrying it would either leak the text or fold to `***`.
        # The shape of the hit is what a reader needs anyway, and `window_ms`
        # says which ring spoke — the selector's 350ms one or the source's
        # guard-merge ring.
        log.debug(
            "safety.dedup_hit",
            age_ms=round((now - stamp) * 1000, 1),
            window_ms=round(self._window_s * 1000, 1),
            ring_size=len(self._last_seen),
        )
        return True

    def contains(self, key: str, now: float) -> bool:
        """Like `seen`, but records nothing.

        For callers that must not burn the key until the work behind it
        actually landed — a paid delivery that raises has to stay retryable.
        Pair it with `mark`.
        """
        self._purge(now)
        stamp = self._last_seen.get(key)
        return stamp is not None and now - stamp <= self._window_s

    def mark(self, key: str, now: float) -> None:
        """Record `key` as seen. The commit half of `contains`."""
        self._purge(now)
        self._last_seen[key] = now
        self._last_seen.move_to_end(key)

    def _purge(self, now: float) -> None:
        while self._last_seen:
            _, oldest = next(iter(self._last_seen.items()))
            if len(self._last_seen) <= self._capacity and now - oldest <= self._window_s:
                break
            self._last_seen.popitem(last=False)


class CircuitBreaker:
    """Stops the danmaku pipeline after repeated caught failures.

    Two books on purpose (plan section 15.11): this one counts failures our
    own code CAUGHT (mapping errors, handler exceptions); escaped crashes are
    SupervisedSource's book. Once open it stays open for the run — no
    auto-close, because the failures it counts are systematic. `reset()` is
    the fresh-book call at the start of a supervised restart.
    """

    def __init__(self, threshold: int = BREAKER_THRESHOLD, window_s: float = BREAKER_WINDOW_S):
        self._threshold = threshold
        self._window_s = window_s
        self._failures: deque[float] = deque()
        self._open = False
        self._reason = ""

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def reason(self) -> str:
        return self._reason

    def record_failure(self, now: float, reason: str = "") -> bool:
        """Count one failure; returns whether the breaker is (now) open."""
        if self._open:
            return True
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window_s:
            self._failures.popleft()
        if len(self._failures) >= self._threshold:
            self._open = True
            self._reason = reason or "repeated pipeline failures"
            # Logged on the TRANSITION only — the early return above means an
            # already-open breaker never reaches here, so this fires once per
            # breaker lifetime however many failures follow.
            log.warning(
                "safety.breaker_opened",
                failures=len(self._failures),
                window_s=self._window_s,
                error_text=self._reason,
            )
        return self._open

    def reset(self) -> None:
        if self._open:
            # Only the real close is a state flip. reset() also runs at the
            # start of every supervised run (source.py's _reset_run_state), and
            # a line per healthy restart would say nothing.
            log.info("safety.breaker_reset", error_text=self._reason)
        self._open = False
        self._reason = ""
        self._failures.clear()


def aggregate_gift_events(events: tuple[LiveEvent, ...]) -> LiveEvent:
    """Build a derived view without reusing a raw contribution's identity."""
    if not events or any(
        event.kind is not EventKind.GIFT or event.gift is None for event in events
    ):
        raise ValueError("礼物合计必须包含非空的礼物贡献记录")
    last = events[-1]
    gift = last.gift
    assert gift is not None
    if any(
        event.viewer.identity != last.viewer.identity
        or event.gift is None
        or event.gift.gift_id != gift.gift_id
        or event.gift.coin_type != gift.coin_type
        for event in events
    ):
        raise ValueError("礼物合计不能混合不同观众、礼物或计价类型")
    gifts = [event.gift for event in events if event.gift is not None]
    merged = dataclasses.replace(
        gift,
        num=sum(item.num for item in gifts),
        total_coin=sum(item.total_coin for item in gifts),
        aggregated_count=sum(item.aggregated_count for item in gifts),
    )
    identity = hashlib.sha256("\n".join(event.dedup_key for event in events).encode()).hexdigest()[
        :24
    ]
    return dataclasses.replace(
        last.redacted(),
        gift=merged,
        event_id=f"gift-combo:{identity}",
        value_cny=cny_from_gold(merged.total_coin) if merged.coin_type == "gold" else 0.0,
    )


@dataclass
class _Combo:
    first: LiveEvent
    last: LiveEvent
    num: int
    total_coin: int
    hits: int
    last_add: float
    members: deque[tuple[LiveEvent, float]] = field(default_factory=deque)
    # Compacted hits remain in the totals under a separate derived identity. Only
    # the newest prefix boundary is needed to restore freshness when every
    # retained hit is withdrawn. Older contributions cannot be guessed apart.
    prefix_last: tuple[LiveEvent, float] | None = None


class GiftComboAggregator:
    """Merges a gift combo into ONE aggregated event.

    Without it, a 50-hit combo is 50 big-gift intents and the scheduler
    thanks the same person fifty times. A combo settles after 1.0s of idle;
    once settled, the same combo id is suppressed for 600s, so the platform
    re-emitting combo totals (or a viewer topping up the same gift right
    after the thank-you) does not buy a second thank-you. Suppressed hits are
    counted, never silently discarded — memory still saw them at emit time,
    upstream of this component.

    `add()` takes hits in. Delivery is two-phase — `peek_due()` hands the
    oldest settled aggregate out WITHOUT removing it, and `commit()` removes
    it and arms the suppress window once the caller has actually delivered —
    so a delivery failure leaves the combo pending for the next tick instead
    of silently discarding a paid thank-you and suppressing its re-send.

    Combos still pending when the process exits are dropped; memory recorded
    every hit at emit time, so the loss is one thank-you, not the money.

    Only the latest 256 hits retain individual withdrawal records. Older hits
    stay in a compacted prefix with unchanged totals; requests targeting that
    lost detail are reported as unresolved, never guessed or called successful.
    """

    def __init__(self, idle_s: float = COMBO_IDLE_S, suppress_s: float = COMBO_SUPPRESS_S) -> None:
        self._idle_s = idle_s
        self._suppress_s = suppress_s
        self._pending: OrderedDict[str, _Combo] = OrderedDict()
        # Insertion-ordered by settle time, so purging expired suppressions
        # walks the front instead of scanning the whole map on every add.
        self._settled_at: OrderedDict[str, float] = OrderedDict()
        self._suppressed_events = 0
        self._compacted_events = 0
        self._unresolved_discards = 0

    @property
    def suppressed_events(self) -> int:
        return self._suppressed_events

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def retained_hit_count(self) -> int:
        return sum(len(combo.members) for combo in self._pending.values())

    @property
    def compacted_events(self) -> int:
        return self._compacted_events

    @property
    def unresolved_discards(self) -> int:
        return self._unresolved_discards

    def add(self, event: LiveEvent, now: float) -> None:
        if event.gift is None:
            raise ValueError("GiftComboAggregator.add() wants a gift event with gift set")
        combo_id = event.gift.combo_id or f"{event.viewer.identity}:{event.gift.gift_id}"
        self._purge_settled(now)
        if combo_id in self._settled_at:
            self._suppressed_events += 1
            log.debug(
                "safety.combo_suppressed",
                since_settle_ms=round((now - self._settled_at[combo_id]) * 1000, 1),
                suppress_s=self._suppress_s,
                suppressed_total=self._suppressed_events,
            )
            return
        combo = self._pending.get(combo_id)
        if combo is None:
            self._pending[combo_id] = _Combo(
                first=event,
                last=event,
                num=event.gift.num,
                total_coin=event.gift.total_coin,
                hits=1,
                last_add=now,
                members=deque([(event.redacted(), now)]),
            )
            return
        combo.last = event
        combo.num += event.gift.num
        combo.total_coin += event.gift.total_coin
        combo.hits += 1
        combo.last_add = now
        combo.members.append((event.redacted(), now))
        if len(combo.members) > COMBO_MEMBER_CAPACITY:
            combo.prefix_last = combo.members.popleft()
            self._compacted_events += 1
            if self._compacted_events == 1 or self._compacted_events % COMBO_MEMBER_CAPACITY == 0:
                log.warning(
                    "safety.combo_hits_compacted",
                    retained_per_combo=COMBO_MEMBER_CAPACITY,
                    compacted_total=self._compacted_events,
                )

    def discard_events(self, keys: set[str]) -> None:
        """Subtract exact retained hits, keeping newer gifts and combo timing."""
        if not keys:
            return
        removed_keys: set[str] = set()
        has_prefix = any(combo.prefix_last is not None for combo in self._pending.values())
        for combo_id, combo in tuple(self._pending.items()):
            removed = [event for event, _ in combo.members if event.dedup_key in keys]
            if not removed:
                continue
            removed_keys.update(event.dedup_key for event in removed)
            combo.members = deque(
                (event, stamp) for event, stamp in combo.members if event.dedup_key not in keys
            )
            for event in removed:
                assert event.gift is not None
                combo.num -= event.gift.num
                combo.total_coin -= event.gift.total_coin
                combo.hits -= 1
            if combo.hits == 0:
                # No suppression tombstone: a genuinely new gift in the same
                # combo must remain eligible after only earlier hits were handled.
                self._pending.pop(combo_id)
                continue
            if combo.prefix_last is None:
                combo.first = combo.members[0][0]
            if combo.members:
                combo.last, combo.last_add = combo.members[-1]
            else:
                assert combo.prefix_last is not None
                combo.last, combo.last_add = combo.prefix_last
        unresolved = {key for key in keys - removed_keys if key.startswith(f"{EventKind.GIFT}:")}
        if has_prefix and unresolved:
            self._unresolved_discards += len(unresolved)
            log.warning(
                "safety.combo_discard_unresolved",
                requested_count=len(unresolved),
                unresolved_total=self._unresolved_discards,
                reason="not_in_retained_hits",
            )

    def peek_due(self, now: float) -> tuple[str, LiveEvent] | None:
        """The oldest settled aggregate, left in place until commit()."""
        for combo_id, combo in self._pending.items():
            if now - combo.last_add >= self._idle_s:
                return combo_id, aggregate_gift_events(self.contributions(combo_id))
        return None

    def contributions(self, combo_id: str) -> tuple[LiveEvent, ...]:
        """Exact retained hits, plus an explicitly incomplete compacted prefix."""
        combo = self._pending[combo_id]
        members = tuple(event for event, _ in combo.members)
        if combo.prefix_last is None:
            return members
        boundary, _ = combo.prefix_last
        gift = boundary.gift
        assert gift is not None
        kept_gifts = [event.gift for event in members if event.gift is not None]
        prefix_gift = dataclasses.replace(
            gift,
            num=combo.num - sum(item.num for item in kept_gifts),
            total_coin=combo.total_coin - sum(item.total_coin for item in kept_gifts),
            aggregated_count=combo.hits - len(members),
        )
        mark = (
            f"{combo.first.dedup_key}:{boundary.dedup_key}:"
            f"{prefix_gift.num}:{prefix_gift.total_coin}"
        )
        identity = hashlib.sha256(mark.encode()).hexdigest()[:24]
        prefix = dataclasses.replace(
            boundary.redacted(),
            gift=prefix_gift,
            event_id=f"gift-combo-prefix:{identity}",
            value_cny=(
                cny_from_gold(prefix_gift.total_coin) if prefix_gift.coin_type == "gold" else 0.0
            ),
            text="压缩前缀合计，逐笔明细已不完整；不能确认某一笔已答谢就撤销整个合计。",
        )
        return (prefix, *members)

    def commit(self, combo_id: str, now: float) -> None:
        """Delivery succeeded: drop the combo and arm its suppress window."""
        combo = self._pending.pop(combo_id, None)
        if combo is None:
            return
        self._settled_at[combo_id] = now
        gift = combo.last.gift
        assert gift is not None  # add() enforced it
        # The merge is invisible downstream — one thank-you carries no trace of
        # the fifty hits behind it — so this is the only place that can say how
        # many were folded together and for how much.
        log.debug(
            "safety.combo_settled",
            hits=combo.hits,
            gift_name=gift.name,
            gift_num=combo.num,
            value_cny=(
                round(cny_from_gold(combo.total_coin), 3) if gift.coin_type == "gold" else 0.0
            ),
        )

    def _purge_settled(self, now: float) -> None:
        while self._settled_at:
            _, oldest = next(iter(self._settled_at.items()))
            if now - oldest <= self._suppress_s:
                break
            self._settled_at.popitem(last=False)
