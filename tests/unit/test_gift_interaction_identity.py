"""A combo must not make one raw gift reference stand for every contribution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.config.derive import derive
from bilisama.config.enums import Chattiness
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intents import _event_ref
from bilisama.director.interaction_state import (
    DiscussionUpdate,
    EventUpdate,
    InteractionReport,
    InteractionState,
)
from bilisama.director.scheduler import Scheduler
from bilisama.ingest.bilibili import safety
from bilisama.ingest.bilibili.safety import GiftComboAggregator
from bilisama.ingest.bilibili.selector import DanmakuSelector
from bilisama.ingest.events import LiveEvent
from tests.fakes.bili import gift_event
from tests.unit.conftest import AssemblyKit, build_assembly_kit
from tests.unit.test_director import _ScriptedLink


async def _queued_combo(
    tmp_path: Path,
) -> tuple[AssemblyKit, InteractionState, Scheduler, LiveEvent, LiveEvent, list[LiveEvent]]:
    state = InteractionState(FakeClock())
    feed: list[LiveEvent] = []
    kit = build_assembly_kit(tmp_path, interaction_state=state, event_observer=feed.append)
    selector = DanmakuSelector(kit.clock, thresholds=lambda: derive(Chattiness.MEDIUM))
    kit.assembly._selector = selector
    scheduler = Scheduler(
        _ScriptedLink(), SpeakingFloor(kit.clock), kit.clock, interaction_state=state
    )
    kit.assembly._submit = scheduler.submit
    first = replace(gift_event(num=1, coin=100, event_id="hit-first"), ts_ms=1000)
    second = replace(gift_event(num=2, coin=200, event_id="hit-second"), ts_ms=1200)
    for event in (first, second):
        await kit.assembly.on_event(event)
    await kit.clock.advance(2)
    await selector._advance(
        kit.clock.monotonic(),
        kit.assembly.deliver_selected,
        kit.assembly.deliver_danmaku_batch,
        kit.assembly.deliver_gift_batch,
    )
    assert selector._combos.pending_count == 0
    assert scheduler.status()["queued"] == 1
    return kit, state, scheduler, first, second, feed


async def test_queued_aggregate_does_not_overwrite_the_first_raw_gift_fact(tmp_path: Path) -> None:
    kit, state, scheduler, first, second, feed = await _queued_combo(tmp_path)
    try:
        assert feed == [first, second]
        assert state._events[_event_ref(first)] == first.redacted()
        assert state._events[_event_ref(second)] == second.redacted()
    finally:
        scheduler.panic_mute()
        await scheduler.drain_pending_io()
        kit.store.close()


@pytest.mark.parametrize("handled_index", [0, 1])
async def test_handling_one_queued_combo_hit_keeps_the_other_contribution(
    tmp_path: Path, handled_index: int
) -> None:
    kit, state, scheduler, first, second, feed = await _queued_combo(tmp_path)
    handled, remaining = (first, second) if handled_index == 0 else (second, first)
    try:
        kit.assembly.apply_interaction_report(
            InteractionReport(
                events=[
                    EventUpdate(
                        event_ref=_event_ref(handled),
                        state="handled",
                        evidence="主播明确感谢了这一笔礼物",
                    )
                ],
                silence="keep",
                discussion=DiscussionUpdate(action="keep", topic=""),
            )
        )
        scheduler.refresh_interactions()
        assert state.is_handled(handled)
        assert not state.is_handled(remaining)
        assert feed == [first, second]
        assert scheduler.status()["queued"] == 1
        pending = scheduler._heap[0].intent
        assert pending.event is not None and pending.event.gift is not None
        assert remaining.gift is not None
        assert pending.event.gift.num == remaining.gift.num
        assert pending.event.gift.total_coin == remaining.gift.total_coin
        assert _event_ref(handled) not in (pending.injection.reply.instructions or "")
        assert _event_ref(remaining) in (pending.injection.reply.instructions or "")
    finally:
        scheduler.panic_mute()
        await scheduler.drain_pending_io()
        kit.store.close()


def test_compacted_prefix_has_a_separate_identity_and_preserves_honest_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(safety, "COMBO_MEMBER_CAPACITY", 2)
    agg = GiftComboAggregator()
    events = tuple(gift_event(event_id=f"hit-{i}", num=i + 1, coin=(i + 1) * 100) for i in range(4))
    for index, event in enumerate(events):
        agg.add(event, float(index))
    due = agg.peek_due(5)
    assert due is not None
    combo_id, aggregate = due
    members = agg.contributions(combo_id)
    prefix = members[0]
    assert prefix.event_id.startswith("gift-combo-prefix:")
    assert prefix.dedup_key not in {event.dedup_key for event in events}
    assert prefix.gift is not None and prefix.gift.num == 3
    assert prefix.gift.aggregated_count == 2
    assert "逐笔明细已不完整" in prefix.text
    assert members[1:] == events[2:]
    assert aggregate.gift is not None and aggregate.gift.num == 10
    assert aggregate.gift.total_coin == 1000
    assert aggregate.gift.aggregated_count == 4
    assert aggregate.dedup_key not in {event.dedup_key for event in (*events, prefix)}
    agg.discard_events({events[0].dedup_key})
    assert agg.unresolved_discards == 1
    assert agg.peek_due(5) == due


async def test_failed_member_aware_delivery_keeps_the_same_combo_for_retry() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.MEDIUM))
    events = (gift_event(event_id="a"), gift_event(event_id="b"))
    for event in events:
        selector.offer(event)
    await clock.advance(2)
    attempts: list[tuple[LiveEvent, tuple[LiveEvent, ...]]] = []

    async def unused(_: LiveEvent) -> None:
        pytest.fail("带成员的礼物交付不应走旧的单事件出口")

    async def unused_batch(_: tuple[LiveEvent, ...]) -> None:
        pytest.fail("礼物交付不应走弹幕出口")

    async def deliver(aggregate: LiveEvent, members: tuple[LiveEvent, ...]) -> None:
        attempts.append((aggregate, members))
        if len(attempts) == 1:
            raise RuntimeError("本次交付失败")

    with pytest.raises(RuntimeError, match="交付失败"):
        await selector._advance(2, unused, unused_batch, deliver)
    assert selector._combos.pending_count == 1
    await selector._advance(2, unused, unused_batch, deliver)
    assert selector._combos.pending_count == 0
    assert attempts[0] == attempts[1]
    assert attempts[1][1] == events
