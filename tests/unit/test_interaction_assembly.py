"""Assembly shares model reports without creating a second inference path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bilisama.app import SummaryOutcome
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


async def test_host_typed_reply_matches_a_masked_viewer_by_the_reply_name(
    tmp_path: Path,
) -> None:
    """bilibili masks ordinary viewers' uids; the host's reply still names the
    target. hard-19 on 2026-09-17: 小松 (uid 0, hash only) asked, the host
    typed 「@小松 不会…」, and she answered 小松 anyway."""
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    selector = DanmakuSelector(kit.clock, thresholds=lambda: derive(Chattiness.HIGH))
    kit.assembly._selector = selector
    question = replace(
        _event("会上传服务器吗"), room_id=1, viewer=Viewer(uid=0, uid_hash="h-1", name="小松")
    )
    await kit.assembly.on_event(question)
    host = replace(
        _event("不会，只保存在本地", uid=99),
        room_id=1,
        viewer=Viewer(uid=99, name="主播", is_anchor=True),
        reply_to_uid=91001,
        reply_to_name="小松",
        reply_to_anchor=False,
    )
    await kit.assembly.on_event(host)
    assert state.is_handled(question)
    assert selector.status()["pending_count"] == 0
    # A different name does not match, masked or not.
    other = replace(
        _event("我也想问", uid=0), room_id=1, viewer=Viewer(uid=0, uid_hash="h-2", name="小柏")
    )
    await kit.assembly.on_event(other)
    await kit.assembly.on_event(replace(host, event_id="host-2", text="@小松 对的"))
    assert not state.is_handled(other)


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


async def test_summary_delegation_by_marker_needs_no_report_channel(tmp_path: Path) -> None:
    """The [SUMMARY] head starts the same summary the function report used to,
    on any provider: no InteractionState, no tool channel, same boundary rule."""
    kit = build_assembly_kit(tmp_path)
    old = replace(_event("委托前的旧弹幕"), room_id=10)
    await kit.assembly.on_event(old)
    await kit.clock.advance(1)
    assert kit.assembly.request_danmaku_summary(voice_started_at=1) is SummaryOutcome.DELIVERED
    await kit.clock.advance(1)
    fresh = replace(_event("委托后的最新弹幕"), room_id=10)
    await kit.assembly.on_event(fresh)
    assert kit.proactive is not None
    summary = next(intent for intent in kit.intents if intent.source == "danmaku_summary")
    assert summary.priority is Priority.DANMAKU_SUMMARY
    assert summary.events == (old.redacted(),)
    assert "委托后的最新弹幕" not in (summary.injection.item_text or "")
    assert kit.proactive.status()["danmaku_summary_pending"] is False


async def test_a_summary_includes_what_she_already_answered_and_says_so(tmp_path: Path) -> None:
    """16:02 on 2026-09-17: 小满 asked, she answered, the host came back and
    asked "what did people ask you" — and got "no new danmaku". The reply
    lane settling a line must not hide it from the host's own request."""
    from bilisama.obs.outcome import Outcome, Phase, Verdict

    kit = build_assembly_kit(tmp_path)
    asked = replace(_event("我们刚才为什么把自动分类砍掉了"), room_id=10)
    await kit.assembly.on_event(asked)
    kit.assembly.note_verdict(
        Verdict(
            intent_id=kit.intents[0].dedup_key,
            source="danmaku",
            outcome=Outcome.SPOKEN,
            phase=Phase.PLAYED,
        )
    )
    await kit.clock.advance(20)
    assert kit.assembly.request_danmaku_summary(voice_started_at=20) is SummaryOutcome.DELIVERED
    summary = next(intent for intent in kit.intents if intent.source == "danmaku_summary")
    item = summary.injection.item_text or ""
    assert "自动分类砍掉了" in item and "[你已回过]" in item
    # A proactive topic having drawn on a line is that lane's bookkeeping,
    # not this one's: still summarized.
    assert kit.proactive is not None
    kit.proactive._opportunities.mark_used({_event_ref(asked)})
    kit.proactive._opportunities._summarized.clear()  # the second ask is about a new line below
    await kit.clock.advance(40)
    assert kit.assembly.request_danmaku_summary(voice_started_at=60) is SummaryOutcome.DELIVERED
    assert "自动分类砍掉了" in (kit.intents[-1].injection.item_text or "")
    assert "你当时怎么答的" in (summary.injection.reply.instructions or "")


async def test_summary_delegation_with_no_backlog_says_so_and_arms_nothing(tmp_path: Path) -> None:
    """The streamer asked; silence is not an answer (2026-09-16). One plain
    "no new danmaku" line goes out, the request is not left armed, and a
    second delegation right behind it (the report channel repeating the
    marker's) stays quiet."""
    kit = build_assembly_kit(tmp_path)
    await kit.clock.advance(1)
    assert kit.assembly.request_danmaku_summary(voice_started_at=1) is SummaryOutcome.EMPTY
    assert kit.proactive is not None
    assert kit.proactive.status()["danmaku_summary_pending"] is False
    empty = [intent for intent in kit.intents if intent.source == "danmaku_summary"]
    assert len(empty) == 1 and empty[0].events == ()
    assert "没有新弹幕" in (empty[0].injection.reply.instructions or "")
    assert "没有尚未处理的观众弹幕" in (empty[0].injection.item_text or "")
    assert kit.assembly.request_danmaku_summary(voice_started_at=1) is SummaryOutcome.SILENT
    later = replace(_event("委托之后才来的弹幕"), room_id=10)
    await kit.assembly.on_event(later)
    assert (
        sum(1 for intent in kit.intents if intent.source == "danmaku_summary") == 1
    ), "a request with no eligible backlog must not fire on a later, unrelated danmaku"


async def test_the_report_channel_still_starts_a_summary_the_same_way(tmp_path: Path) -> None:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    old = replace(_event("旧弹幕"), room_id=10)
    await kit.assembly.on_event(old)
    await kit.clock.advance(1)
    kit.assembly.apply_interaction_report(_report(danmaku_summary="start"), voice_started_at=1)
    assert sum(1 for intent in kit.intents if intent.source == "danmaku_summary") == 1
    # A second start over the same backlog finds it consumed: one delivery per delegation.
    assert kit.assembly.request_danmaku_summary(voice_started_at=1) is SummaryOutcome.SILENT
    assert sum(1 for intent in kit.intents if intent.source == "danmaku_summary") == 1


_NAMES = {"userName": "AI代码侠土豆", "username": "AI代码侠土豆", "agentName": "豆腐"}


def _from(name: str, uid: int, text: str, **fields: object) -> LiveEvent:
    return replace(
        LiveEvent(
            kind=EventKind.DANMAKU,
            event_id=f"chat:{uid}:{text}",
            viewer=Viewer(uid=uid, name=name),
            text=text,
        ),
        room_id=0,
        **fields,  # type: ignore[arg-type]
    )


async def test_a_danmaku_typed_at_another_viewer_never_enters_the_reply_lane(
    tmp_path: Path,
) -> None:
    """hard-11 s1 (2026-09-15 15:53:33): 「@白团 你那个按钮…」was answered.
    The weak fact stops it before the funnel; memory and the shared
    observations still see it, so she can follow the thread without
    joining it."""
    observed: list[str] = []

    async def observe(text: str) -> None:
        observed.append(text)

    kit = build_assembly_kit(tmp_path, variables=_NAMES, observe_context_item=observe)
    await kit.assembly.on_event(_from("小路", 1, "@白团 你那个按钮是不是也越修越歪？"))
    assert kit.intents == []
    assert kit.assembly.status()["viewer_chat_skipped"] == 1
    assert kit.store.recent_events(limit=5), "memory still saw it"
    await kit.assembly.flush_event_observations()
    assert any("越修越歪" in text for text in observed), "shared context still saw it"
    # Addressed to the host, or to her, or turned to the room: the model decides.
    await kit.assembly.on_event(_from("小路", 1, "@豆腐 你说这按钮歪不歪"))
    await kit.assembly.on_event(_from("小路", 1, "@白团 主播你看这个"))
    assert len(kit.intents) == 2
    assert kit.assembly.status()["viewer_chat_skipped"] == 1


async def test_the_platform_reply_target_is_honoured_the_same_way(tmp_path: Path) -> None:
    kit = build_assembly_kit(tmp_path, variables=_NAMES)
    await kit.assembly.on_event(
        _from(
            "小路", 1, "你那个按钮呢", reply_to_uid=2, reply_to_name="白团", reply_to_anchor=False
        )
    )
    assert kit.intents == []
    await kit.assembly.on_event(
        _from("小路", 1, "你那个按钮呢", reply_to_uid=7, reply_to_name="土豆", reply_to_anchor=True)
    )
    assert len(kit.intents) == 1


async def test_a_reply_from_the_viewer_who_was_mentioned_is_chat_by_fact(
    tmp_path: Path,
) -> None:
    """hard-11 s2: 白团 writes back six seconds after being @'d. Since
    2026-09-17 that is viewer chat by definition — kept out of the reply lane
    by the harness, not left to the model — unless the words turn to the
    host, her or the room. Long after the window it is an ordinary danmaku."""
    kit = build_assembly_kit(tmp_path, variables=_NAMES)
    await kit.assembly.on_event(_from("小路", 1, "@白团 你那个按钮是不是也越修越歪？"))
    await kit.clock.advance(6)
    await kit.assembly.on_event(_from("白团", 2, "哈哈哈哈哈哈笑死，笨蛋deepseek"))
    assert kit.intents == [], "written back to 小路: not a reply candidate"
    assert kit.assembly.status()["viewer_chat_reasons"] == {"at_viewer": 1, "thread_reply": 1}
    await kit.clock.advance(3)
    await kit.assembly.on_event(_from("白团", 2, "主播这个按钮到底怎么调"))
    assert len(kit.intents) == 1, "turned to the host: an ordinary danmaku even inside the window"
    assert "主播这个按钮到底怎么调" in (kit.intents[0].injection.item_text or "")
    await kit.clock.advance(120)
    await kit.assembly.on_event(_from("白团", 2, "笑死"))
    assert len(kit.intents) == 2, "long after the window: ordinary"
    assert "被观众" not in (kit.intents[1].injection.item_text or "")


async def test_an_opinion_answering_the_host_is_an_ordinary_danmaku(tmp_path: Path) -> None:
    """hard-09: no @ anywhere, so nothing here is chat — the batch reaches the
    model, and the rules tell it to merge and hand the views to the host."""
    kit = build_assembly_kit(tmp_path, variables=_NAMES)
    await kit.assembly.on_event(
        _from("橘子", 3, "额aigc短剧大家都是倍速看啊，看剧情罢了，细粒度没这么重要")
    )
    await kit.assembly.on_event(_from("草莓", 4, "短剧建模都长一个样，分不清脸连剧情都对不上啊"))
    assert len(kit.intents) == 2 and kit.assembly.status()["viewer_chat_skipped"] == 0
    rules = kit.intents[0].injection.reply.instructions or ""
    assert "能到你这里的观众弹幕都不是互聊" in rules
    assert "观众回应主播提问或观点的弹幕按正常弹幕处理" in rules
    assert "只是观众之间问答" not in rules and "没有@也可能是观众互聊" not in rules


async def test_a_replay_reset_forgets_the_threads(tmp_path: Path) -> None:
    kit = build_assembly_kit(tmp_path, variables=_NAMES)
    await kit.assembly.on_event(_from("小路", 1, "@白团 你那个按钮是不是也越修越歪？"))
    kit.assembly.set_replay_context("下一例")
    await kit.assembly.on_event(_from("白团", 2, "笑死"))
    assert "被观众" not in (kit.intents[0].injection.item_text or "")
