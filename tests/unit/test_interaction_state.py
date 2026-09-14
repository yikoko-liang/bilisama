"""Exact event bookkeeping from model reports and trusted reply metadata."""

import json

import pytest

from bilisama.clock import FakeClock
from bilisama.director.intents import _event_ref, danmaku_batch_intent
from bilisama.director.interaction_state import InteractionState, parse_report
from bilisama.ingest.events import EventKind, LiveEvent, Viewer


def message(key: str, *, uid: int = 1, kind: EventKind = EventKind.DANMAKU) -> LiveEvent:
    return LiveEvent(kind=kind, event_id=key, viewer=Viewer(uid=uid, name="小松"), text="会开源吗")


def report(ref: str, state: str = "handled", *, silence: str = "keep") -> str:
    return json.dumps(
        {
            "events": [{"event_ref": ref, "state": state, "evidence": "下播后会发"}],
            "silence": silence,
            "discussion": {"action": "keep", "topic": ""},
        }
    )


def test_one_handled_event_does_not_cancel_same_user_or_same_name() -> None:
    state = InteractionState(FakeClock())
    events = (message("upload"), message("opensource"), message("other", uid=2))
    for event in events:
        state.observe(event)
    state.apply(parse_report(report(_event_ref(events[0]))))
    assert state.is_handled(events[0])
    assert not state.is_handled(events[1])
    assert not state.is_handled(events[2])
    original = danmaku_batch_intent(events, now=1)
    filtered = state.filter_intent(original)
    assert filtered is not None
    assert filtered.events == events[1:]
    assert _event_ref(events[0]) not in (filtered.injection.item_text or "")


def test_reading_question_is_processing_not_completed() -> None:
    state = InteractionState(FakeClock())
    event = message("question")
    state.observe(event)
    state.apply(parse_report(report(_event_ref(event), "processing")))
    assert state.is_processing(event)
    assert not state.is_handled(event)
    state.apply(parse_report(report(_event_ref(event), "pending")))
    assert not state.is_processing(event)


def test_sc_thanks_does_not_complete_the_body() -> None:
    state = InteractionState(FakeClock())
    event = message("sc", kind=EventKind.SUPER_CHAT)
    state.observe(event)
    state.apply(parse_report(report(_event_ref(event), "support_thanked")))
    assert not state.is_handled(event)
    assert not state.is_processing(event)
    state.apply(parse_report(report(_event_ref(event))))
    assert state.is_handled(event)


def test_invalid_reference_cannot_change_silence_or_cancel_anything() -> None:
    state = InteractionState(FakeClock())
    with pytest.raises(ValueError, match="编号"):
        state.apply(parse_report(report("unknown", silence="enter")))
    assert not state.silenced


def test_silence_persists_across_event_arrival_until_model_releases() -> None:
    state = InteractionState(FakeClock())
    state.apply(
        parse_report('{"events":[],"silence":"enter","discussion":{"action":"keep","topic":""}}')
    )
    state.observe(message("fresh"))
    assert state.silenced
    state.apply(
        parse_report('{"events":[],"silence":"release","discussion":{"action":"keep","topic":""}}')
    )
    assert not state.silenced


def test_terminal_state_is_idempotent_and_cannot_reopen() -> None:
    state = InteractionState(FakeClock())
    event = message("one")
    state.observe(event)
    parsed = parse_report(report(_event_ref(event)))
    assert state.apply(parsed) == {_event_ref(event)}
    assert state.apply(parsed) == set()
    state.apply(parse_report(report(_event_ref(event), "pending")))
    assert state.is_handled(event)
    state.reset()
    assert state.context() == ""
    assert not state.silenced


def test_processing_event_stays_in_context_after_new_event_burst() -> None:
    state = InteractionState(FakeClock())
    event = message("in-progress")
    state.observe(event)
    state.apply(parse_report(report(_event_ref(event), "processing")))
    for index in range(40):
        state.observe(message(f"later-{index}"))
    assert _event_ref(event) in state.context()


def test_anchor_reply_marks_only_latest_matching_audience_question() -> None:
    state = InteractionState(FakeClock())
    first = message("first", uid=7)
    second = message("second", uid=7)
    state.observe(first)
    state.observe(second)
    anchor = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=777,
        viewer=Viewer(uid=42, name="主播", is_anchor=True),
        text="不会，文件只保存在本地。",
        reply_to_uid=7,
        reply_to_name="小松",
        reply_to_anchor=False,
        recv_at=3.0,
    )

    assert state.mark_anchor_reply(anchor) == {_event_ref(second)}
    assert not state.is_handled(first)
    assert state.is_handled(second)


def test_anchor_reply_without_confirmed_audience_target_does_not_mark_anything() -> None:
    state = InteractionState(FakeClock())
    question = message("question", uid=7)
    state.observe(question)
    anchor = LiveEvent(
        kind=EventKind.DANMAKU,
        viewer=Viewer(uid=42, name="主播", is_anchor=True),
        text="我先说一下。",
        reply_to_uid=7,
        reply_to_name="小松",
    )

    assert state.mark_anchor_reply(anchor) == set()
    assert not state.is_handled(question)


@pytest.mark.parametrize("raw", ["{}", "null", "[]", '{"events":"all"}', "not json"])
def test_malformed_report_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_report(raw)
