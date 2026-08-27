"""Ingest-side safety pieces: dedup, per-viewer cooldown, breaker, combo merge.

Four small components, wired between the raw event stream and the selector
(stage 6 B4). Numeric defaults are N.E.K.O's production-tuned values (plan
section 5.3); only the per-viewer cooldown is a config knob
(`[interaction.danmaku] per_uid_cooldown_s`), the rest are constants here.

All four are synchronous and take `now` — the injected clock's monotonic
seconds — as an argument. They never sleep and never look at a wall clock,
so tests pin them with plain floats and stay deterministic.

The dedup window is a LOOK-BACK, not a wait: `seen()` answers immediately.
Plan section 2.8 calls this out — a waiting window would tax every event
350ms of latency for nothing.
"""

from __future__ import annotations

import dataclasses
from collections import OrderedDict, deque
from dataclasses import dataclass

from bilisama.ingest.events import LiveEvent, cny_from_gold

__all__ = [
    "CircuitBreaker",
    "DedupRing",
    "GiftComboAggregator",
    "PerUidCooldown",
]

DEDUP_WINDOW_S = 0.35
DEDUP_CAPACITY = 4096
BREAKER_THRESHOLD = 3
BREAKER_WINDOW_S = 60.0
COMBO_IDLE_S = 1.0
COMBO_SUPPRESS_S = 600.0


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
        return stamp is not None and now - stamp <= self._window_s

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


class PerUidCooldown:
    """One reply per viewer per window.

    The REPLY arms it, not the attempt: the selector calls `mark()` only for
    the danmaku it actually picks, so a viewer whose messages keep losing the
    window never gets locked out. Keyed on Viewer.identity, which falls back
    to uid_hash — masked viewers cool down too.
    """

    def __init__(self, cooldown_s: float = 60.0, capacity: int = 8192) -> None:
        self._cooldown_s = cooldown_s
        self._capacity = capacity
        self._marked: OrderedDict[str, float] = OrderedDict()

    def blocked(self, identity: str, now: float) -> bool:
        stamp = self._marked.get(identity)
        return stamp is not None and now - stamp < self._cooldown_s

    def mark(self, identity: str, now: float) -> None:
        self._marked[identity] = now
        self._marked.move_to_end(identity)
        while len(self._marked) > self._capacity or (
            self._marked and now - next(iter(self._marked.values())) >= self._cooldown_s
        ):
            self._marked.popitem(last=False)


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
        return self._open

    def reset(self) -> None:
        self._open = False
        self._reason = ""
        self._failures.clear()


@dataclass
class _Combo:
    first: LiveEvent
    last: LiveEvent
    num: int
    total_coin: int
    hits: int
    last_add: float


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
    """

    def __init__(self, idle_s: float = COMBO_IDLE_S, suppress_s: float = COMBO_SUPPRESS_S) -> None:
        self._idle_s = idle_s
        self._suppress_s = suppress_s
        self._pending: OrderedDict[str, _Combo] = OrderedDict()
        # Insertion-ordered by settle time, so purging expired suppressions
        # walks the front instead of scanning the whole map on every add.
        self._settled_at: OrderedDict[str, float] = OrderedDict()
        self._suppressed_events = 0

    @property
    def suppressed_events(self) -> int:
        return self._suppressed_events

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, event: LiveEvent, now: float) -> None:
        if event.gift is None:
            raise ValueError("GiftComboAggregator.add() wants a gift event with gift set")
        combo_id = event.gift.combo_id or f"{event.viewer.identity}:{event.gift.gift_id}"
        self._purge_settled(now)
        if combo_id in self._settled_at:
            self._suppressed_events += 1
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
            )
            return
        combo.last = event
        combo.num += event.gift.num
        combo.total_coin += event.gift.total_coin
        combo.hits += 1
        combo.last_add = now

    def peek_due(self, now: float) -> tuple[str, LiveEvent] | None:
        """The oldest settled aggregate, left in place until commit()."""
        for combo_id, combo in self._pending.items():
            if now - combo.last_add >= self._idle_s:
                return combo_id, self._aggregate(combo)
        return None

    def commit(self, combo_id: str, now: float) -> None:
        """Delivery succeeded: drop the combo and arm its suppress window."""
        if self._pending.pop(combo_id, None) is not None:
            self._settled_at[combo_id] = now

    def _aggregate(self, combo: _Combo) -> LiveEvent:
        gift = combo.last.gift
        assert gift is not None  # add() enforced it
        merged = dataclasses.replace(
            gift, num=combo.num, total_coin=combo.total_coin, aggregated_count=combo.hits
        )
        # One division over the summed coins, not fifty accumulated ones —
        # 50 x 0.1 in floats is 4.999999999999998 and the ranking thresholds
        # downstream compare against round numbers.
        value = cny_from_gold(combo.total_coin) if merged.coin_type == "gold" else 0.0
        # Identity of the FIRST hit (id, platform timestamp) so the dedup key
        # stays stable however long the combo ran; freshness of the last.
        return dataclasses.replace(
            combo.last,
            gift=merged,
            value_cny=value,
            event_id=combo.first.event_id,
            ts_ms=combo.first.ts_ms,
        )

    def _purge_settled(self, now: float) -> None:
        while self._settled_at:
            _, oldest = next(iter(self._settled_at.items()))
            if now - oldest <= self._suppress_s:
                break
            self._settled_at.popitem(last=False)
