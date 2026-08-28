"""Browser-audio and real-room orchestration for the live mock console.

The controller changes inputs, not product behaviour. Browser PCM still
enters the existing ``SpeechLink`` — through the same AudioInputSwitch every
microphone frame passes (ui/audio.py), elected via ``use_browser`` — and room
events still enter ``Assembly.on_event``. Preflight keeps both lanes out of
the product path until the operator starts a run, which makes a green
checklist mean something observable.

One transport deviation from the yiko original: browser PCM arrives as
BINARY frames on the existing point-to-point audio socket (``?role=mock``),
not base64 on the control socket. The page reuses the shipped
capture-worklet, so what lands here is already 16 kHz mono Int16 — the same
shape the microphone path produces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from bilisama.ingest.events import LiveEvent
from bilisama.ingest.sources import EventSink
from bilisama.ui.audio import AudioInputSwitch
from bilisama.ui.events import live_event_payload

__all__ = ["LiveMockController", "RoomSource"]

StateSink = Callable[[dict[str, Any]], None]
PreviewSink = Callable[[dict[str, object]], None]

_INPUT_RATE = 16_000
_MAX_AUDIO_BYTES = _INPUT_RATE  # 500 ms of mono signed 16-bit PCM


class RoomSource(Protocol):
    """The status-bearing subset exposed by ``BilibiliEventSource``."""

    name: str

    async def start(self, emit: EventSink) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


RoomSourceFactory = Callable[[int], RoomSource]


def _event_preview(event: LiveEvent) -> dict[str, object]:
    return live_event_payload(event)


class LiveMockController:
    """Preflight and run a browser-audio + real-room validation session."""

    def __init__(
        self,
        *,
        source_factory: RoomSourceFactory,
        event_sink: Callable[[LiveEvent], Awaitable[None]],
        audio: AudioInputSwitch,
        publish_state: StateSink,
        publish_event: PreviewSink,
        connect_timeout_s: float = 12.0,
        enabled: bool = True,
        disabled_reason: str = "",
    ) -> None:
        self._source_factory = source_factory
        self._event_sink = event_sink
        self._audio = audio
        self._publish_state = publish_state
        self._publish_event = publish_event
        self._connect_timeout_s = connect_timeout_s
        self._enabled = enabled
        self._disabled_reason = disabled_reason
        self._lock = asyncio.Lock()
        self._source: RoomSource | None = None
        self._source_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._status = "idle"
        self._error = disabled_reason if not enabled else ""
        self._room_error = ""
        self._room_id = 0
        self._capture: dict[str, object] = {}
        self._room_snapshot: dict[str, Any] = {}
        self._running = False
        self._closing = False
        self._events_seen = 0
        self._events_forwarded = 0
        self._audio_frames = 0
        self._audio_bytes = 0
        self._audio_dropped = 0

    def state(self) -> dict[str, Any]:
        room = self._source.status() if self._source is not None else self._room_snapshot
        room_connected = bool(room.get("connected"))
        screen_ok = bool(self._capture.get("video_live"))
        audio_ok = bool(self._capture.get("audio_live"))
        source_label = str(self._capture.get("source_label") or "未选择共享源")
        sample_rate = self._capture.get("sample_rate")
        sample_rate_hz = (
            int(sample_rate)
            if isinstance(sample_rate, int | float) and not isinstance(sample_rate, bool)
            else 0
        )
        real_room_id = int(room.get("room_id") or 0)
        ready = (
            self._enabled
            and screen_ok
            and audio_ok
            and room_connected
            and self._status in {"ready", "running"}
        )
        return {
            "enabled": self._enabled,
            "status": self._status,
            "error": self._error,
            "room_id": self._room_id,
            "real_room_id": real_room_id,
            "can_start": ready and not self._running,
            "running": self._running,
            "checks": {
                "backend": {
                    "ok": self._enabled,
                    "label": "伴播后端",
                    "detail": "realtime 与正式调度链路已连接" if self._enabled else self._error,
                },
                "screen": {
                    "ok": screen_ok,
                    "label": "共享画面",
                    "detail": source_label if screen_ok else "尚未选择浏览器标签页或窗口",
                },
                "audio": {
                    "ok": audio_ok,
                    "label": "共享音轨",
                    "detail": (
                        f"{sample_rate_hz} Hz，页面已转成 16000 Hz PCM"
                        if audio_ok
                        else "共享源没有音轨，不能替代麦克风"
                    ),
                },
                "room": {
                    "ok": room_connected,
                    "label": "真实直播间流",
                    "detail": (
                        f"已连接真实房间 {real_room_id}，人气 {int(room.get('popularity') or 0)}"
                        if room_connected
                        else self._room_error or "尚未检测房间"
                    ),
                },
            },
            "events_seen": self._events_seen,
            "events_forwarded": self._events_forwarded,
            "audio_frames": self._audio_frames,
            "audio_bytes": self._audio_bytes,
            "audio_dropped": self._audio_dropped,
            "room": dict(room),
        }

    async def check(self, *, room_id: int, capture: Mapping[str, object]) -> None:
        """Connect the room but keep its events out of Assembly until start."""
        async with self._lock:
            self._audio.use_browser(False)
            self._running = False
            self._capture = dict(capture)
            self._room_id = room_id
            self._error = ""
            self._room_error = ""
            if not self._enabled:
                self._fail(self._disabled_reason or "直播 Mock 当前不可用")
                return
            if room_id <= 0:
                self._fail("请输入大于 0 的 B 站房间号")
                return
            if not bool(capture.get("video_live")):
                self._fail("共享画面没有启动，请先选择浏览器标签页或窗口")
                return
            if not bool(capture.get("audio_live")):
                self._fail("共享源没有音轨，请勾选「共享标签页音频」后重新选择")
                return

            await self._stop_room()
            self._status = "checking"
            self._publish()
            source = self._source_factory(room_id)
            self._source = source
            self._generation += 1
            generation = self._generation
            self._source_task = asyncio.create_task(
                self._run_source(source, generation), name="live-mock:bilibili"
            )
            deadline = asyncio.get_running_loop().time() + self._connect_timeout_s
            while not bool(source.status().get("connected")):
                if self._source_task.done():
                    self._fail(self._room_error or f"房间 {room_id} 连接失败")
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    await self._stop_room()
                    self._fail(f"房间 {room_id} 在 {self._connect_timeout_s:g} 秒内没有连接成功")
                    return
                await asyncio.sleep(0.02)

            self._room_snapshot = dict(source.status())
            self._status = "ready"
            self._publish()

    async def start(self) -> None:
        async with self._lock:
            room_connected = self._source is not None and bool(
                self._source.status().get("connected")
            )
            if self._status != "ready" or not room_connected:
                self._fail("状态检测没有全部通过，不能开始直播 Mock")
                return
            self._running = True
            self._status = "running"
            self._audio.use_browser(True)
            self._publish()

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            self._audio.use_browser(False)
            connected = self._source is not None and bool(self._source.status().get("connected"))
            self._status = "ready" if connected else "idle"
            self._publish()

    async def capture_stopped(self) -> None:
        """Invalidate preflight when the browser or operator ends sharing."""
        async with self._lock:
            self._running = False
            self._audio.use_browser(False)
            self._capture = {
                **self._capture,
                "video_live": False,
                "audio_live": False,
                "source_label": "未选择共享源",
            }
            self._status = "idle"
            self._error = "共享已停止，请重新选择共享源并检测状态"
            self._publish()

    async def push_audio(self, pcm: bytes) -> None:
        """One binary browser frame off the ?role=mock socket.

        Malformed or out-of-run frames are accounted, never raised — the
        socket handler must not die over one bad frame.
        """
        if not self._running or self._status != "running":
            self._audio_dropped += 1
            return
        if not pcm or len(pcm) % 2 or len(pcm) > _MAX_AUDIO_BYTES:
            self._audio_dropped += 1
            return
        await self._audio.push_browser_audio(pcm)
        self._audio_frames += 1
        self._audio_bytes += len(pcm)
        if self._audio_frames == 1 or self._audio_frames % 50 == 0:
            self._publish()

    async def aclose(self) -> None:
        async with self._lock:
            self._closing = True
            self._running = False
            self._audio.use_browser(False)
            await self._stop_room()
            self._status = "idle"

    async def _run_source(self, source: RoomSource, generation: int) -> None:
        try:
            await source.start(self._on_event)
            detail = "直播间连接已结束"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
        if generation != self._generation or self._closing:
            return
        self._room_error = detail
        self._error = detail
        self._running = False
        self._audio.use_browser(False)
        self._status = "error"
        self._room_snapshot = dict(source.status())
        self._publish()

    async def _on_event(self, event: LiveEvent) -> None:
        self._events_seen += 1
        if not self._running:
            return
        await self._event_sink(event)
        self._events_forwarded += 1
        self._publish_event(_event_preview(event))
        if self._events_forwarded == 1 or self._events_forwarded % 10 == 0:
            self._publish()

    async def _stop_room(self) -> None:
        source = self._source
        task = self._source_task
        self._generation += 1
        self._source = None
        self._source_task = None
        if source is not None:
            await source.stop()
            self._room_snapshot = dict(source.status())
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _fail(self, message: str) -> None:
        self._running = False
        self._audio.use_browser(False)
        self._status = "error"
        self._error = message
        self._publish()

    def _publish(self) -> None:
        self._publish_state(self.state())
