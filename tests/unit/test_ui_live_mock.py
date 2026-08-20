"""Live-mock input switching and room preflight."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.ui.live_mock import AudioInputSwitch, LiveMockController


class _RoomSource:
    name = "bilibili"

    def __init__(self, room_id: int, *, fail: bool = False) -> None:
        self.room_id = room_id
        self.fail = fail
        self.connected = False
        self._stopped = asyncio.Event()
        self._emit: Callable[[LiveEvent], Awaitable[None]] | None = None

    async def start(self, emit: Callable[[LiveEvent], Awaitable[None]]) -> None:
        if self.fail:
            raise RuntimeError("房间不存在")
        self._emit = emit
        self.connected = True
        await self._stopped.wait()
        self.connected = False

    async def stop(self) -> None:
        self._stopped.set()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "room_id": self.room_id if self.connected else 0,
            "popularity": 123,
            "counts": {},
        }

    async def emit(self, event: LiveEvent) -> None:
        assert self._emit is not None
        await self._emit(event)


def _capture(*, audio: bool = True, video: bool = True) -> dict[str, object]:
    return {
        "video_live": video,
        "audio_live": audio,
        "source_label": "Bilibili 直播 - Chrome 标签页",
        "sample_rate": 48_000,
    }


def test_audio_input_switch_never_mixes_microphone_and_browser() -> None:
    async def run() -> None:
        chunks: list[bytes] = []

        async def sink(pcm: bytes) -> None:
            chunks.append(pcm)

        switch = AudioInputSwitch(sink)
        await switch.push_audio(b"mic-1")
        switch.use_browser(True)
        await switch.push_audio(b"mic-blocked")
        await switch.push_browser_audio(b"browser-1")
        switch.use_browser(False)
        await switch.push_browser_audio(b"browser-blocked")
        await switch.push_audio(b"mic-2")
        assert chunks == [b"mic-1", b"browser-1", b"mic-2"]

    asyncio.run(run())


def test_preflight_requires_a_live_shared_audio_track_before_room_connect() -> None:
    async def run() -> None:
        created: list[_RoomSource] = []

        def factory(room_id: int) -> _RoomSource:
            source = _RoomSource(room_id)
            created.append(source)
            return source

        async def discard(_event: LiveEvent) -> None:
            return

        switch = AudioInputSwitch(lambda _pcm: asyncio.sleep(0))
        controller = LiveMockController(
            source_factory=factory,
            event_sink=discard,
            audio=switch,
            publish_state=lambda _state: None,
            publish_event=lambda _event: None,
        )
        try:
            await controller.check(room_id=123, capture=_capture(audio=False))
            state = controller.state()
            assert state["status"] == "error"
            assert state["can_start"] is False
            assert state["checks"]["screen"]["ok"] is True
            assert state["checks"]["audio"]["ok"] is False
            assert created == []
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_room_events_are_held_until_start_and_audio_reaches_the_existing_sink() -> None:
    async def run() -> None:
        sources: list[_RoomSource] = []
        events: list[LiveEvent] = []
        previews: list[dict[str, object]] = []
        audio_chunks: list[bytes] = []
        states: list[dict[str, object]] = []

        def factory(room_id: int) -> _RoomSource:
            source = _RoomSource(room_id)
            sources.append(source)
            return source

        async def event_sink(event: LiveEvent) -> None:
            events.append(event)

        async def audio_sink(pcm: bytes) -> None:
            audio_chunks.append(pcm)

        switch = AudioInputSwitch(audio_sink)

        def browser_active() -> bool:
            return switch.browser_active

        controller = LiveMockController(
            source_factory=factory,
            event_sink=event_sink,
            audio=switch,
            publish_state=lambda state: states.append(dict(state)),
            publish_event=lambda event: previews.append(dict(event)),
            connect_timeout_s=1,
        )
        event = LiveEvent(
            kind=EventKind.DANMAKU,
            room_id=123,
            viewer=Viewer(uid=7, name="阿强"),
            text="今天测什么",
            event_id="dm:1",
        )
        try:
            await controller.check(room_id=123, capture=_capture())
            assert controller.state()["status"] == "ready"
            assert controller.state()["checks"]["room"]["ok"] is True
            assert controller.state()["real_room_id"] == 123

            await sources[0].emit(event)
            assert events == []
            assert previews == []

            await controller.start()
            assert controller.state()["status"] == "running"
            assert browser_active() is True
            await sources[0].emit(event)
            assert events == [event]
            assert previews[-1]["kind"] == "danmaku"
            assert previews[-1]["name"] == "阿强"

            pcm = b"\x01\x00\x02\x00"
            await controller.push_audio(
                {
                    "sample_rate": 16_000,
                    "pcm16_b64": base64.b64encode(pcm).decode("ascii"),
                }
            )
            assert audio_chunks == [pcm]
            assert controller.state()["audio_frames"] == 1

            await controller.stop()
            assert controller.state()["status"] == "ready"
            assert browser_active() is False
            await sources[0].emit(event)
            assert events == [event]
            assert states[-1]["can_start"] is True

            await controller.capture_stopped()
            assert controller.state()["status"] == "idle"
            assert controller.state()["can_start"] is False
            assert controller.state()["checks"]["audio"]["ok"] is False
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_start_is_refused_when_room_preflight_failed() -> None:
    async def run() -> None:
        switch = AudioInputSwitch(lambda _pcm: asyncio.sleep(0))
        controller = LiveMockController(
            source_factory=lambda room_id: _RoomSource(room_id, fail=True),
            event_sink=lambda _event: asyncio.sleep(0),
            audio=switch,
            publish_state=lambda _state: None,
            publish_event=lambda _event: None,
            connect_timeout_s=0.2,
        )
        try:
            await controller.check(room_id=404, capture=_capture())
            assert controller.state()["status"] == "error"
            await controller.start()
            assert controller.state()["status"] == "error"
            assert "状态检测" in str(controller.state()["error"])
            assert switch.browser_active is False
        finally:
            await controller.aclose()

    asyncio.run(run())
