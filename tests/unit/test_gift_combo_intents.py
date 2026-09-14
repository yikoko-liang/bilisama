"""Gift aggregates retain exact members instead of borrowing the first hit's ID."""

from __future__ import annotations

import dataclasses
import json

import pytest

from bilisama.clock import FakeClock
from bilisama.director import intents
from bilisama.director.intent import Priority
from bilisama.director.interaction_state import InteractionState, parse_report
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer


def _gift(key: str, *, num: int = 1, at: float = 1, hits: int = 1) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=123,
        event_id=key,
        viewer=Viewer(uid=1, name="小灯"),
        gift=Gift(
            gift_id=8,
            name="小花花",
            num=num,
            total_coin=4000 * num,
            coin_type="gold",
            unit_battery=40,
            combo_id="combo-one",
            aggregated_count=hits,
        ),
        value_cny=4 * num,
        ts_ms=int(at * 1000),
        recv_at=at,
        raw={"secret": "not-model-input"},
    )


def _display(events: tuple[LiveEvent, ...]) -> LiveEvent:
    last = events[-1]
    assert last.gift is not None
    gifts = [event.gift for event in events]
    assert all(gift is not None for gift in gifts)
    return dataclasses.replace(
        last,
        event_id="gift-combo:test-independent-display",
        gift=dataclasses.replace(
            last.gift,
            num=sum(gift.num for gift in gifts if gift is not None),
            total_coin=sum(gift.total_coin for gift in gifts if gift is not None),
            aggregated_count=sum(gift.aggregated_count for gift in gifts if gift is not None),
        ),
        value_cny=sum(event.value_cny for event in events),
    )


def _handled(state: InteractionState, event: LiveEvent) -> None:
    state.apply(
        parse_report(
            json.dumps(
                {
                    "events": [
                        {
                            "event_ref": intents._event_ref(event),
                            "state": "handled",
                            "evidence": "谢谢小灯这朵花",
                        }
                    ],
                    "silence": "keep",
                    "discussion": {"action": "keep", "topic": ""},
                }
            )
        )
    )


def test_combo_targets_members_and_not_the_display_record() -> None:
    members = (_gift("first"), _gift("second", at=2))
    aggregate = _display(members)
    intent = intents.gift_combo_intent(aggregate, members, now=3)
    assert intent.event == aggregate.redacted()
    assert intent.events == tuple(event.redacted() for event in members)
    assert intent.dedup_key == aggregate.dedup_key
    assert intent.injection.item_text is not None
    assert intent.injection.reply.instructions is not None
    assert intents._event_ref(aggregate) not in intent.injection.item_text
    assert intents._event_ref(aggregate) not in intent.injection.reply.instructions
    for event in members:
        assert intents._event_ref(event) in intent.injection.item_text
        assert intents._event_ref(event) in intent.injection.reply.instructions
    assert "not-model-input" not in intent.injection.item_text


def test_handled_first_hit_does_not_cancel_unhandled_remainder_or_change_policy() -> None:
    first, second = _gift("first"), _gift("second", at=7)
    original = intents.gift_combo_intent(
        _display((first, second)),
        (first, second),
        now=8,
        max_tokens=71,
        gift_battery_high=60,
        gift_battery_medium=30,
        protect_ms=987,
        protect_paid=True,
        base_instructions="事件上下文",
    )
    original = dataclasses.replace(original, expires_at=90)
    state = InteractionState(FakeClock())
    state.observe(first)
    state.observe(second)
    _handled(state, first)
    remaining = state.filter_intent(original)
    assert remaining is not None and remaining.event is not None
    assert remaining.events == (second.redacted(),)
    assert remaining.event.event_id != first.event_id
    assert remaining.event.event_id != second.event_id
    assert remaining.dedup_key != original.dedup_key
    assert remaining.event.gift == second.gift
    assert remaining.event.value_cny == second.value_cny
    assert (remaining.event.ts_ms, remaining.event.recv_at) == (second.ts_ms, second.recv_at)
    assert remaining.event.viewer == second.viewer and remaining.event.raw is None
    assert remaining.priority is original.priority is Priority.BIG_GIFT
    assert remaining.expires_at == 90 and remaining.created_at == original.created_at
    assert remaining.requeue_on_interrupt is original.requeue_on_interrupt is True
    assert remaining.injection.reply.protected is True
    assert remaining.injection.reply.protect_ms == 987
    assert remaining.injection.reply.max_tokens == 71
    assert remaining.injection.reply.base_instructions == "事件上下文"
    assert original.injection.reply.instructions is not None
    assert remaining.injection.reply.instructions is not None
    assert remaining.injection.item_text is not None
    before = original.injection.reply.instructions.removesuffix(
        intents._candidate_focus(original.events)
    )
    after = remaining.injection.reply.instructions.removesuffix(
        intents._candidate_focus(remaining.events)
    )
    assert before == after
    assert intents._event_ref(first) not in remaining.injection.item_text
    assert intents._event_ref(first) not in remaining.injection.reply.instructions
    assert intents._event_ref(second) in remaining.injection.item_text
    assert state.filter_intent(original) == remaining


def test_all_handled_members_remove_the_combo_but_unknown_display_ref_does_not() -> None:
    members = (_gift("first"), _gift("second", at=2))
    aggregate = _display(members)
    intent = intents.gift_combo_intent(aggregate, members, now=3)
    state = InteractionState(FakeClock())
    for event in members:
        state.observe(event)
    assert state.filter_intent(intent) is intent
    for event in members:
        _handled(state, event)
    assert state.filter_intent(intent) is None


def test_compacted_prefix_keeps_its_fact_limit_and_does_not_alias_a_raw_hit() -> None:
    prefix = dataclasses.replace(
        _gift("gift-combo-prefix:independent", num=12, hits=12),
        text="压缩前缀合计，逐笔明细已不完整；不能确认某一笔已答谢就撤销整个合计",
    )
    latest = _gift("latest", num=2, at=3)
    intent = intents.gift_combo_intent(_display((prefix, latest)), (prefix, latest), now=4)
    assert intent.injection.item_text is not None
    assert prefix.text in intent.injection.item_text
    state = InteractionState(FakeClock())
    state.observe(prefix)
    state.observe(latest)
    _handled(state, latest)
    remaining = state.filter_intent(intent)
    assert remaining is not None and remaining.event is not None
    assert remaining.events == (prefix.redacted(),)
    assert remaining.event.gift is not None
    assert remaining.event.gift.num == 12 and remaining.event.gift.aggregated_count == 12
    assert remaining.event.gift.total_coin == 48000 and remaining.event.value_cny == 48
    assert remaining.injection.item_text is not None
    assert prefix.text in remaining.injection.item_text


@pytest.mark.parametrize("members", [(), (LiveEvent(kind=EventKind.DANMAKU),)])
def test_invalid_combo_members_are_rejected(members: tuple[LiveEvent, ...]) -> None:
    with pytest.raises(ValueError):
        intents.gift_combo_intent(_gift("aggregate"), members, now=0)


def test_aggregate_cannot_borrow_a_member_id() -> None:
    first = _gift("first")
    with pytest.raises(ValueError, match="独立"):
        intents.gift_combo_intent(first, (first,), now=0)
