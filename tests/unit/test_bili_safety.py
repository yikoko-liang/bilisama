"""The four ingest safety pieces, pinned with explicit clocks.

Every component takes `now` as a plain float, so these tests need no event
loop and no sleeping — time is just an argument.
"""

from __future__ import annotations

import dataclasses

from bilisama.ingest.bilibili.safety import (
    CircuitBreaker,
    DedupRing,
    GiftComboAggregator,
    PerUidCooldown,
)
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer


def _gift_event(
    *, uid: int = 9, gift_id: int = 31036, num: int = 1, coin: int = 100, event_id: str = ""
) -> LiveEvent:
    viewer = Viewer(uid=uid, name="老板")
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=777,
        viewer=viewer,
        gift=Gift(
            gift_id=gift_id,
            name="小心心",
            num=num,
            coin_type="gold",
            total_coin=coin,
            combo_id=f"{viewer.identity}:{gift_id}",
        ),
        value_cny=coin / 1000.0,
        event_id=event_id,
        ts_ms=1_755_000_000_000,
    )


# ------------------------------------------------------------------ DedupRing


def test_dedup_drops_the_replay_inside_the_window() -> None:
    ring = DedupRing()
    assert not ring.seen("dm:1", now=10.0)
    assert ring.seen("dm:1", now=10.2), "0.2s later is a transport replay"
    assert not ring.seen("dm:2", now=10.2), "a different key is not"


def test_dedup_forgets_after_the_window() -> None:
    ring = DedupRing(window_s=0.35)
    assert not ring.seen("dm:1", now=10.0)
    assert not ring.seen("dm:1", now=10.4), "same content later is a genuine repost"


def test_dedup_capacity_evicts_the_oldest_first() -> None:
    ring = DedupRing(window_s=100.0, capacity=3)
    for i in range(4):
        assert not ring.seen(f"k{i}", now=1.0 + i * 0.01)
    # k0 was evicted by capacity even though the window would keep it.
    assert not ring.seen("k0", now=1.05)
    assert ring.seen("k3", now=1.05)


# ------------------------------------------------------------------ PerUidCooldown


def test_cooldown_is_armed_by_mark_not_by_asking() -> None:
    cd = PerUidCooldown(cooldown_s=60.0)
    assert not cd.blocked("uid:42", now=5.0)
    assert not cd.blocked("uid:42", now=6.0), "asking twice must not lock anyone out"
    cd.mark("uid:42", now=6.0)
    assert cd.blocked("uid:42", now=65.9)
    assert not cd.blocked("uid:42", now=66.0)
    assert not cd.blocked("uid:7", now=10.0), "someone else is unaffected"


def test_cooldown_capacity_stays_bounded() -> None:
    cd = PerUidCooldown(cooldown_s=1000.0, capacity=8)
    for i in range(20):
        cd.mark(f"uid:{i}", now=float(i))
    assert len(cd._marked) <= 8


# ------------------------------------------------------------------ CircuitBreaker


def test_breaker_trips_on_the_third_failure_within_the_window() -> None:
    breaker = CircuitBreaker()
    assert not breaker.record_failure(10.0, "boom")
    assert not breaker.record_failure(30.0, "boom")
    assert breaker.record_failure(60.0, "mapping exploded")
    assert breaker.is_open
    assert breaker.reason == "mapping exploded"


def test_breaker_ignores_failures_spread_beyond_the_window() -> None:
    breaker = CircuitBreaker(threshold=3, window_s=60.0)
    assert not breaker.record_failure(0.0)
    assert not breaker.record_failure(61.0), "first one aged out"
    assert not breaker.record_failure(122.0)
    assert not breaker.is_open


def test_breaker_stays_open_until_reset() -> None:
    """No auto-close: the failures it counts are systematic (plan 15.11)."""
    breaker = CircuitBreaker()
    tripped = False
    for t in (1.0, 2.0, 3.0):
        tripped = breaker.record_failure(t)
    assert tripped
    assert breaker.record_failure(500.0), "still open long after the window"
    breaker.reset()
    assert not breaker.is_open
    assert not breaker.record_failure(501.0), "and the old failures don't count again"


# ------------------------------------------------------------------ GiftComboAggregator


def _drain(agg: GiftComboAggregator, now: float) -> list[LiveEvent]:
    """Deliver-and-commit everything due, the way the selector does."""
    out: list[LiveEvent] = []
    while (due := agg.peek_due(now)) is not None:
        combo_id, aggregate = due
        out.append(aggregate)
        agg.commit(combo_id, now)
    return out


def test_fifty_hit_combo_becomes_one_event_with_the_right_totals() -> None:
    agg = GiftComboAggregator()
    for i in range(50):
        agg.add(_gift_event(coin=100, event_id=f"gift:t{i}"), now=10.0 + i * 0.02)
    last_add = 10.0 + 49 * 0.02
    assert agg.peek_due(last_add + 0.5) is None, "still inside the 1.0s idle window"
    settled = _drain(agg, last_add + 1.0)
    assert len(settled) == 1
    event = settled[0]
    assert event.gift is not None
    assert event.gift.num == 50
    assert event.gift.total_coin == 5000
    assert event.gift.aggregated_count == 50
    assert event.value_cny == 5.0
    assert event.event_id == "gift:t0", "keeps the first hit's identity"


def test_settled_combo_is_suppressed_for_the_window_then_free_again() -> None:
    agg = GiftComboAggregator()
    agg.add(_gift_event(), now=10.0)
    assert len(_drain(agg, 11.5)) == 1
    agg.add(_gift_event(), now=20.0)  # same viewer, same gift, settled 8.5s ago
    assert _drain(agg, 30.0) == []
    assert agg.suppressed_events == 1
    agg.add(_gift_event(), now=11.5 + 601.0)  # suppress window over
    assert len(_drain(agg, 11.5 + 603.0)) == 1


def test_distinct_viewers_and_gifts_aggregate_separately() -> None:
    agg = GiftComboAggregator()
    agg.add(_gift_event(uid=1), now=10.0)
    agg.add(_gift_event(uid=2), now=10.1)
    agg.add(_gift_event(uid=1, gift_id=999), now=10.2)
    settled = _drain(agg, 12.0)
    assert len(settled) == 3


def test_uncommitted_combo_survives_a_failed_delivery() -> None:
    """peek without commit models a delivery failure: the combo stays
    pending — retried next tick — and its suppression is never armed."""
    agg = GiftComboAggregator()
    agg.add(_gift_event(), now=10.0)
    first = agg.peek_due(11.5)
    assert first is not None
    again = agg.peek_due(11.6)  # no commit happened: still there
    assert again is not None and again[0] == first[0]
    agg.commit(first[0], 11.7)
    assert agg.peek_due(11.8) is None, "committed and gone"


def test_masked_viewer_combo_keys_on_the_hash() -> None:
    base = _gift_event(uid=0)
    viewer = dataclasses.replace(base.viewer, uid=0, uid_hash="deadbeef")
    masked = dataclasses.replace(
        base,
        viewer=viewer,
        gift=dataclasses.replace(
            base.gift if base.gift is not None else Gift(), combo_id=f"{viewer.identity}:31036"
        ),
    )
    agg = GiftComboAggregator()
    agg.add(masked, now=10.0)
    agg.add(masked, now=10.2)
    settled = _drain(agg, 12.0)
    assert len(settled) == 1
    assert settled[0].gift is not None and settled[0].gift.aggregated_count == 2
