"""Assembly shares model reports without creating a second inference path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.config.derive import derive
from bilisama.config.enums import Chattiness
from bilisama.director.intent import Priority
from bilisama.director.intents import _event_ref
from bilisama.director.interaction_state import (
    REPORT_RULES,
    InteractionReport,
    InteractionState,
    parse_report,
)
from bilisama.event_pacing import EventPacer
from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer
from bilisama.ingest.events import EventKind, Gift, GuardLevel, LiveEvent, Viewer
from tests.unit.conftest import build_assembly_kit


def _event(key: str, kind: EventKind = EventKind.DANMAKU, *, uid: int = 1) -> LiveEvent:
    return LiveEvent(
        kind=kind,
        event_id=key,
        viewer=Viewer(uid=uid, name=f"观众{uid}"),
        text=key,
        gift=Gift(name="小花花", num=1, total_coin=100) if kind is EventKind.GIFT else None,
    )


def _report(
    event: LiveEvent | None = None,
    state: str = "handled",
    *,
    silence: str = "keep",
    discussion: str = "keep",
    topic: str = "",
    danmaku_summary: str = "keep",
) -> InteractionReport:
    return parse_report(
        json.dumps(
            {
                "events": (
                    []
                    if event is None
                    else [
                        {
                            "event_ref": _event_ref(event),
                            "state": state,
                            "evidence": "主播已经回应本条问题",
                        }
                    ]
                ),
                "silence": silence,
                "discussion": {"action": discussion, "topic": topic},
                "danmaku_summary": {"action": danmaku_summary},
            }
        )
    )


async def test_intake_observes_all_event_kinds_before_pause_and_preserves_feed(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    feed: list[LiveEvent] = []
    kit = build_assembly_kit(tmp_path, interaction_state=state, event_observer=feed.append)
    kit.assembly.set_event_input_enabled(False)
    kinds = (
        EventKind.DANMAKU,
        EventKind.GIFT,
        EventKind.GUARD_BUY,
        EventKind.ENTRY,
        EventKind.VIP_ENTER,
        EventKind.SUPER_CHAT,
    )
    events = [_event(f"事件-{kind.value}", kind, uid=i + 1) for i, kind in enumerate(kinds)]
    for event in events:
        await kit.assembly.on_event(event)
    context = kit.assembly.build_public_context()
    assert all(_event_ref(event) in context for event in events)
    assert feed == events and not kit.intents
    assert all(kit.store.viewer(event.viewer.identity) is not None for event in events)
    assert kit.proactive is not None
    assert "事件-danmaku" in kit.proactive._opportunities.material()


async def test_host_typed_reply_only_observes_and_does_not_cancel_an_existing_task(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    observed: list[str] = []

    async def observe(text: str) -> None:
        observed.append(text)

    kit = build_assembly_kit(tmp_path, interaction_state=state, observe_context_item=observe)
    question = _event("会上传服务器吗")
    await kit.assembly.on_event(question)
    original = kit.intents[0]
    host = replace(
        _event("不会，只保存在本地", uid=99),
        viewer=Viewer(uid=99, name="主播", is_anchor=True),
        reply_to_uid=question.viewer.uid,
        reply_to_name=question.viewer.name,
        reply_to_anchor=False,
    )
    await kit.assembly.on_event(host)
    assert kit.intents == [original]
    assert state.is_handled(question)
    assert observed and "主播本人" in observed[-1]
    assert _event_ref(host) in kit.assembly.build_public_context()
    assert kit.store.viewer(host.viewer.identity) is not None


async def test_host_typed_reply_removes_target_from_pending_danmaku_selection(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    selector = DanmakuSelector(kit.clock, thresholds=lambda: derive(Chattiness.HIGH))
    kit.assembly._selector = selector
    question = replace(_event("会上传服务器吗"), room_id=1)
    await kit.assembly.on_event(question)
    assert selector.status()["pending_count"] == 1
    host = replace(
        _event("不会，只保存在本地", uid=99),
        room_id=1,
        viewer=Viewer(uid=99, name="主播", is_anchor=True),
        reply_to_uid=question.viewer.uid,
        reply_to_name=question.viewer.name,
        reply_to_anchor=False,
    )

    await kit.assembly.on_event(host)

    assert state.is_handled(question)
    assert selector.status()["pending_count"] == 0


async def test_report_prunes_only_named_pending_danmaku_and_entry(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    selector = DanmakuSelector(kit.clock, thresholds=lambda: derive(Chattiness.HIGH))
    pacer = EventPacer(kit.clock, chattiness=lambda: Chattiness.MEDIUM)
    entries = EntryCoalescer(kit.clock, policy=pacer.snapshot)
    kit.assembly._selector, kit.assembly._entries = selector, entries
    questions = [replace(_event(f"问题{i}"), room_id=1) for i in range(2)]
    arrival = replace(_event("新观众", EventKind.ENTRY, uid=2), room_id=1)
    for event in (*questions, arrival):
        await kit.assembly.on_event(event)
    handled = kit.assembly.apply_interaction_report(_report(questions[0]))
    assert handled == {_event_ref(questions[0])}
    assert selector.status()["pending_count"] == 1
    assert entries.status()["pending"] == 1
    kit.assembly.apply_interaction_report(_report(arrival))
    assert entries.status()["pending"] == 0
    assert selector._pending == [questions[1]]
    assert kit.proactive is not None
    assert _event_ref(questions[0]) not in kit.proactive._opportunities.material_event_refs()


async def test_danmaku_summary_request_is_separate_and_uses_pre_voice_events(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    old = replace(_event("委托前的旧弹幕"), room_id=10)
    await kit.assembly.on_event(old)
    await kit.clock.advance(1)
    kit.assembly.apply_interaction_report(_report(danmaku_summary="start"), voice_started_at=1)
    await kit.clock.advance(1)
    fresh = replace(_event("委托后的最新弹幕"), room_id=10)
    await kit.assembly.on_event(fresh)
    assert kit.proactive is not None
    summary = next(intent for intent in kit.intents if intent.source == "danmaku_summary")
    assert summary.source == "danmaku_summary"
    assert summary.priority is Priority.DANMAKU_SUMMARY
    assert Priority.STREAMER > summary.priority > Priority.SUPERCHAT
    assert summary.events == (old.redacted(),)
    assert "委托前的旧弹幕" in (summary.injection.item_text or "")
    assert "委托后的最新弹幕" not in (summary.injection.item_text or "")
    assert kit.proactive.status()["danmaku_summary_pending"] is False


async def test_batch_filters_handled_members_before_budget_and_does_not_reopen_them(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state, event_rules="事件回合约定")
    pacer = EventPacer(kit.clock, chattiness=lambda: Chattiness.MEDIUM)
    kit.assembly._event_pacer = pacer
    first, second = _event("已回答"), _event("未回答")
    state.observe(first)
    kit.assembly.apply_interaction_report(_report(first))
    await kit.assembly.deliver_danmaku_batch((first,))
    assert not kit.intents and not pacer._consumed
    await kit.assembly.deliver_danmaku_batch((first, second))
    assert len(kit.intents) == 1 and kit.intents[0].events == (second,)
    assert pacer._consumed["danmaku"] == 1
    assert state.is_handled(first)
    assert _event_ref(second) in kit.assembly.build_event_context()


async def test_entry_batch_filters_handled_members_before_budget(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    pacer = EventPacer(kit.clock, chattiness=lambda: Chattiness.MEDIUM)
    kit.assembly._event_pacer = pacer
    first, second = _event("欢迎过", EventKind.ENTRY), _event("还没欢迎", EventKind.ENTRY, uid=2)
    state.observe(first)
    kit.assembly.apply_interaction_report(_report(first))
    await kit.assembly.deliver_entries((first,))
    assert not kit.intents and not pacer._consumed
    await kit.assembly.deliver_entries((first, second))
    assert len(kit.intents) == 1 and kit.intents[0].events == (second,)
    assert pacer._consumed["entry"] == 1


async def test_sc_support_thanks_keeps_its_unanswered_body_eligible(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    event = _event("SC的问题", EventKind.SUPER_CHAT)
    state.observe(event)
    assert kit.assembly.apply_interaction_report(_report(event, "support_thanked")) == set()
    kit.assembly._submit_event(event)
    assert len(kit.intents) == 1
    assert "support_thanked" in kit.assembly.build_public_context()
    assert kit.assembly.apply_interaction_report(_report(event)) == {_event_ref(event)}


async def test_silence_is_shared_with_proactive_and_new_events_do_not_release_it(
    tmp_path: Path,
) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    kit.assembly.apply_interaction_report(_report(silence="enter"))
    await kit.assembly.on_event(_event("新问题"))
    kit.assembly.apply_interaction_report(_report())
    assert state.silenced
    assert kit.proactive is not None and kit.proactive.status()["silenced"] is True
    assert "助手持续静默：是" in kit.assembly.build_public_context()
    kit.assembly.apply_interaction_report(_report(silence="release"))
    assert bool(state.silenced) is False
    assert kit.proactive.status()["silenced"] is False


@pytest.mark.parametrize("action", ["finish", "cancel"])
async def test_discussion_uses_voice_start_and_finishing_does_not_enqueue_duplicate_summary(
    tmp_path: Path,
    action: str,
) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    await kit.clock.advance(10)
    kit.assembly.apply_interaction_report(
        _report(discussion="start", topic="大家更喜欢哪个界面"),
        voice_started_at=5,
    )
    assert kit.proactive is not None
    collection = kit.proactive._opportunities._collection
    assert collection is not None and collection.started_at == 5
    kit.assembly.apply_interaction_report(_report(discussion=action))
    assert kit.proactive.status()["opinion_collection_pending"] is False
    assert not kit.intents


async def test_shared_report_rules_are_opt_in_and_replay_clears_old_state(tmp_path: Path) -> None:
    legacy = build_assembly_kit(tmp_path / "legacy")
    assert REPORT_RULES.strip() not in legacy.assembly.build_public_context()
    assert legacy.assembly.apply_interaction_report(_report(silence="enter")) == set()
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(
        tmp_path / "enabled",
        interaction_state=state,
        voice_rules="语音回合约定",
        event_rules="事件回合约定",
    )
    event = _event("上一例问题")
    await kit.assembly.on_event(event)
    kit.assembly.apply_interaction_report(_report(event, silence="enter"))
    for context in (kit.assembly.build_context(), kit.assembly.build_event_context()):
        assert REPORT_RULES.strip() in context and _event_ref(event) in context
    kit.assembly.set_replay_context("本例仅讨论界面")
    assert not state.silenced and state.context() == ""
    assert _event_ref(event) not in kit.assembly.build_public_context()
    assert "本例仅讨论界面" in kit.assembly.build_public_context()
    assert REPORT_RULES.strip() in kit.assembly.build_public_context()


async def test_promoted_vip_has_one_canonical_record_for_the_same_arrival(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state, event_rules="事件回合规则")
    arrival = replace(
        _event("舰长来了", EventKind.ENTRY),
        viewer=Viewer(uid=1, name="舰长观众", guard_level=GuardLevel.CAPTAIN),
    )
    await kit.assembly.on_event(arrival)
    promoted = replace(arrival, kind=EventKind.VIP_ENTER)
    assert len(kit.intents) == 1
    context = kit.assembly.build_public_context()
    assert _event_ref(promoted) in context and _event_ref(arrival) not in context
    kit.assembly.apply_interaction_report(_report(promoted))
    assert state.is_handled(promoted)


async def test_invalid_report_does_not_prune_or_change_proactive_state(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    with pytest.raises(ValueError, match="未知"):
        kit.assembly.apply_interaction_report(_report(_event("从未收到"), silence="enter"))
    assert not state.silenced
    assert kit.proactive is not None and kit.proactive.status()["silenced"] is False
