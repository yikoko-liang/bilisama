"""UiHub: delivery rules, replay, state arbitration, thread-safe logs.

The load-bearing promises: a wedged client can never block the loop
(drop-oldest), a late client is never blank or stale (sticky + rings), and a
log line from a PortAudio thread arrives without touching asyncio off-loop.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from bilisama.clock import FakeClock
from bilisama.obs.logging import get_logger, setup
from bilisama.ui.events import ServerEvent
from bilisama.ui.hub import UiHub, VoiceSignals, resolve_voice_state


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """setup() clears root handlers; put pytest's own back afterwards."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def _payload(line: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(line)
    return payload


def _drain(queue: asyncio.Queue[str | None]) -> list[str]:
    items: list[str] = []
    while not queue.empty():
        item = queue.get_nowait()
        assert item is not None
        items.append(item)
    return items


# ------------------------------------------------------------ arbitration


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (VoiceSignals(False, False, False, False, False), "idle"),
        (VoiceSignals(True, False, False, False, False), "listening"),
        # The streamer talking wins over everything: barge-in feedback.
        (VoiceSignals(True, True, True, True, True), "listening"),
        (VoiceSignals(False, False, False, False, True), "speaking"),
        # Sound beats "in flight": the tail of a reply is still speaking.
        (VoiceSignals(False, True, True, False, True), "speaking"),
        (VoiceSignals(False, True, False, False, False), "thinking"),
        (VoiceSignals(False, False, True, False, False), "thinking"),
        (VoiceSignals(False, False, False, True, False), "thinking"),
    ],
)
def test_resolve_voice_state(signals: VoiceSignals, expected: str) -> None:
    assert resolve_voice_state(signals) == expected


# ------------------------------------------------------------ delivery


async def test_broadcast_reaches_an_attached_client_with_a_timestamp() -> None:
    hub = UiHub(FakeClock())
    replay, queue = hub.attach()
    assert replay == []
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "你好"})
    (line,) = _drain(queue)
    payload = _payload(line)
    assert payload["event"] == "reply.delta"
    assert payload["data"]["text"] == "你好"
    assert payload["data"]["ts"].startswith("2026-01-01T")


async def test_full_queue_drops_the_oldest_frame_and_keeps_the_newest() -> None:
    hub = UiHub(FakeClock(), queue_max=3)
    _, queue = hub.attach()
    for n in range(5):
        hub.broadcast(ServerEvent.REPLY_DELTA, {"text": str(n)})
    texts = [_payload(line)["data"]["text"] for line in _drain(queue)]
    assert texts == ["2", "3", "4"]


async def test_detached_client_receives_nothing_further() -> None:
    hub = UiHub(FakeClock())
    _, queue = hub.attach()
    hub.detach(queue)
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "你好"})
    assert queue.empty()
    hub.detach(queue)  # double detach must not raise


# ------------------------------------------------------------ replay


async def test_late_client_gets_newest_sticky_state_not_the_stale_one() -> None:
    hub = UiHub(FakeClock())
    hub.broadcast(ServerEvent.VOICE_STATE, {"state": "thinking"})
    hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    hub.broadcast(ServerEvent.PANEL_STATE, {"panicked": False})
    replay, _ = hub.attach()
    states = [_payload(line) for line in replay]
    assert [p["event"] for p in states] == ["voice.state", "panel.state"]
    assert states[0]["data"]["state"] == "speaking"


async def test_replay_carries_feed_and_log_history_in_order() -> None:
    hub = UiHub(FakeClock(), feed_keep=2)
    for n in range(3):
        hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": str(n)})
    hub.broadcast(ServerEvent.LOG_LINE, {"line": "{}"})
    replay, _ = hub.attach()
    events = [_payload(line)["event"] for line in replay]
    assert events == ["event.feed", "event.feed", "log.line"]
    # feed_keep=2: the oldest feed entry aged out of the ring.
    texts = [_payload(line)["data"]["text"] for line in replay[:2]]
    assert texts == ["1", "2"]


async def test_transient_frames_are_not_replayed() -> None:
    hub = UiHub(FakeClock())
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "你好"})
    replay, _ = hub.attach()
    assert replay == []


# ------------------------------------------------------------ state loop


async def test_run_broadcasts_only_state_changes() -> None:
    clock = FakeClock()
    hub = UiHub(clock)
    signals = {"value": VoiceSignals(False, False, False, False, False)}
    _, queue = hub.attach()
    task = asyncio.create_task(hub.run(lambda: signals["value"]))
    try:
        await clock.advance(0.35)  # several idle ticks -> exactly one frame
        signals["value"] = VoiceSignals(True, False, False, False, False)
        await clock.advance(0.2)
        states = [_payload(line)["data"]["state"] for line in _drain(queue)]
        assert states == ["idle", "listening"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ------------------------------------------------------------ logs


async def test_log_line_from_another_thread_arrives_after_a_tick() -> None:
    clock = FakeClock()
    hub = UiHub(clock)
    setup(stream=io.StringIO(), extra_handlers=(hub.log_handler,))
    _, queue = hub.attach()
    worker = threading.Thread(target=lambda: get_logger("test.ui").info("audio.underrun", frames=3))
    worker.start()
    worker.join()
    assert queue.empty()  # nothing crossed to asyncio yet: staging only
    task = asyncio.create_task(hub.run(lambda: VoiceSignals(False, False, False, False, False)))
    try:
        await clock.advance(0.1)
        lines = _drain(queue)
        log_frames = [_payload(line) for line in lines if _payload(line)["event"] == "log.line"]
        assert len(log_frames) == 1
        inner = json.loads(log_frames[0]["data"]["line"])
        assert inner["event"] == "audio.underrun"
        assert inner["frames"] == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ------------------------------------------------------------ shutdown


async def test_aclose_sends_the_sentinel_and_silences_the_hub() -> None:
    hub = UiHub(FakeClock())
    _, queue = hub.attach()
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "你好"})
    await hub.aclose()
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "关门之后"})
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1] is None
    assert hub.clients == 0


async def test_aclose_reaches_a_full_queue() -> None:
    """The sentinel must land even when the client never drained a frame."""
    hub = UiHub(FakeClock(), queue_max=1)
    _, queue = hub.attach()
    hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "占满"})
    await hub.aclose()
    assert queue.get_nowait() is None
