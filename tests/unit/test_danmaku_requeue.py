"""Ordinary replies may retry once without acquiring the paid event lifetime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from bilisama.director.intent import Intent, Priority
from bilisama.director.intents import intent_for
from bilisama.director.scheduler import Scheduler
from bilisama.obs.outcome import Outcome
from bilisama.realtime.link import ReplyDone, ReplyHandle, ReplyStatus, SpeechStarted, SpeechStopped
from tests.fakes.bili import danmaku_event
from tests.unit.test_director import _intent, _ScriptedLink, _until
from tests.unit.test_scheduler_playback import _generated, _running


def _reply(*, now: float = 10.0) -> Intent:
    event = replace(danmaku_event("今天玩什么游戏？"), recv_at=8.0)
    intent = intent_for(event, now=now, requeue_danmaku=True)
    assert intent is not None
    return intent


def _stop_speaking(scheduler: Scheduler) -> None:
    scheduler._handle_event(SpeechStopped(audio_ms=0))
    scheduler._floor.on_speech_stopped(quiet_s=0)
    scheduler.notify()


async def _cut(
    scheduler: Scheduler,
    speech: _ScriptedLink,
    handle: ReplyHandle,
    stage: str,
) -> None:
    scheduler.playback_queued(handle.handle_id, audio_s=4)
    scheduler.playback_started(handle.handle_id)
    if stage == "playback":
        await _generated(scheduler, speech, handle)
    if stage == "server_cancel":
        scheduler._handle_event(ReplyDone(handle, ReplyStatus.CANCELLED))
        scheduler._handle_event(SpeechStarted(audio_ms=0))
    else:
        scheduler._handle_event(SpeechStarted(audio_ms=0))
    scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
    if stage == "generating":
        scheduler._handle_event(ReplyDone(handle, ReplyStatus.CANCELLED))
    # Both player and server may repeat their terminal callbacks.
    scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
    scheduler._handle_event(ReplyDone(handle, ReplyStatus.CANCELLED))


@pytest.mark.parametrize("stage", ["generating", "playback", "server_cancel"])
async def test_danmaku_interruption_retries_once_in_each_phase(stage: str) -> None:
    async with _running() as (scheduler, speech, _clock):
        intent = _reply()
        scheduler.submit(intent)
        await _until(lambda: scheduler._active is not None, "弹幕没有派发")
        first = speech.handles[-1]
        with patch("random.random", side_effect=AssertionError("重排不能重新抽取概率")):
            await _cut(scheduler, speech, first, stage)
            assert scheduler.status()["queued"] == 1
            assert not scheduler.verdicts
            _stop_speaking(scheduler)
            await _until(lambda: len(speech.handles) == 2, "弹幕没有补说")
            active = scheduler._active
            assert active is not None
            assert active.intent.interrupt_retries_left == 0
            assert active.intent.expires_at == 28.0
            assert active.intent.event is intent.event
            assert active.intent.dedup_key == intent.dedup_key
            assert len([text for text, role in speech.history if role == "user"]) == 1
            await _cut(scheduler, speech, speech.handles[-1], stage)
        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 1
        assert scheduler.verdicts[0].outcome is Outcome.CANCELLED
        _stop_speaking(scheduler)
        await asyncio.sleep(0)
        assert len(speech.handles) == 2


async def test_interrupted_danmaku_expires_at_its_original_deadline() -> None:
    async with _running() as (scheduler, speech, clock):
        scheduler.submit(_reply())
        await _until(lambda: scheduler._active is not None, "弹幕没有派发")
        await _cut(scheduler, speech, speech.handles[-1], "playback")
        await clock.advance(19)
        _stop_speaking(scheduler)
        await _until(lambda: bool(scheduler.verdicts), "旧弹幕没有过期")
        assert scheduler.status()["queued"] == 0
        assert len(speech.handles) == 1
        assert scheduler.verdicts[0].outcome in {Outcome.EXPIRED, Outcome.SKIPPED}


async def test_preemption_spends_the_same_single_danmaku_retry() -> None:
    async with _running() as (scheduler, speech, _clock):
        scheduler.submit(_reply())
        await _until(lambda: scheduler._active is not None, "弹幕没有派发")
        first = speech.handles[-1]
        scheduler.submit(_intent("super_chat", Priority.SUPERCHAT, dedup="sc"))
        await _until(lambda: len(speech.handles) == 2, "SC 没有抢占")
        scheduler._handle_event(ReplyDone(first, ReplyStatus.CANCELLED))
        scheduler._handle_event(ReplyDone(speech.handles[-1], ReplyStatus.COMPLETED))
        await _until(lambda: len(speech.handles) == 3, "抢占后没有补说弹幕")
        assert scheduler._active is not None
        assert scheduler._active.intent.interrupt_retries_left == 0
        await _cut(scheduler, speech, speech.handles[-1], "playback")
        assert scheduler.status()["queued"] == 0
        assert len([v for v in scheduler.verdicts if v.source == "danmaku"]) == 1


async def test_dispatch_failure_preserves_danmaku_retry_and_original_expiry() -> None:
    async with _running() as (scheduler, speech, clock):
        speech._item_failures = 1
        scheduler.submit(_reply())
        await _until(lambda: speech.item_attempts == 1, "没有尝试写入")
        await clock.advance(1.1)
        await _until(lambda: scheduler._active is not None, "写入失败后没有重试")
        assert scheduler._active is not None
        assert scheduler._active.intent.interrupt_retries_left == 1
        assert scheduler._active.intent.expires_at == 28.0
        await _cut(scheduler, speech, speech.handles[-1], "playback")
        _stop_speaking(scheduler)
        await _until(lambda: len(speech.handles) == 2, "派发失败不应消耗补说机会")
        assert scheduler._active is not None
        assert scheduler._active.intent.interrupt_retries_left == 0


@pytest.mark.parametrize("requeue", [False, True])
async def test_danmaku_protection_starts_at_playback_and_requeue_stays_independent(
    requeue: bool,
) -> None:
    async with _running() as (scheduler, speech, clock):
        intent = intent_for(
            danmaku_event("今天玩什么游戏？"),
            now=10.0,
            protect_danmaku=True,
            danmaku_protect_ms=2000,
            requeue_danmaku=requeue,
        )
        assert intent is not None
        scheduler.submit(intent)
        await _until(lambda: scheduler._active is not None, "弹幕没有派发")
        handle = speech.handles[-1]
        await clock.advance(4)
        scheduler.playback_queued(handle.handle_id, audio_s=5)
        scheduler.playback_started(handle.handle_id)
        await _generated(scheduler, speech, handle)
        scheduler._handle_event(SpeechStarted(audio_ms=0))
        assert scheduler.controls.empty()
        await clock.advance(1.9)
        assert scheduler.controls.empty()
        await clock.advance(0.2)
        assert scheduler.controls.get_nowait().reply_ids == (handle.handle_id,)
        scheduler.playback_finished(handle.handle_id, interrupted=True, reason="barge_in")
        assert scheduler.status()["queued"] == int(requeue)


async def test_repeated_dispatch_failures_cannot_renew_danmaku_expiry() -> None:
    async with _running() as (scheduler, speech, clock):
        speech._item_failures = 100
        scheduler.submit(_reply())
        await _until(lambda: speech.item_attempts >= 1, "没有尝试派发")
        await clock.advance(19)
        await _until(lambda: bool(scheduler.verdicts), "不断失败的弹幕没有过期")
        assert scheduler.verdicts[0].outcome is Outcome.EXPIRED
        assert not speech.handles
        assert scheduler.status()["queued"] == 0
