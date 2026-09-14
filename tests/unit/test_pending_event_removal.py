"""Exact event withdrawals preserve unrelated pending room interactions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bilisama.clock import FakeClock
from bilisama.config.derive import derive
from bilisama.config.enums import Chattiness
from bilisama.event_pacing import EventPacer
from bilisama.ingest.bilibili.safety import GiftComboAggregator
from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer


def _event(key: str, *, uid: int = 1, kind: EventKind = EventKind.DANMAKU) -> LiveEvent:
    return LiveEvent(kind=kind, event_id=key, viewer=Viewer(uid=uid, name="观众"), text=key)


def _gift(key: str, *, num: int = 1, coin: int = 100, uid: int = 1) -> LiveEvent:
    return replace(
        _event(key, uid=uid, kind=EventKind.GIFT),
        gift=Gift(gift_id=7, name="小花花", num=num, total_coin=coin, coin_type="gold"),
    )


async def _collect(selector: DanmakuSelector, now: float) -> list[LiveEvent]:
    collected: list[LiveEvent] = []

    async def single(event: LiveEvent) -> None:
        collected.append(event)

    async def batch(events: tuple[LiveEvent, ...]) -> None:
        collected.extend(events)

    await selector._advance(now, single, batch)
    return collected


async def test_discard_only_named_pending_danmaku_not_other_questions_by_same_user() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.HIGH))
    first, second = _event("问题一"), _event("问题二")
    selector.offer(first)
    selector.offer(second)
    selector.discard_events({first.dedup_key})
    assert selector.status()["pending_count"] == 1
    assert await _collect(selector, 13) == [second]


async def test_discard_deferred_danmaku_and_preserve_new_pending_window() -> None:
    clock = FakeClock()
    blocked = True
    selector = DanmakuSelector(
        clock, thresholds=lambda: derive(Chattiness.HIGH), delivery_blocked=lambda: blocked
    )
    first, second, third = _event("已答"), _event("未答"), _event("新问题")
    selector.offer(first)
    selector.offer(second)
    assert await _collect(selector, 13) == []
    await clock.advance(13)
    selector.offer(third)
    selector.discard_events({first.dedup_key})
    assert selector.status()["pending_count"] == 1
    assert selector.status()["deferred_count"] == 1
    blocked = False
    assert await _collect(selector, 13) == [second]
    assert await _collect(selector, 26) == [third]


async def test_removing_last_candidate_closes_window_and_starts_next_window_fresh() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.HIGH))
    first = _event("已答")
    selector.offer(first)
    selector.discard_events({first.dedup_key})
    assert selector.status()["window_open"] is False
    await clock.advance(11)
    second = _event("后来的问题")
    selector.offer(second)
    assert await _collect(selector, 13) == []
    assert await _collect(selector, 24) == [second]


async def test_empty_or_unknown_discard_changes_nothing() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.HIGH))
    event = _event("保留")
    selector.offer(event)
    selector.discard_events(set())
    selector.discard_events({_event("不存在").dedup_key})
    assert await _collect(selector, 13) == [event]


async def test_entry_discard_preserves_other_arrivals_and_existing_seen_book() -> None:
    clock = FakeClock()
    pacer = EventPacer(clock, chattiness=lambda: Chattiness.MEDIUM)
    entries = EntryCoalescer(clock, policy=pacer.snapshot)
    first = _event("进房一", uid=1, kind=EventKind.ENTRY)
    second = _event("进房二", uid=2, kind=EventKind.ENTRY)
    entries.offer(first)
    entries.offer(second)
    entries.discard_events({first.dedup_key})
    entries.offer(first)
    await clock.advance(2)
    collected: list[LiveEvent] = []

    async def deliver(events: tuple[LiveEvent, ...]) -> None:
        collected.extend(events)

    await entries._advance_entries(deliver)
    assert collected == [second]
    entries.offer(_event("进房三", uid=3, kind=EventKind.ENTRY))
    entries.discard_events({_event("进房三", uid=3, kind=EventKind.ENTRY).dedup_key})
    assert entries.status()["pending"] == 0
    assert entries._window_opened is None


@pytest.mark.parametrize("removed", [0, 1, 2])
def test_gift_discard_rebuilds_totals_first_last_and_idle_time(removed: int) -> None:
    agg = GiftComboAggregator()
    events = [_gift(f"礼物{i}", num=i + 1, coin=100 * (i + 1)) for i in range(3)]
    for index, event in enumerate(events):
        agg.add(event, float(index))
    agg.discard_events({events[removed].dedup_key})
    remaining = [event for index, event in enumerate(events) if index != removed]
    last_index = 1 if removed == 2 else 2
    assert agg.peek_due(last_index + 0.9) is None
    due = agg.peek_due(last_index + 1.0)
    assert due is not None
    result = due[1]
    assert result.event_id.startswith("gift-combo:")
    assert result.gift is not None
    assert result.gift.num == sum(event.gift.num for event in remaining if event.gift)
    assert result.gift.total_coin == sum(event.gift.total_coin for event in remaining if event.gift)
    assert result.gift.aggregated_count == 2
    assert result.value_cny == result.gift.total_coin / 1000


def test_discarding_complete_gift_combo_does_not_suppress_a_new_real_gift() -> None:
    agg = GiftComboAggregator()
    first, later = _gift("已谢的礼物"), _gift("后续新增礼物")
    agg.add(first, 0)
    agg.discard_events({first.dedup_key})
    assert agg.pending_count == 0
    agg.add(later, 1)
    due = agg.peek_due(2)
    assert due is not None and due[1].event_id.startswith("gift-combo:")
    assert agg.suppressed_events == 0


def test_large_combo_retains_bounded_detail_and_exact_recent_discard() -> None:
    agg = GiftComboAggregator()
    events = [_gift(f"连击{i}") for i in range(300)]
    for index, event in enumerate(events):
        agg.add(event, index / 100)
    assert agg.retained_hit_count == 256
    assert agg.compacted_events == 44
    agg.discard_events({events[-1].dedup_key, events[-2].dedup_key})
    due = agg.peek_due(4)
    assert due is not None and due[1].gift is not None
    assert due[1].event_id.startswith("gift-combo:")
    assert due[1].gift.num == 298
    assert due[1].gift.total_coin == 29800
    assert due[1].gift.aggregated_count == 298
    assert agg.retained_hit_count == 254


def test_discard_all_retained_hits_keeps_compacted_prefix_and_original_identity() -> None:
    agg = GiftComboAggregator()
    events = [_gift(f"连击{i}") for i in range(300)]
    for index, event in enumerate(events):
        agg.add(event, index / 100)
    agg.discard_events({event.dedup_key for event in events[44:]})
    assert agg.retained_hit_count == 0
    assert agg.peek_due(1.42) is None
    due = agg.peek_due(1.44)
    assert due is not None and due[1].gift is not None
    assert due[1].event_id.startswith("gift-combo:")
    assert due[1].gift.num == 44
    assert due[1].gift.aggregated_count == 44


def test_compacted_old_hit_is_not_guessed_or_silently_reported_removed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agg = GiftComboAggregator()
    events = [_gift(f"连击{i}") for i in range(300)]
    for index, event in enumerate(events):
        agg.add(event, index / 100)
    agg.discard_events({events[10].dedup_key})
    due = agg.peek_due(4)
    assert due is not None and due[1].gift is not None
    assert due[1].gift.num == 300
    assert agg.unresolved_discards == 1
    assert any(record.message == "safety.combo_discard_unresolved" for record in caplog.records)


async def test_selector_discard_reaches_gift_combo_and_leaves_another_viewer() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.HIGH))
    first, second = _gift("礼物一"), _gift("礼物二", uid=2)
    selector.offer(first)
    selector.offer(second)
    selector.discard_events({first.dedup_key})
    collected = await _collect(selector, 2)
    assert len(collected) == 1 and collected[0].event_id.startswith("gift-combo:")
