"""Reply-owned playback receipts keep paid work alive after generation ends."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from bilisama.clock import FakeClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Priority
from bilisama.director.intents import intent_for
from bilisama.director.scheduler import Scheduler
from bilisama.obs.outcome import Outcome, Phase
from bilisama.realtime.link import (
    LinkDown,
    ReplyAudioDelta,
    ReplyDone,
    ReplyHandle,
    ReplyStatus,
    SpeechStarted,
    SpeechStopped,
)
from tests.fakes.bili import gift_event
from tests.unit.test_director import _intent, _protected_intent, _ScriptedLink, _until


@contextlib.asynccontextmanager
async def _running() -> AsyncIterator[tuple[Scheduler, _ScriptedLink, FakeClock]]:
    clock = FakeClock(start=10.0)
    speech = _ScriptedLink()
    scheduler = Scheduler(speech, SpeakingFloor(clock), clock, playback_receipts=True)
    runner = asyncio.create_task(scheduler.run())
    try:
        yield scheduler, speech, clock
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        for task in tuple(scheduler._tasks):
            task.cancel()
        await asyncio.gather(*scheduler._tasks, return_exceptions=True)


async def _dispatch(
    scheduler: Scheduler, speech: _ScriptedLink, *, protected: bool = True
) -> ReplyHandle:
    scheduler.submit(
        _protected_intent("sc", protect_ms=4000)
        if protected
        else _intent("super_chat", dedup="sc", requeue=True)
    )
    await _until(lambda: scheduler._active is not None, "没有派发 SC")
    return speech.handles[-1]


async def _generated(scheduler: Scheduler, speech: _ScriptedLink, handle: ReplyHandle) -> None:
    speech.feed.put_nowait(ReplyDone(handle, ReplyStatus.COMPLETED, text="谢谢你来支持"))
    await _until(lambda: scheduler._active is None, "生成槽没有释放")


async def test_protection_starts_at_playback_and_survives_generation_done() -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        await clock.advance(6.0)
        scheduler.playback_queued(handle.handle_id, audio_s=10.0)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty(), "等待首音也不能把已保护的回复切掉"
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        await clock.advance(3.9)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        assert not scheduler.verdicts
        assert not speech.cancels
        await clock.advance(0.2)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        clear = scheduler.controls.get_nowait()
        assert clear.reply_ids == (handle.handle_id,)
        assert clear.reason == "barge_in"
        assert not speech.cancels, "生成完的旧句柄不再发 cancel"


async def test_generation_before_first_playback_receipt_keeps_protection() -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id, audio_s=10)
        await _generated(scheduler, speech, handle)
        await clock.advance(5)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        scheduler.playback_started(handle.handle_id)
        await clock.advance(3)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        await _until(lambda: bool(scheduler.verdicts), "播放结束没结算")
        assert scheduler.verdicts[-1].phase is Phase.PLAYED


async def test_paid_cut_after_generation_requeues_once_and_ignores_old_receipts() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech, protected=False)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        assert scheduler.status()["queued"] == 1
        assert not speech.cancels
        assert not scheduler.verdicts
        assert len(speech.items) == 1


async def test_played_receipt_before_generation_done_waits_and_settles_once() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id)
        scheduler.playback_started(handle.handle_id)
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        assert not scheduler.verdicts
        await _generated(scheduler, speech, handle)
        await _until(lambda: bool(scheduler.verdicts), "两个结束信号没有汇合")
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        assert len(scheduler.verdicts) == 1
        assert scheduler.verdicts[0].phase is Phase.PLAYED


@pytest.mark.parametrize("reason", ["panic_mute", "output_disabled", "link_down", "handoff"])
async def test_non_barge_playback_cut_does_not_resurrect_paid_work(reason: str) -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason=reason)
        await _until(lambda: bool(scheduler.verdicts), "被主动停止的回复没有结算")
        assert scheduler.status()["queued"] == 0
        assert scheduler.verdicts[0].outcome is Outcome.CANCELLED


async def test_panic_retires_parked_reply_before_late_barge_receipt() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler.panic_mute()
        scheduler.release_panic()
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 1


async def test_link_down_retires_parked_reply_without_requeue() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler._handle_event(LinkDown(reason="连接断开"))
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 1


async def test_long_audio_does_not_settle_on_old_fifteen_second_grace() -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id, audio_s=40)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        await clock.advance(20)
        assert not scheduler.verdicts
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        await _until(lambda: bool(scheduler.verdicts), "长音频播完没结算")
        assert scheduler.verdicts[-1].phase is Phase.PLAYED


async def test_missing_first_playback_receipt_has_a_finite_wait() -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        await _generated(scheduler, speech, handle)
        await clock.advance(16)
        await _until(lambda: bool(scheduler.verdicts), "没有首音回执时不能一直占着付费回复")
        assert scheduler.verdicts[0].phase is Phase.GENERATING
        scheduler.playback_started(handle.handle_id)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 0


async def test_implicit_audio_still_clears_on_speech_without_cancelling_generation() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = ReplyHandle(implicit=True)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_started(handle.handle_id)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        clear = scheduler.controls.get_nowait()
        assert clear.reply_ids == (handle.handle_id,)
        assert not speech.cancels


async def test_zero_millisecond_protection_does_not_block_before_first_audio() -> None:
    async with _running() as (scheduler, speech, _clock):
        scheduler.submit(_protected_intent("sc", protect_ms=0))
        await _until(lambda: scheduler._active is not None, "没有派发 SC")
        handle = speech.handles[-1]
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.get_nowait().reply_ids == (handle.handle_id,)


async def test_protection_cap_cuts_if_streamer_is_still_speaking() -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id, audio_s=10)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        await clock.advance(4.1)
        assert scheduler.controls.get_nowait().reply_ids == (handle.handle_id,)
        assert not speech.cancels


async def test_releasing_panic_before_generation_cancel_cannot_requeue_paid_reply() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id)
        scheduler.playback_started(handle.handle_id)
        scheduler._floor.on_speech_started()
        scheduler.panic_mute()
        scheduler.release_panic()
        speech.feed.put_nowait(ReplyDone(handle, ReplyStatus.CANCELLED))
        await _until(lambda: scheduler._active is None, "取消未结算")
        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 1


async def test_audio_and_done_can_beat_the_player_fanout() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler._handle_event(ReplyAudioDelta(handle, b"\x01\x00" * 240))
        await _generated(scheduler, speech, handle)
        assert not scheduler.verdicts
        scheduler.playback_queued(handle.handle_id, audio_s=0.01)
        scheduler.playback_started(handle.handle_id)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        assert scheduler.verdicts[0].phase is Phase.PLAYED


async def test_old_attempt_receipts_do_not_finish_or_cut_its_retry() -> None:
    async with _running() as (scheduler, speech, _clock):
        old = await _dispatch(scheduler, speech, protected=False)
        scheduler.playback_queued(old.handle_id, audio_s=5)
        scheduler.playback_started(old.handle_id)
        await _generated(scheduler, speech, old)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        scheduler.controls.get_nowait()
        scheduler.playback_finished(old.handle_id, interrupted=True, reason="barge_in")
        scheduler._handle_event(SpeechStopped(audio_ms=0))
        scheduler._floor.on_speech_stopped(quiet_s=0)
        scheduler.notify()
        await _until(lambda: len(speech.handles) == 2, "付费回复未重新派发")
        new = speech.handles[-1]
        scheduler.playback_queued(new.handle_id, audio_s=5)
        scheduler.playback_started(new.handle_id)
        await _generated(scheduler, speech, new)
        scheduler.playback_queued(old.handle_id, audio_s=5)
        scheduler.playback_started(old.handle_id)
        scheduler.playback_finished(old.handle_id, interrupted=True, reason="barge_in")
        scheduler.playback_finished(old.handle_id, interrupted=False)
        assert not scheduler.verdicts
        assert scheduler.controls.empty()
        scheduler.playback_finished(new.handle_id, interrupted=False)
        assert len(scheduler.verdicts) == 1
        assert scheduler.verdicts[0].phase is Phase.PLAYED
        assert len(speech.items) == 1, "重试不重复写观众事件历史"


async def test_generation_cancel_stays_cancelled_if_partial_audio_reports_ended() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech, protected=False)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_started(handle.handle_id)
        scheduler._floor.on_speech_started()
        scheduler.playback_finished(handle.handle_id, interrupted=False)
        speech.feed.put_nowait(ReplyDone(handle, ReplyStatus.CANCELLED))
        await _until(lambda: scheduler._active is None, "服务端取消没有结算")
        assert scheduler.status()["queued"] == 1
        assert not scheduler.verdicts


async def test_duplicate_sc_is_still_deduplicated_while_its_audio_is_playing() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        await _generated(scheduler, speech, handle)
        scheduler.submit(_protected_intent("sc", protect_ms=4000))
        assert scheduler.status()["queued"] == 0
        assert scheduler.verdicts[-1].outcome is Outcome.SKIPPED
        assert len(speech.handles) == 1


async def test_clear_before_player_sees_first_pcm_still_requeues_paid_once() -> None:
    async with _running() as (scheduler, speech, _clock):
        handle = await _dispatch(scheduler, speech, protected=False)
        scheduler._handle_event(ReplyAudioDelta(handle, b"\x01\x00" * 240))
        await _generated(scheduler, speech, handle)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.get_nowait().reply_ids == (handle.handle_id,)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 1
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert handle.stale, "生成结束后的迟到音频也必须被丢掉"
        assert scheduler.status()["queued"] == 1
        assert not speech.cancels
        assert not scheduler.verdicts


@pytest.mark.parametrize("stop", ["panic", "timeout", "link_down"])
async def test_retiring_parked_attempt_drops_pcm_waiting_in_another_consumer(stop: str) -> None:
    async with _running() as (scheduler, speech, clock):
        handle = await _dispatch(scheduler, speech)
        scheduler._handle_event(ReplyAudioDelta(handle, b"\x01\x00" * 240))
        await _generated(scheduler, speech, handle)
        if stop == "panic":
            scheduler.panic_mute()
        elif stop == "link_down":
            scheduler._handle_event(LinkDown(reason="连接断开"))
        else:
            await clock.advance(16)
            await _until(lambda: bool(scheduler.verdicts), "等待播放超时未结算")
        assert handle.stale, "另一消费者尚未处理的 PCM 不能在结算后重新响起"
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 1


@pytest.mark.parametrize("protect_paid", [False, True])
@pytest.mark.parametrize("generation_finished", [False, True])
async def test_small_paid_gift_requeues_without_protection_after_a_long_interruption(
    protect_paid: bool, generation_finished: bool
) -> None:
    """A low-tier paid thank-you survives both cut paths and the old 20s TTL."""
    async with _running() as (scheduler, speech, clock):
        event = gift_event(coin=100)
        intent = intent_for(event, now=clock.monotonic(), protect_paid=protect_paid)
        assert intent is not None
        assert intent.priority is Priority.DANMAKU
        assert not intent.injection.reply.protected
        scheduler.submit(intent)
        await _until(lambda: scheduler._active is not None, "小额礼物没有派发")
        handle = speech.handles[-1]
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_started(handle.handle_id)
        if generation_finished:
            await _generated(scheduler, speech, handle)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.get_nowait().reply_ids == (handle.handle_id,)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        if not generation_finished:
            speech.feed.put_nowait(ReplyDone(handle, ReplyStatus.CANCELLED))
            await _until(lambda: scheduler._active is None, "生成取消没有结算")
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == 1
        await clock.advance(25)
        scheduler._handle_event(SpeechStopped(audio_ms=0))
        scheduler._floor.on_speech_stopped(quiet_s=0)
        scheduler.notify()
        await _until(lambda: len(speech.handles) == 2, "小额付费礼物被打断后未补说")
        assert scheduler._active is not None
        assert scheduler._active.intent.event is event
        assert scheduler._active.intent.priority is Priority.DANMAKU
        assert not scheduler._active.intent.injection.reply.protected
        assert scheduler._active.intent.expires_at is None
