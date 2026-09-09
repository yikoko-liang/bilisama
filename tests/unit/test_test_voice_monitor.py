"""Test-voice monitoring is owner-only and independent of assistant playback."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from bilisama.ui.audio import AudioBroker
from tests.unit.test_ui_server import _TOKEN, _audio_app

_PCM = b"\x00\x20" * 512


async def test_monitor_requires_owner_callbacks_and_browser_ready_handshake() -> None:
    broker = AudioBroker()
    assert not bool(broker.monitor_ready)
    with pytest.raises(RuntimeError, match="监听"):
        broker.monitor_test_voice(_PCM)
    await broker.claim("browser", send=lambda _pcm: None)
    assert not bool(broker.monitor_ready)
    await broker.release("browser")
    monitored: list[bytes] = []
    await broker.claim(
        "browser", send=lambda _pcm: None, monitor=monitored.append, clear_monitor=lambda _seq: None
    )
    assert not bool(broker.monitor_ready)
    broker.test_voice_ready(True)
    assert bool(broker.monitor_ready)
    broker.monitor_test_voice(_PCM)
    assert monitored == [_PCM]
    broker.clear_test_voice()
    broker.test_voice_cleared(1)
    await broker.release("browser")
    assert not bool(broker.monitor_ready)
    await broker.wait_test_voice_clear()


async def test_monitor_clear_waits_for_matching_ack_without_touching_assistant() -> None:
    broker = AudioBroker()
    assistant_clears: list[bool] = []
    requested: list[int] = []
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        flush=lambda: assistant_clears.append(True),
        monitor=lambda _pcm: None,
        clear_monitor=requested.append,
    )
    broker.test_voice_ready(True)
    broker.clear_test_voice()
    waiting = asyncio.create_task(broker.wait_test_voice_clear())
    await asyncio.sleep(0)
    assert assistant_clears == []
    broker.test_voice_cleared(requested[-1] - 1)
    await asyncio.sleep(0)
    assert not waiting.done()
    broker.test_voice_cleared(requested[-1])
    await asyncio.wait_for(waiting, timeout=1)
    broker.flush()
    assert assistant_clears == [True]
    assert len(requested) == 1


async def test_unacknowledged_clear_fails_instead_of_pretending_audio_stopped() -> None:
    broker = AudioBroker()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    broker.test_voice_ready(True)
    broker.clear_test_voice()
    with pytest.raises(RuntimeError, match="监听"):
        await broker.wait_test_voice_clear()


async def test_disconnect_after_monitor_send_is_not_a_successful_clear() -> None:
    broker = AudioBroker()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    broker.test_voice_ready(True)
    broker.monitor_test_voice(_PCM)
    broker.clear_test_voice()
    waiting = asyncio.create_task(broker.wait_test_voice_clear())
    await asyncio.sleep(0)
    await broker.release("browser")
    with pytest.raises(RuntimeError, match="监听"):
        await waiting


async def test_handoff_rejects_old_socket_ready_and_clear_receipts() -> None:
    from bilisama.ui.server import _monitor_receipt

    broker = AudioBroker()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    old_generation = broker.monitor_generation
    await broker.claim(
        "shell",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    ready = json.dumps({"event": "test.voice.ready", "data": {"ready": True, "version": 1}})
    assert _monitor_receipt(ready, broker, old_generation)
    assert not bool(broker.monitor_ready)
    assert _monitor_receipt(ready, broker, broker.monitor_generation)
    assert bool(broker.monitor_ready)
    broker.clear_test_voice()
    waiting = asyncio.create_task(broker.wait_test_voice_clear())
    await asyncio.sleep(0)
    clear = json.dumps({"event": "test.voice.cleared", "data": {"seq": 1}})
    _monitor_receipt(clear, broker, old_generation)
    await asyncio.sleep(0)
    assert not waiting.done()
    _monitor_receipt(clear, broker, broker.monitor_generation)
    await waiting


async def test_invalid_or_unplayable_handshakes_never_enable_monitor() -> None:
    from bilisama.ui.server import _monitor_receipt

    broker = AudioBroker()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    assert not _monitor_receipt('{"event": []}', broker, broker.monitor_generation)
    for data in (
        {"ready": True},
        {"version": True, "ready": True},
        {"version": 1, "ready": "true"},
        {"version": 1, "ready": False, "error": "请点击音频页面"},
    ):
        _monitor_receipt(
            json.dumps({"event": "test.voice.ready", "data": data}),
            broker,
            broker.monitor_generation,
        )
        assert not broker.monitor_ready
        with pytest.raises(RuntimeError, match="监听"):
            broker.monitor_test_voice(_PCM)


async def test_explicit_recovery_requires_current_page_ack_before_new_monitoring() -> None:
    broker = AudioBroker()
    requested: list[int] = []
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=requested.append,
    )
    broker.test_voice_ready(True)
    broker.monitor_test_voice(_PCM)
    await broker.release("browser")
    with pytest.raises(RuntimeError, match="监听"):
        await broker.recover_test_voice_monitor()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=requested.append,
    )
    broker.test_voice_ready(True)
    with pytest.raises(RuntimeError, match="监听"):
        broker.monitor_test_voice(_PCM)
    recovery = asyncio.create_task(broker.recover_test_voice_monitor())
    await asyncio.sleep(0)
    assert requested
    assert not recovery.done()
    broker.test_voice_cleared(requested[-1])
    await recovery
    broker.monitor_test_voice(_PCM)


async def test_recovery_timeout_keeps_old_monitor_uncertainty() -> None:
    broker = AudioBroker()
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    broker.test_voice_ready(True)
    broker.monitor_test_voice(_PCM)
    await broker.release("browser")
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=lambda _seq: None,
    )
    broker.test_voice_ready(True)
    with pytest.raises(RuntimeError, match="监听"):
        await broker.recover_test_voice_monitor()
    with pytest.raises(RuntimeError, match="监听"):
        broker.monitor_test_voice(_PCM)


async def test_recovery_without_a_monitor_failure_is_a_noop() -> None:
    broker = AudioBroker()
    await broker.recover_test_voice_monitor()
    requested: list[int] = []
    await broker.claim(
        "browser",
        send=lambda _pcm: None,
        monitor=lambda _pcm: None,
        clear_monitor=requested.append,
    )
    broker.test_voice_ready(True)
    await broker.recover_test_voice_monitor()
    assert requested == []


def test_audio_socket_uses_dedicated_monitor_messages_and_ack() -> None:
    broker = AudioBroker()
    client, heard = _audio_app(broker)
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell") as ws:
        assert not broker.monitor_ready
        ws.send_json({"event": "test.voice.ready", "data": {"ready": True, "version": 1}})
        # Uplink handling follows the ready frame on this same ordered socket.
        ws.send_bytes(b"\x01\x02")
        deadline = time.monotonic() + 2
        while b"\x01\x02" not in heard and time.monotonic() < deadline:
            time.sleep(0.001)
        assert b"\x01\x02" in heard
        broker.play(b"\x01\x02")
        assert ws.receive_bytes() == b"\x01\x02"
        broker.monitor_test_voice(_PCM)
        message = ws.receive_json()
        assert message["event"] == "test.voice"
        assert message["data"]["sample_rate"] == 16000
        assert base64.b64decode(message["data"]["pcm"]) == _PCM
        broker.clear_test_voice()
        clear = ws.receive_json()
        assert clear["event"] == "test.voice.clear"
        ws.send_json({"event": "test.voice.cleared", "data": clear["data"]})
    assert not broker.monitor_ready


async def test_independent_queues_keep_the_other_lane_on_clear_and_bound_monitor() -> None:
    from bilisama.ui.server import _AUDIO_QUEUE, _AudioOutbound

    queue = _AudioOutbound()
    queue.send(b"\x11\x11")
    queue.monitor(_PCM)
    queue.flush()
    message = await queue.get()
    assert isinstance(message, str) and json.loads(message)["event"] == "test.voice"
    clear = await queue.get()
    assert isinstance(clear, str) and json.loads(clear)["event"] == "playback.clear"
    queue.monitor(_PCM)
    queue.send(b"\x22\x22")
    queue.clear_monitor(7)
    assert await queue.get() == b"\x22\x22"
    clear = await queue.get()
    assert isinstance(clear, str) and json.loads(clear)["data"]["seq"] == 7
    for _ in range(_AUDIO_QUEUE):
        queue.send(b"\x33\x33")
        queue.monitor(_PCM)
    with pytest.raises(RuntimeError, match="监听"):
        queue.monitor(_PCM)
    queue.close()
    assert await queue.get() is None
