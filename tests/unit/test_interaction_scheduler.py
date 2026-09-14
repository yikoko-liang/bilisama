"""Model-owned interaction state affects queued work, not late live output."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from bilisama.clock import FakeClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.intents import (
    _event_ref,
    danmaku_batch_intent,
    entry_welcome_intent,
    intent_for,
)
from bilisama.director.interaction_state import (
    DiscussionUpdate,
    EventUpdate,
    InteractionReport,
    InteractionState,
)
from bilisama.director.scheduler import Scheduler
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.realtime import link
from tests.unit.test_director import _ScriptedLink


def _event(uid: int, kind: EventKind = EventKind.DANMAKU) -> LiveEvent:
    return LiveEvent(
        kind=kind,
        event_id=f"interaction:{kind.value}:{uid}",
        viewer=Viewer(uid=uid, name=f"观众{uid}"),
        text=f"问题{uid}",
    )


def _intent(event: LiveEvent) -> Intent:
    result = intent_for(event, now=1)
    assert result is not None
    return result


def _update(
    state: InteractionState,
    event: LiveEvent | None = None,
    *,
    status: Literal["pending", "processing", "handled", "support_thanked"] = "handled",
    silence: Literal["keep", "enter", "release"] = "keep",
) -> None:
    state.apply(
        InteractionReport(
            events=(
                [EventUpdate(event_ref=_event_ref(event), state=status, evidence="主播实际回应")]
                if event is not None
                else []
            ),
            silence=silence,
            discussion=DiscussionUpdate(action="keep", topic=""),
        )
    )


def _rig() -> tuple[FakeClock, _ScriptedLink, InteractionState, Scheduler]:
    clock = FakeClock()
    speech = _ScriptedLink()
    state = InteractionState(clock)
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    return clock, speech, state, scheduler


async def _cleanup(scheduler: Scheduler) -> None:
    scheduler.panic_mute()
    await scheduler.drain_pending_io()


def test_refresh_removes_only_the_handled_event_and_releases_its_key() -> None:
    _, speech, state, scheduler = _rig()
    a, b = _event(1), _event(2)
    for event in (a, b):
        state.observe(event)
        scheduler.submit(_intent(event))
    _update(state, a)
    scheduler.refresh_interactions()
    assert scheduler.status()["queued"] == 1
    assert a.dedup_key not in scheduler._queued_keys
    assert b.dedup_key in scheduler._queued_keys
    assert scheduler.verdicts[0].reason is not None
    assert scheduler.verdicts[0].reason.value == "event.host_handled"
    assert speech.cancels == []
    assert scheduler._next_dispatchable() == _intent(b)


def test_partial_batch_is_rebuilt_with_only_unanswered_members() -> None:
    _, _, state, scheduler = _rig()
    a, b = _event(1), _event(2)
    for event in (a, b):
        state.observe(event)
    original = danmaku_batch_intent((a, b), now=1)
    scheduler.submit(original)
    _update(state, a)
    scheduler.refresh_interactions()
    rebuilt = scheduler._next_dispatchable()
    assert rebuilt is not None and rebuilt.events == (b,)
    assert rebuilt.dedup_key != original.dedup_key
    assert original.dedup_key not in scheduler._queued_keys
    assert rebuilt.dedup_key in scheduler._queued_keys
    assert _event_ref(a) not in (rebuilt.injection.reply.instructions or "")
    assert "问题1" not in (rebuilt.injection.item_text or "")
    assert "问题2" in (rebuilt.injection.item_text or "")


def test_revoked_batch_does_not_return_after_partial_handling_refresh() -> None:
    _, _, state, scheduler = _rig()
    a, b = _event(1), _event(2)
    for event in (a, b):
        state.observe(event)
    original = danmaku_batch_intent((a, b), now=1)
    scheduler.submit(original)
    assert scheduler.revoke(original.dedup_key or "")
    _update(state, a)
    scheduler.refresh_interactions()
    assert scheduler._next_dispatchable() is None
    assert scheduler._queued_keys == set()
    assert len(scheduler.verdicts) == 1
    assert scheduler.verdicts[0].reason is not None
    assert scheduler.verdicts[0].reason.value == "platform.revoked"


def test_entry_builder_preserves_redacted_members_for_exact_batch_filtering() -> None:
    _, _, state, scheduler = _rig()
    a, b = _event(1, EventKind.ENTRY), _event(2, EventKind.ENTRY)
    for event in (a, b):
        state.observe(event)
    original = entry_welcome_intent((a, b), now=1)
    assert original.events == (a.redacted(), b.redacted())
    scheduler.submit(original)
    _update(state, a)
    scheduler.refresh_interactions()
    remaining = scheduler._next_dispatchable()
    assert remaining is not None and remaining.events == (b.redacted(),)
    assert _event_ref(a) not in (remaining.injection.reply.instructions or "")
    assert _event_ref(b) in (remaining.injection.reply.instructions or "")


def test_processing_high_priority_event_does_not_starve_other_events() -> None:
    _, _, state, scheduler = _rig()
    high, normal = _event(1, EventKind.SUPER_CHAT), _event(2)
    for event in (high, normal):
        state.observe(event)
        scheduler.submit(_intent(event))
    _update(state, high, status="processing")
    scheduler.refresh_interactions()
    assert scheduler._next_dispatchable() == _intent(normal)
    assert scheduler.status()["queued"] == 1
    _update(state, high, status="pending")
    scheduler.refresh_interactions()
    assert scheduler._next_dispatchable() == _intent(high)


def test_welcome_event_waits_on_the_floor_without_being_requeued_or_skipped() -> None:
    """A speaking host delays playback eligibility, not the event's identity."""
    _, _, state, scheduler = _rig()
    event = _event(1, EventKind.VIP_ENTER)
    state.observe(event)
    intent = _intent(event)
    scheduler.submit(intent)

    scheduler._floor.on_speech_started()
    assert scheduler._next_dispatchable() is None
    assert scheduler.status()["queued"] == 1
    assert scheduler.verdicts == []

    scheduler._floor.on_speech_stopped(quiet_s=0.0)
    ready = scheduler._next_dispatchable()
    assert ready is not None
    assert ready.dedup_key == intent.dedup_key


def test_silence_holds_events_and_proactive_until_released() -> None:
    _, _, state, scheduler = _rig()
    proactive = Intent("proactive", Priority.PROACTIVE, Injection(link.ReplySpec()))
    event = _event(1)
    state.observe(event)
    scheduler.submit(_intent(event))
    scheduler.submit(proactive)
    _update(state, silence="enter")
    scheduler.refresh_interactions()
    assert scheduler._next_dispatchable() is None
    assert scheduler.status()["queued"] == 2
    scheduler._wake.clear()
    _update(state, silence="release")
    scheduler.refresh_interactions()
    assert scheduler._wake.is_set()
    assert scheduler._next_dispatchable() == _intent(event)


@pytest.mark.parametrize("playing", [False, True])
async def test_later_host_text_does_not_cancel_generating_or_playing_reply(playing: bool) -> None:
    _, speech, state, scheduler = _rig()
    event = _event(1)
    state.observe(event)
    await scheduler._dispatch(_intent(event))
    handle = speech.handles[0]
    if playing:
        scheduler._floor.on_playback(True)
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="回答"))
    _update(state, event)
    scheduler.refresh_interactions()
    await asyncio.sleep(0)
    assert speech.cancels == []
    assert scheduler.controls.empty()
    assert scheduler._parked if playing else scheduler._active is not None
    await _cleanup(scheduler)


@pytest.mark.parametrize("status", ["handled", "processing"])
async def test_status_changed_during_item_write_is_checked_before_generation(
    status: Literal["handled", "processing"],
) -> None:
    clock = FakeClock()
    state = InteractionState(clock)
    event = _event(1)
    state.observe(event)

    class UpdatingLink(_ScriptedLink):
        async def add_context_item(self, text: str, *, role: str = "user") -> None:
            await super().add_context_item(text, role=role)
            _update(state, event, status=status)

    speech = UpdatingLink()
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    await scheduler._dispatch(_intent(event))
    assert speech.replies == []
    assert scheduler.status()["queued"] == (1 if status == "processing" else 0)
    assert not scheduler.status()["dispatching"]
    await _cleanup(scheduler)


async def test_partial_answer_arriving_during_write_changes_dispatch_focus() -> None:
    clock = FakeClock()
    state = InteractionState(clock)
    a, b = _event(1), _event(2)
    for event in (a, b):
        state.observe(event)

    class UpdatingLink(_ScriptedLink):
        async def add_context_item(self, text: str, *, role: str = "user") -> None:
            await super().add_context_item(text, role=role)
            _update(state, a)

    speech = UpdatingLink()
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    await scheduler._dispatch(danmaku_batch_intent((a, b), now=1))
    assert len(speech.replies) == 1
    assert _event_ref(a) not in (speech.replies[0].instructions or "")
    assert _event_ref(b) in (speech.replies[0].instructions or "")
    assert scheduler.reply_intent(speech.handles[0].handle_id) is not None
    await _cleanup(scheduler)


@pytest.mark.parametrize("parked", [False, True])
async def test_vip_handled_by_host_does_not_resurrect_after_barge_in(parked: bool) -> None:
    _, speech, state, scheduler = _rig()
    event = _event(1, EventKind.VIP_ENTER)
    state.observe(event)
    await scheduler._dispatch(_intent(event))
    handle = speech.handles[0]
    scheduler._floor.on_playback(True)
    if parked:
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="欢迎"))
    _update(state, event)
    scheduler._handle_event(link.SpeechStarted())
    if not parked:
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.CANCELLED))
    scheduler._floor.on_playback(False)
    scheduler._flush_played()
    assert scheduler.status()["queued"] == 0
    assert any(
        v.reason is not None and v.reason.value == "event.host_handled" for v in scheduler.verdicts
    )
    await _cleanup(scheduler)


async def test_voice_skip_can_keep_generation_for_its_nonspoken_report() -> None:
    _, speech, _, scheduler = _rig()
    handle = link.ReplyHandle(implicit=True)
    scheduler._handle_event(link.ReplyStarted(handle))
    scheduler.skip_reply(handle, detail="主播要求先听", preserve_generation=True)
    scheduler.skip_reply(handle, detail="主播要求先听", preserve_generation=True)
    await asyncio.sleep(0)
    assert speech.cancels == []
    assert scheduler.status()["implicit_active"]
    assert len(scheduler.verdicts) == 1
    assert scheduler.controls.empty()
    scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="[SKIP]"))
    assert not scheduler.status()["implicit_active"]
    assert len(scheduler.verdicts) == 1
    await _cleanup(scheduler)


async def test_panic_still_cancels_a_voice_skip_that_preserved_generation() -> None:
    _, speech, _, scheduler = _rig()
    handle = link.ReplyHandle(implicit=True)
    scheduler._handle_event(link.ReplyStarted(handle))
    scheduler.skip_reply(handle, preserve_generation=True)
    scheduler.panic_mute()
    scheduler.panic_mute()
    await asyncio.sleep(0)
    assert speech.cancels == [handle]
    assert len(scheduler.verdicts) == 1
    await scheduler.drain_pending_io()


async def test_preserve_generation_does_not_keep_a_dispatched_event_skip_alive() -> None:
    _, speech, state, scheduler = _rig()
    event = _event(1)
    state.observe(event)
    await scheduler._dispatch(_intent(event))
    scheduler.skip_reply(speech.handles[0], preserve_generation=True)
    await asyncio.sleep(0)
    assert speech.cancels == speech.handles
    assert scheduler._active is None
    await _cleanup(scheduler)


async def test_revoke_reports_only_a_new_queued_withdrawal() -> None:
    _, speech, state, scheduler = _rig()
    a, b = _event(1), _event(2)
    for event in (a, b):
        state.observe(event)
    scheduler.submit(_intent(a))
    assert scheduler.revoke(a.dedup_key) is True
    assert scheduler.revoke(a.dedup_key) is False
    assert scheduler.revoke("不存在") is False
    await scheduler._dispatch(_intent(b))
    assert scheduler.revoke(b.dedup_key) is False
    scheduler._floor.on_playback(True)
    scheduler._handle_event(
        link.ReplyDone(speech.handles[0], link.ReplyStatus.COMPLETED, text="回答")
    )
    assert scheduler.revoke(b.dedup_key) is False
    await _cleanup(scheduler)


@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.parametrize("done_before_speech", [False, True])
async def test_genuine_barge_in_notifies_once_with_source_and_partial_text(
    implicit: bool, done_before_speech: bool
) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    interrupted: list[tuple[Intent | None, link.ReplyHandle, str]] = []
    scheduler = Scheduler(
        speech,
        SpeakingFloor(clock),
        clock,
        on_interrupted=lambda intent, handle, text: interrupted.append((intent, handle, text)),
    )
    intent = None if implicit else _intent(_event(1))
    if intent is None:
        handle = link.ReplyHandle(implicit=True)
        scheduler._handle_event(link.ReplyStarted(handle))
    else:
        await scheduler._dispatch(intent)
        handle = speech.handles[0]
    scheduler._handle_event(link.ReplyTextDelta(handle, "先说到这里"))
    done = link.ReplyDone(handle, link.ReplyStatus.CANCELLED, text="先说到这里")
    if done_before_speech:
        scheduler._handle_event(done)
        assert not interrupted
    scheduler._handle_event(link.SpeechStarted())
    if not done_before_speech:
        scheduler._handle_event(done)
    scheduler._handle_event(link.SpeechStarted())
    assert interrupted == [(intent, handle, "先说到这里")]
    await _cleanup(scheduler)


@pytest.mark.parametrize("implicit", [False, True])
async def test_playback_barge_in_reports_completed_generation_as_interrupted(
    implicit: bool,
) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    interrupted: list[tuple[Intent | None, link.ReplyHandle, str]] = []
    scheduler = Scheduler(
        speech,
        SpeakingFloor(clock),
        clock,
        on_interrupted=lambda intent, handle, text: interrupted.append((intent, handle, text)),
    )
    intent = None if implicit else _intent(_event(1))
    if intent is None:
        handle = link.ReplyHandle(implicit=True)
        scheduler._handle_event(link.ReplyStarted(handle))
    else:
        await scheduler._dispatch(intent)
        handle = speech.handles[0]
    scheduler._floor.on_playback(True)
    scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="尚未播完"))
    scheduler._handle_event(link.SpeechStarted())
    scheduler._floor.on_playback(False)
    scheduler._flush_played()
    assert interrupted == [(intent, handle, "尚未播完")]
    await _cleanup(scheduler)


@pytest.mark.parametrize("kind", [EventKind.DANMAKU, EventKind.VIP_ENTER])
async def test_audio_already_drained_before_next_speech_is_not_interrupted(
    kind: EventKind,
) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    interrupted: list[str] = []
    scheduler = Scheduler(
        speech,
        SpeakingFloor(clock),
        clock,
        on_interrupted=lambda _intent, _handle, text: interrupted.append(text),
    )
    await scheduler._dispatch(_intent(_event(1, kind)))
    scheduler._floor.on_playback(True)
    scheduler._handle_event(
        link.ReplyDone(speech.handles[0], link.ReplyStatus.COMPLETED, text="已播完")
    )
    scheduler._floor.on_playback(False)
    scheduler._handle_event(link.SpeechStarted())
    assert interrupted == []
    assert scheduler.status()["queued"] == 0
    assert scheduler.verdicts[-1].phase.value == "played"
    await _cleanup(scheduler)


async def test_late_done_of_preserved_skip_does_not_release_newer_implicit_floor() -> None:
    _, _, _, scheduler = _rig()
    old, new = link.ReplyHandle(implicit=True), link.ReplyHandle(implicit=True)
    scheduler._handle_event(link.ReplyStarted(old))
    scheduler.skip_reply(old, preserve_generation=True)
    scheduler._handle_event(link.ReplyStarted(new))
    scheduler._handle_event(link.ReplyDone(old, link.ReplyStatus.COMPLETED, text="[SKIP]"))
    assert scheduler.status()["implicit_active"]
    assert scheduler._floor.blocking_reason() is not None
    assert scheduler._implicit is not None and scheduler._implicit.handle is new
    await _cleanup(scheduler)


@pytest.mark.parametrize("stage", ["queued", "selected", "generating", "playing"])
@pytest.mark.parametrize("report_after_eviction", [False, True])
async def test_inflight_vip_state_survives_payload_window_eviction(
    stage: str, report_after_eviction: bool
) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    state = InteractionState(clock, capacity=2)
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    event = _event(1, EventKind.VIP_ENTER)
    state.observe(event)
    scheduler.submit(_intent(event))
    selected = scheduler._next_dispatchable() if stage != "queued" else None
    if stage in ("generating", "playing"):
        assert selected is not None
        await scheduler._dispatch(selected)
    if stage == "playing":
        scheduler._floor.on_playback(True)
        scheduler._handle_event(
            link.ReplyDone(speech.handles[0], link.ReplyStatus.COMPLETED, text="欢迎")
        )
    if not report_after_eviction:
        _update(state, event)
    for uid in range(2, 5):
        state.observe(_event(uid))
    assert len(state._events) == 2
    assert _event_ref(event) not in state._events
    if report_after_eviction:
        _update(state, event)
    assert state.is_handled(event)
    if stage == "queued":
        scheduler.refresh_interactions()
    elif stage == "selected":
        assert selected is not None
        await scheduler._dispatch(selected)
    else:
        scheduler._handle_event(link.SpeechStarted())
        if stage == "generating":
            scheduler._handle_event(link.ReplyDone(speech.handles[0], link.ReplyStatus.CANCELLED))
        scheduler._floor.on_playback(False)
        scheduler._flush_played()
    assert scheduler.status()["queued"] == 0
    assert _event_ref(event) not in state._states
    await _cleanup(scheduler)


async def test_report_during_context_write_still_resolves_an_evicted_event() -> None:
    clock = FakeClock()
    state = InteractionState(clock, capacity=1)
    event = _event(1)
    state.observe(event)

    class UpdatingLink(_ScriptedLink):
        async def add_context_item(self, text: str, *, role: str = "user") -> None:
            await super().add_context_item(text, role=role)
            state.observe(_event(2))
            _update(state, event)

    speech = UpdatingLink()
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    await scheduler._dispatch(_intent(event))
    assert speech.replies == []
    assert scheduler.status()["queued"] == 0
    assert scheduler.verdicts[-1].reason is not None
    assert scheduler.verdicts[-1].reason.value == "event.host_handled"
    assert _event_ref(event) not in state._states
    await _cleanup(scheduler)


@pytest.mark.parametrize("ending", ["completed", "skip", "failed", "panic"])
async def test_terminal_tasks_release_evicted_interaction_state(ending: str) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    state = InteractionState(clock, capacity=1)
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, interaction_state=state)
    event = _event(1)
    state.observe(event)
    await scheduler._dispatch(_intent(event))
    _update(state, event)
    state.observe(_event(2))
    assert state.is_handled(event)
    handle = speech.handles[0]
    if ending == "skip":
        scheduler.skip_reply(handle)
    elif ending == "panic":
        scheduler.panic_mute()
        await scheduler.drain_pending_io()
    else:
        status = link.ReplyStatus.COMPLETED if ending == "completed" else link.ReplyStatus.FAILED
        scheduler._handle_event(link.ReplyDone(handle, status))
    assert _event_ref(event) not in state._states
    assert not state._retained
    await _cleanup(scheduler)


def test_invalid_member_rejects_the_entire_report_without_partial_changes() -> None:
    state = InteractionState(FakeClock())
    event = _event(1)
    state.observe(event)
    report = InteractionReport(
        events=[
            EventUpdate(event_ref=_event_ref(event), state="handled", evidence="主播回答了"),
            EventUpdate(event_ref="unknown", state="handled", evidence="找不到的记录"),
        ],
        silence="enter",
        discussion=DiscussionUpdate(action="keep", topic=""),
    )
    with pytest.raises(ValueError, match="编号"):
        state.apply(report)
    assert not state.is_handled(event)
    assert not state.silenced


def test_partial_support_thanks_rejects_non_sc_events() -> None:
    state = InteractionState(FakeClock())
    event = _event(1, EventKind.GIFT)
    state.observe(event)
    with pytest.raises(ValueError, match="SC"):
        _update(state, event, status="support_thanked", silence="enter")
    assert not state.is_handled(event)
    assert not state.silenced


@pytest.mark.parametrize("ending", ["skip", "panic", "failed", "requeued", "no_speech_edge"])
async def test_nonrecoverable_endings_do_not_emit_interruption_candidates(ending: str) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    interrupted: list[str] = []
    scheduler = Scheduler(
        speech,
        SpeakingFloor(clock),
        clock,
        on_interrupted=lambda _intent, _handle, text: interrupted.append(text),
    )
    kind = EventKind.VIP_ENTER if ending == "requeued" else EventKind.DANMAKU
    await scheduler._dispatch(_intent(_event(1, kind)))
    handle = speech.handles[0]
    scheduler._handle_event(link.ReplyTextDelta(handle, "不应恢复"))
    if ending == "skip":
        scheduler.skip_reply(handle)
    elif ending == "panic":
        scheduler.panic_mute()
    elif ending == "failed":
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.FAILED))
    else:
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.CANCELLED))
    if ending == "no_speech_edge":
        await clock.advance(0.4)
    scheduler._handle_event(link.SpeechStarted())
    assert interrupted == []
    await _cleanup(scheduler)
