"""Opinion windows and interrupted material share scheduling, not idle gates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import Chattiness
from bilisama.config.schema import ProactiveConfig
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.director.intents import _event_ref
from bilisama.event_pacing import EventPacer
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.store import MemoryStore
from bilisama.obs.outcome import Outcome, Phase, Verdict
from bilisama.proactive import ProactiveTopicLoop


@dataclass
class Harness:
    loop: ProactiveTopicLoop
    clock: FakeClock
    floor: SpeakingFloor
    pacer: EventPacer
    intents: list[Intent]


@pytest.fixture
def h() -> Iterator[Harness]:
    clock = FakeClock()
    floor = SpeakingFloor(clock)
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    pacer = EventPacer(clock, chattiness=lambda: Chattiness.MEDIUM)
    intents: list[Intent] = []
    loop = ProactiveTopicLoop(
        None,
        store,
        floor,
        clock,
        submit=intents.append,
        prompt="想一个话题",
        idle_threshold_s=9999,
        event_pacer=pacer,
        collection_window_s=120,
        min_gap_s=0.0,
    )
    try:
        yield Harness(loop, clock, floor, pacer, intents)
    finally:
        store.close()


def _event(number: int, text: str, *, anchor: bool = False) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=10,
        event_id=f"event-{number}",
        viewer=Viewer(uid=number, name=f"观众{number}", is_anchor=anchor),
        text=text,
    )


def _body(intent: Intent) -> str:
    return intent.injection.item_text or ""


def _revoke_recorder(keys: list[str]) -> Callable[[str], bool]:
    def revoke(key: str) -> bool:
        keys.append(key)
        return True

    return revoke


async def test_opinion_summary_ignores_idle_and_room_busyness(h: Harness) -> None:
    h.loop.collect_opinions("新手觉得哪个操作最麻烦")
    await h.clock.advance(119)
    for number in range(100):
        event = _event(number + 1, "找设置入口很麻烦")
        h.loop.note_event(event)
        h.pacer.note_event(event)
        h.loop.note_activity()
    await h.clock.advance(1)
    assert not h.pacer.snapshot().proactive_enabled
    h.loop._tick()
    assert len(h.intents) == 1
    assert h.intents[0].dedup_key.startswith("proactive:opinions:")
    assert "新手觉得哪个操作最麻烦" in _body(h.intents[0])
    assert "找设置入口很麻烦" in _body(h.intents[0])
    assert "无有效反馈" in (h.intents[0].injection.reply.instructions or "")
    h.loop._tick()
    assert len(h.intents) == 1


async def test_window_excludes_old_anchor_handled_and_late_messages(h: Harness) -> None:
    h.loop.note_event(_event(1, "征集前的旧观点"))
    h.loop.collect_opinions("选择简单还是灵活")
    h.loop.note_event(_event(2, "主播自己的回答", anchor=True))
    removed = _event(3, "这条已经被主播解决")
    h.loop.note_event(removed)
    h.loop.note_event(_event(4, "我倾向简单"))
    h.loop.discard_events({removed.dedup_key})
    h.floor.on_speech_started()
    await h.clock.advance(121)
    h.loop.note_event(_event(5, "窗口之后的想法"))
    h.loop._tick()
    assert not h.intents
    h.floor.on_speech_stopped(quiet_s=0)
    h.loop._tick()
    body = _body(h.intents[0])
    assert "我倾向简单" in body
    for absent in ("征集前的旧观点", "主播自己的回答", "这条已经被主播解决", "窗口之后的想法"):
        assert absent not in body


async def test_early_finish_waits_for_floor_and_silence(h: Harness) -> None:
    h.loop.collect_opinions("界面偏好")
    h.loop.note_event(_event(1, "按钮少一点"))
    h.loop.set_silenced(True)
    h.loop.finish_collection()
    h.loop._tick()
    assert not h.intents
    h.loop.set_silenced(False)
    h.floor.on_speech_started()
    h.loop._tick()
    assert not h.intents
    h.floor.on_speech_stopped(quiet_s=0)
    h.loop._tick()
    assert len(h.intents) == 1


async def test_cancel_replace_empty_and_expired_collections(h: Harness) -> None:
    h.loop.collect_opinions("旧话题")
    h.loop.note_event(_event(1, "旧话题的观点"))
    h.loop.collect_opinions("新话题")
    h.loop.finish_collection()
    h.loop._tick()
    assert "旧话题的观点" not in _body(h.intents[0])
    assert "没有收到" in _body(h.intents[0])
    h.loop.collect_opinions("取消的征集")
    h.loop.cancel_collection()
    h.loop.finish_collection()
    h.loop._tick()
    assert len(h.intents) == 1
    h.loop.collect_opinions("过期征集")
    h.floor.on_speech_started()
    await h.clock.advance(500)
    h.loop._tick()
    h.floor.on_speech_stopped(quiet_s=0)
    h.loop.note_activity()
    h.loop._tick()
    assert len(h.intents) == 1


async def test_interrupted_material_is_bounded_expires_and_is_not_revived(h: Harness) -> None:
    h.loop.note_interrupted("turn-1", "voice", "之前问过怎么部署", "部署可以先")
    h.loop.note_interrupted("turn-2", "gift", "小灯送的小花花", "谢谢", event_refs=("gift-ref",))
    h.loop.discard_events({"gift-ref"})
    await h.clock.advance(31)
    h.loop._tick()
    assert "之前问过怎么部署" in _body(h.intents[0])
    candidate_material = _body(h.intents[0]).split("已经处理或撤销", 1)[0]
    assert "小灯送的小花花" not in candidate_material
    assert "先简短回顾" in (h.intents[0].injection.reply.instructions or "")
    h.loop.set_silenced(True)
    h.loop.note_interrupted("turn-3", "voice", "静默时停止的内容", "没说完")
    h.loop.set_silenced(False)
    await h.clock.advance(31)
    h.loop._tick()
    assert "静默时停止的内容" not in _body(h.intents[-1])
    h.loop.note_interrupted("turn-4", "voice", "已经过期的内容", "没说完")
    h.floor.on_speech_started()
    await h.clock.advance(400)
    h.floor.on_speech_stopped(quiet_s=0)
    h.loop._tick()
    assert "已经过期的内容" not in _body(h.intents[-1])


async def test_replay_reset_clears_all_new_state(h: Harness) -> None:
    h.loop.collect_opinions("上一例话题")
    h.loop.note_event(_event(1, "上一例观点"))
    h.loop.note_interrupted("old", "voice", "上一例未说完", "内容")
    h.loop.set_silenced(True)
    await h.loop.reset_for_replay("下一例")
    h.loop.finish_collection()
    await h.clock.advance(31)
    h.loop._tick()
    assert len(h.intents) == 1
    assert "上一例" not in _body(h.intents[0])


def test_collection_configuration_and_invalid_requests(h: Harness) -> None:
    assert ProactiveConfig().collection_window_s == 30
    with pytest.raises(ValueError):
        ProactiveConfig(collection_window_s=0)
    with pytest.raises(ValueError):
        h.loop.collect_opinions("   ")
    with pytest.raises(ValueError):
        h.loop.collect_opinions("错误时间", started_at=float("nan"))


async def test_delayed_collection_report_uses_speech_start_not_report_arrival(h: Harness) -> None:
    await h.clock.advance(2)
    h.loop.note_event(_event(1, "语音开始以前的意见"))
    await h.clock.advance(1)
    started_at = h.clock.monotonic()
    await h.clock.advance(1)
    h.loop.note_event(_event(2, "语音开始后的意见"))
    await h.clock.advance(1)
    h.loop.collect_opinions("征集话题", started_at=started_at)
    h.loop.finish_collection()
    h.loop._tick()
    assert "语音开始后的意见" in _body(h.intents[0])
    assert "语音开始以前的意见" not in _body(h.intents[0])


async def test_danmaku_summary_uses_only_messages_before_voice_request(h: Harness) -> None:
    """An explicit voice request summarizes the pre-voice unhandled backlog."""
    old = _event(1, "语音委托前的旧弹幕")
    h.loop.note_event(old)
    await h.clock.advance(2)
    h.loop.request_danmaku_summary()
    await h.clock.advance(1)
    fresh = _event(2, "语音委托后的最新弹幕")
    h.loop.note_event(fresh)

    intent = h.loop.danmaku_summary_intent((old, fresh), now=h.clock.monotonic())

    assert intent is not None
    assert intent.source == "danmaku_summary"
    assert intent.events == (old,)
    assert "语音委托前的旧弹幕" in _body(intent)
    assert "语音委托后的最新弹幕" not in _body(intent)
    assert "最近仍在升温" in (intent.injection.reply.instructions or "")
    assert "只围绕选出的一个最热话题" in (intent.injection.reply.instructions or "")


async def test_danmaku_summary_is_empty_without_a_pre_voice_message(h: Harness) -> None:
    h.loop.request_danmaku_summary()
    assert h.loop.danmaku_summary_intent((), now=h.clock.monotonic()) is None
    await h.clock.advance(1)
    fresh = _event(3, "委托之后才来的弹幕")
    h.loop.note_event(fresh)
    assert h.loop.danmaku_summary_intent((fresh,), now=h.clock.monotonic()) is None


async def test_reply_target_stays_available_for_model_judgment(h: Harness) -> None:
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        event_id="explicit-target",
        viewer=Viewer(uid=12, name="阿白"),
        text="@阿强 我觉得按钮要少一点",
        reply_to_uid=34,
        reply_to_name="阿强",
        reply_to_anchor=False,
    )
    h.loop.collect_opinions("按钮布局")
    h.loop.note_event(event)
    h.loop.finish_collection()
    h.loop._tick()
    body = _body(h.intents[0])
    assert "@其他观众" in body and "UID 34" in body and "按钮要少一点" in body


async def test_event_ref_cancellation_carries_facts_for_old_shared_history(h: Harness) -> None:
    event = _event(1, "上传位置的问题已经解决")
    h.loop.note_event(event)
    h.loop.discard_events({_event_ref(event)})
    await h.clock.advance(31)
    h.loop._tick()
    body = _body(h.intents[0])
    assert "已经处理或撤销" in body
    assert "上传位置的问题已经解决" in body


class DeferredSide:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.user = ""

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        self.user = user
        self.entered.set()
        await self.release.wait()
        return "继续聊已经解决的问题"

    async def aclose(self) -> None:
        return None


async def test_late_side_response_cannot_revive_discarded_or_silenced_material(h: Harness) -> None:
    side = DeferredSide()
    h.loop._side = side
    event = _event(1, "上传文件吗")
    h.loop.note_event(event)
    task = asyncio.create_task(h.loop._refresh())
    await side.entered.wait()
    h.loop.discard_events({_event_ref(event)})
    h.loop.set_silenced(True)
    h.loop.set_silenced(False)
    side.release.set()
    await task
    assert not h.loop.status()["candidate_ready"]


async def test_collection_submission_failure_keeps_window_for_retry(h: Harness) -> None:
    def unavailable(_intent: Intent) -> None:
        raise RuntimeError("暂时无法提交")

    h.loop.collect_opinions("选择哪个")
    h.loop.note_event(_event(1, "我选择简单一点"))
    h.loop.finish_collection()
    h.loop._submit = unavailable
    with pytest.raises(RuntimeError, match="暂时无法提交"):
        h.loop._tick()
    h.loop._submit = h.intents.append
    h.loop._tick()
    assert len(h.intents) == 1


async def test_summary_does_not_add_a_side_model_call(h: Harness) -> None:
    side = DeferredSide()
    h.loop._side = side
    h.loop.collect_opinions("选哪个")
    h.loop.finish_collection()
    h.loop._tick()
    assert len(h.intents) == 1
    assert side.calls == 0


async def test_material_and_collection_are_bounded(h: Harness) -> None:
    h.loop.collect_opinions("哪个按钮好用")
    for number in range(400):
        h.loop.note_event(_event(number + 1, f"第{number}号意见"))
        h.loop.note_interrupted(str(number), "voice", f"第{number}次被打断的问题", "只说到这里")
    assert h.loop.status()["interrupted_candidates"] == 8
    h.loop.finish_collection()
    h.loop._tick()
    assert _body(h.intents[0]).count("[记录 ") == 256
    assert "第0号意见" not in _body(h.intents[0])
    assert "第399号意见" in _body(h.intents[0])


async def test_used_event_material_is_removed_but_new_danmaku_stays(h: Harness) -> None:
    old = _event(1, "已经用于主动话题")
    fresh = _event(2, "新的观众问题")
    h.loop.note_event(old)
    h.loop.note_event(fresh)

    h.loop._opportunities.mark_used({_event_ref(old)})

    material = h.loop._opportunities.material()
    assert "已经用于主动话题" not in material
    assert "新的观众问题" in material
    assert _event_ref(old) not in h.loop._opportunities.material_event_refs()
    assert _event_ref(fresh) in h.loop._opportunities.material_event_refs()


async def test_cancel_and_replace_revoke_submitted_summary_by_its_key(h: Harness) -> None:
    revoked: list[str] = []
    h.loop._revoke = _revoke_recorder(revoked)
    h.loop.collect_opinions("第一个话题")
    h.loop.finish_collection()
    h.loop._tick()
    first = h.intents[-1].dedup_key
    h.loop.collect_opinions("第二个话题")
    assert revoked == [first]
    h.loop.finish_collection()
    h.loop._tick()
    second = h.intents[-1].dedup_key
    h.loop.cancel_collection()
    assert revoked == [first, second]
    await h.loop.reset_for_replay("新测试")
    assert revoked == [first, second]


async def test_handled_part_revokes_summary_but_keeps_other_window_feedback(h: Harness) -> None:
    revoked: list[str] = []
    h.loop._revoke = _revoke_recorder(revoked)
    answered, other = _event(1, "这个问题已经解决"), _event(2, "我另有一个想法")
    h.loop.collect_opinions("反馈建议")
    h.loop.note_event(answered)
    h.loop.note_event(other)
    h.loop.finish_collection()
    h.loop._tick()
    previous = h.intents[-1].dedup_key
    h.loop.discard_events({_event_ref(answered)})
    assert revoked == [previous]
    h.loop.note_verdict(
        Verdict(
            intent_id=previous, source="proactive", outcome=Outcome.CANCELLED, phase=Phase.QUEUED
        )
    )
    h.loop._tick()
    assert len(h.intents) == 2
    assert h.intents[-1].dedup_key != previous
    assert "我另有一个想法" in _body(h.intents[-1])
    assert "这个问题已经解决" not in _body(h.intents[-1])


async def test_completed_summary_is_not_recreated_after_a_handled_update(h: Harness) -> None:
    revoked: list[str] = []
    h.loop._revoke = _revoke_recorder(revoked)
    event = _event(1, "这个问题已经解决")
    h.loop.collect_opinions("反馈建议")
    h.loop.note_event(event)
    h.loop.finish_collection()
    h.loop._tick()
    key = h.intents[-1].dedup_key
    h.loop.note_verdict(
        Verdict(intent_id=key, source="proactive", outcome=Outcome.SPOKEN, phase=Phase.PLAYED)
    )
    h.loop.discard_events({_event_ref(event)})
    h.loop._tick()
    assert len(h.intents) == 1
    assert revoked == []


async def test_only_related_submitted_opportunity_is_revoked(h: Harness) -> None:
    revoked: list[str] = []
    h.loop._revoke = _revoke_recorder(revoked)
    h.loop.note_interrupted("one", "gift", "原礼物", "谢谢", event_refs=("one-ref",))
    await h.clock.advance(31)
    h.loop._tick()
    first = h.intents[-1].dedup_key
    h.loop.note_interrupted("two", "voice", "另一个话题", "说到这里", event_refs=("two-ref",))
    await h.clock.advance(31)
    h.loop._tick()
    second = h.intents[-1].dedup_key
    h.loop.discard_events({"one-ref"})
    assert revoked == [first]
    assert second not in revoked


async def test_summary_already_dispatched_is_not_rebuilt_when_revoke_fails(h: Harness) -> None:
    h.loop._revoke = lambda _key: False
    event = _event(1, "观点刚被主播处理")
    h.loop.collect_opinions("反馈建议")
    h.loop.note_event(event)
    h.loop.finish_collection()
    h.loop._tick()
    h.loop.discard_events({_event_ref(event)})
    h.loop._tick()
    assert len(h.intents) == 1
    assert not h.loop.status()["opinion_collection_pending"]


async def test_topic_material_decays_instead_of_vanishing_for_good(h: Harness) -> None:
    """mark_used used to be a one-way door: a topic that expired in the queue
    burned its material forever. Now a use is a timestamp — hard-excluded for
    a while, then back with a note that it was raised, then forgotten."""
    from bilisama.proactive_opportunities import USED_FORGET_S, USED_HARD_S

    old = _event(1, "聊过一次的观众问题")
    h.loop.note_event(old)
    h.loop._opportunities.mark_used({_event_ref(old)})
    pool = h.loop._opportunities
    assert "聊过一次的观众问题" not in pool.material()
    assert not any("聊过一次的观众问题" in line for line in pool.recent_danmaku_lines())
    assert any(
        "聊过一次的观众问题" in line for line in pool.used_event_lines()
    ), "the side model's store view hides it too"
    await h.clock.advance(USED_HARD_S + 1)
    lines = pool.recent_danmaku_lines()
    assert any(
        "聊过一次的观众问题" in line and "分钟前作为主动话题提过" in line for line in lines
    ), "back for the discussion layer, flagged as raised once"
    assert not any("聊过一次的观众问题" in line for line in pool.used_event_lines())
    await h.clock.advance(USED_FORGET_S)
    assert pool._used_age(old) is None, "forgotten"


async def test_unmarking_restores_material_a_topic_never_spoke(h: Harness) -> None:
    old = _event(1, "排队时过期的话题素材")
    h.loop.note_event(old)
    h.loop._opportunities.mark_used({_event_ref(old)})
    assert "排队时过期的话题素材" not in h.loop._opportunities.material()
    h.loop._opportunities.unmark_used({_event_ref(old)})
    assert "排队时过期的话题素材" in h.loop._opportunities.material()
    assert "分钟前作为主动话题提过" not in h.loop._opportunities.material()


async def test_an_answered_danmaku_never_returns_as_unanswered_material(h: Harness) -> None:
    """The 16:02 repeat: 小满's question was answered in the danmaku lane and
    still fed two proactive topics. Answered is permanent for this stream;
    it stays visible only to the discussion layer, marked as answered."""
    from bilisama.proactive_opportunities import USED_FORGET_S

    done = _event(1, "已经被普通车道回答的问题")
    h.loop.note_event(done)
    h.loop._opportunities.mark_answered({_event_ref(done)})
    assert "已经被普通车道回答的问题" not in h.loop._opportunities.material()
    await h.clock.advance(USED_FORGET_S * 2)
    assert "已经被普通车道回答的问题" not in h.loop._opportunities.material()
    lines = h.loop._opportunities.recent_danmaku_lines(window_s=USED_FORGET_S * 3)
    assert any("已经被普通车道回答的问题" in line and "已回答" in line for line in lines)


def test_over_capacity_her_own_cut_off_turns_go_before_a_reply_that_answered_someone(
    h: Harness,
) -> None:
    pool = h.loop._opportunities
    pool.note_interrupted(
        "danmaku:1", "danmaku", "[弹幕] 糯米：会传服务器吗", "糯米别担心", event_refs=("a",)
    )
    for n in range(9):
        pool.note_interrupted(f"voice:{n}", "voice", "主播在排查", f"太好了{n}")
    assert pool.status()["interrupted_candidates"] == 8
    material = pool.interrupted_material()
    assert "糯米" in material, "the danmaku reply outlives nine voice fragments"
    assert material.index("糯米") < material.index("太好了"), "and is listed first"
