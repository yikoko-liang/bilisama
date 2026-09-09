"""Live input stays protected until the test monitor acknowledges silence."""

from __future__ import annotations

import asyncio

import pytest

from bilisama.ui import test_audio
from bilisama.ui.audio import AudioBroker


async def test_monitor_cleanup_waits_for_clear_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = AudioBroker()
    calls: list[str] = []
    entered = asyncio.Event()
    acknowledged = asyncio.Event()

    async def wait(_self: AudioBroker) -> None:
        entered.set()
        await acknowledged.wait()
        calls.append("ack")

    monkeypatch.setattr(
        AudioBroker, "clear_test_voice", lambda self: calls.append("clear"), raising=False
    )
    monkeypatch.setattr(AudioBroker, "wait_test_voice_clear", wait, raising=False)
    task = asyncio.create_task(
        test_audio.stop_test_voice_monitor(broker, lambda: calls.append("pause"))
    )
    await entered.wait()
    assert calls == ["clear"] and not task.done()
    acknowledged.set()
    await task
    assert calls == ["clear", "ack"]


@pytest.mark.parametrize("stage", ["clear", "ack"])
async def test_monitor_cleanup_failure_preserves_input_protection(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    broker = AudioBroker()
    paused: list[bool] = []

    def clear(_self: AudioBroker) -> None:
        if stage == "clear":
            raise RuntimeError("监听已断开")

    async def wait(_self: AudioBroker) -> None:
        raise RuntimeError("停止回执超时")

    monkeypatch.setattr(AudioBroker, "clear_test_voice", clear, raising=False)
    monkeypatch.setattr(AudioBroker, "wait_test_voice_clear", wait, raising=False)
    with pytest.raises(RuntimeError):
        await test_audio.stop_test_voice_monitor(broker, lambda: paused.append(True))
    assert paused == [True]


async def test_monitor_cleanup_cancel_still_protects_input(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = AudioBroker()
    entered = asyncio.Event()
    paused: list[bool] = []

    async def wait(_self: AudioBroker) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AudioBroker, "clear_test_voice", lambda self: None, raising=False)
    monkeypatch.setattr(AudioBroker, "wait_test_voice_clear", wait, raising=False)
    task = asyncio.create_task(
        test_audio.stop_test_voice_monitor(broker, lambda: paused.append(True))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paused == [True]


async def test_monitor_cleanup_without_ui_needs_no_receipt() -> None:
    paused: list[bool] = []
    await test_audio.stop_test_voice_monitor(None, lambda: paused.append(True))
    assert paused == []
