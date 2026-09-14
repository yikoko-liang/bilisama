"""Regression contracts for model-led shared interactions."""

import asyncio
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intents import anchor_danmaku_context_item, intent_for
from bilisama.director.scheduler import Scheduler
from bilisama.ingest.bilibili.selector import DanmakuSelector
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict
from bilisama.realtime import link
from tests.unit.conftest import build_assembly_kit
from tests.unit.test_bili_selector import _thresholds
from tests.unit.test_director import _ScriptedLink
from tests.unit.test_intent_test_runner import _Rig as RunnerRig


def test_vip_welcome_survives_host_interruption() -> None:
    intent = intent_for(LiveEvent(kind=EventKind.VIP_ENTER, viewer=Viewer(uid=8)), now=1)
    assert intent is not None
    assert intent.requeue_on_interrupt
    assert not intent.injection.reply.protected


def test_danmaku_prompt_carries_target_uid_and_answer_requirement() -> None:
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        viewer=Viewer(uid=1, name="小松"),
        text="今晚开源吗",
        reply_to_uid=42,
        reply_to_name="主播",
        reply_to_anchor=True,
    )
    intent = intent_for(event, now=1)
    assert intent is not None
    assert "42" in (intent.injection.item_text or "")
    assert "@主播" in (intent.injection.item_text or "")
    assert "转交主播" in (intent.injection.reply.instructions or "")
    assert "[SKIP]" in (intent.injection.reply.instructions or "")


def test_voice_prompt_does_not_invent_visual_access() -> None:
    prompt = Path("config/personas/live/voice_addressing.md").read_text()
    assert "右下角那块偏暗" not in prompt
    assert "喊错" in prompt
    assert "后续" in prompt


def test_anchor_typed_answer_keeps_the_viewer_reply_target() -> None:
    event = LiveEvent(
        kind=EventKind.DANMAKU,
        viewer=Viewer(uid=42, name="主播", is_anchor=True),
        text="不会上传",
        reply_to_uid=101,
        reply_to_name="小松",
        reply_to_anchor=False,
    )
    item = anchor_danmaku_context_item(event)
    assert "主播弹幕" in item
    assert "UID 101" in item
    assert "@其他观众" in item
    assert "时间戳" in item


async def test_batch_preserves_short_and_cross_viewer_content_for_model() -> None:
    clock = FakeClock()
    selector = DanmakuSelector(clock, thresholds=lambda: _thresholds())
    batches: list[tuple[LiveEvent, ...]] = []

    async def batch(events: tuple[LiveEvent, ...]) -> None:
        batches.append(events)

    async def gift(event: LiveEvent) -> None:
        pytest.fail("弹幕不应该走单条发送")

    task = asyncio.create_task(selector.run(gift, deliver_batch=batch))
    try:
        for uid, text in enumerate(("好", "好", "@小松 你用哪个", "今晚开源吗"), 1):
            selector.offer(
                LiveEvent(
                    kind=EventKind.DANMAKU, viewer=Viewer(uid=uid), event_id=str(uid), text=text
                )
            )
        await clock.advance(3)
        assert len(batches) == 1
        assert [e.text for e in batches[0]] == ["好", "好", "@小松 你用哪个", "今晚开源吗"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("early", [False, True])
async def test_model_decline_is_terminal_even_for_requeueable_vip(early: bool) -> None:
    clock = FakeClock()

    class Speech(_ScriptedLink):
        async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
            handle = await super().request_reply(spec)
            if early:
                scheduler.skip_reply(handle, detail="主播已欢迎")
            return handle

    speech = Speech()
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock)
    event = LiveEvent(kind=EventKind.VIP_ENTER, viewer=Viewer(uid=7), event_id="vip")
    intent = intent_for(event, now=1)
    assert intent is not None
    await scheduler._dispatch(intent)
    handle = speech.handles[0]
    scheduler.skip_reply(handle, detail="主播已欢迎")
    scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.CANCELLED))
    await clock.advance(0)
    assert len(scheduler.verdicts) == 1
    assert scheduler.verdicts[0].reason is SkipReason.MODEL_DECLINED
    assert scheduler.verdicts[0].detail == "主播已欢迎"
    assert scheduler.status()["queued"] == 0
    assert speech.cancels == [handle]
    assert not any(role == "assistant" for _, role in speech.history)


@pytest.mark.parametrize("generation_done", [False, True])
async def test_vip_interrupted_during_generation_or_playback_requeues(
    generation_done: bool,
) -> None:
    clock = FakeClock()
    speech = _ScriptedLink()
    floor = SpeakingFloor(clock)
    scheduler = Scheduler(speech, floor, clock)
    intent = intent_for(LiveEvent(kind=EventKind.VIP_ENTER, viewer=Viewer(uid=7)), now=1)
    assert intent is not None
    await scheduler._dispatch(intent)
    handle = speech.handles[0]
    floor.on_playback(True)
    if generation_done:
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="晚风来啦"))
    scheduler._handle_event(link.SpeechStarted())
    if not generation_done:
        scheduler._handle_event(link.ReplyDone(handle, link.ReplyStatus.CANCELLED))
    floor.on_playback(False)
    scheduler._flush_played()
    assert scheduler.status()["queued"] == 1
    assert scheduler.verdicts == []
    scheduler.panic_mute()
    await scheduler.drain_pending_io()
    assert scheduler.status()["queued"] == 0


async def test_unselected_events_reach_shared_observation_but_not_a_new_reply(
    tmp_path: Path,
) -> None:
    observed: list[str] = []

    async def write(text: str) -> None:
        observed.append(text)

    from bilisama.config.schema import SpeakSwitches

    kit = build_assembly_kit(
        tmp_path, observe_context_item=write, speak=SpeakSwitches(danmaku=False)
    )
    for uid in range(40):
        await kit.assembly.on_event(
            LiveEvent(
                kind=EventKind.DANMAKU,
                viewer=Viewer(uid=uid + 1, name=f"观众{uid}"),
                text=f"问题{uid}",
                event_id=str(uid),
            )
        )
    await kit.assembly.flush_event_observations()
    assert len(observed) == 1
    assert "问题39" in observed[0] and "问题0\n" not in observed[0]
    assert "不要求回复" in observed[0]
    assert not kit.intents
    await kit.assembly.flush_event_observations()
    assert len(observed) == 1
    await kit.assembly.on_event(
        LiveEvent(kind=EventKind.DANMAKU, text="上一例遗留", event_id="old")
    )
    kit.assembly.set_replay_context("新测试")
    await kit.assembly.flush_event_observations()
    assert len(observed) == 1


async def test_observation_failure_keeps_latest_events_for_retry(tmp_path: Path) -> None:
    async def fail(text: str) -> None:
        raise ConnectionError("断开")

    kit = build_assembly_kit(tmp_path, observe_context_item=fail)
    await kit.assembly.on_event(LiveEvent(kind=EventKind.DANMAKU, text="问题", event_id="new"))
    with pytest.raises(ConnectionError):
        await kit.assembly.flush_event_observations()
    assert len(kit.assembly._pending_observations) == 1
    kit.assembly.set_event_input_enabled(False)
    assert not kit.assembly._pending_observations


async def test_vip_greeted_only_after_playback_receipt(tmp_path: Path) -> None:
    kit = build_assembly_kit(tmp_path)
    event = LiveEvent(kind=EventKind.VIP_ENTER, viewer=Viewer(uid=7), event_id="vip")
    await kit.assembly.on_event(event)
    assert not kit.assembly._vip_greeted
    assert kit.assembly._vip_pending
    kit.assembly.note_verdict(
        Verdict(
            intent_id=event.dedup_key,
            source="vip_enter",
            outcome=Outcome.SPOKEN,
            phase=Phase.PLAYED,
        )
    )
    assert "uid:7" in kit.assembly._vip_greeted
    assert not kit.assembly._vip_pending


@pytest.mark.parametrize("early", [False, True])
async def test_model_skip_closes_test_receipt_without_audio(early: bool) -> None:
    rig = RunnerRig()
    await rig.start()
    try:
        handle = link.ReplyHandle()
        if early:
            rig.runner.note_model_skip(handle)
        rig.runner.observe(link.ReplyStarted(handle))
        rig.runner.note_model_skip(handle)
        assert not rig.runner.unfinished_handles
        assert rig.rows()[0]["audio_chunks"] == 0
        assert rig.rows()[0]["reply_details"][0]["status"] == "skipped"
    finally:
        await rig.runner.stop()
