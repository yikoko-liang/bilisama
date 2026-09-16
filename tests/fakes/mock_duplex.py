"""A fake Seeduplex server, as unhelpful as the real one.

Every rule below was measured against the real endpoint on 2026-09-16, and
every one of them refuses something a kinder fake would let through. That is
the point: this repo has twice shipped a green suite over a mock gentler than
the endpoint it stood in for.

What it insists on:

* **The client speaks first.** No greeting until `session.create` arrives.
  A client that waits for one waits forever, which is exactly what
  RealtimeClient does and why this provider has its own adapter.
* **Replies never announce.** There is no `response.created`. The first frame
  of any reply is `response.output_audio.started`, carrying the `tts_type`
  that says whose words these are — `chat_tts_text` for text we were handed,
  `default` for one she started herself. A client that cannot tell them apart
  puts her own turns and ours in the same basket, and the voice gate then
  holds the wrong ones.
* **`response.create` closes the socket.** Not an error frame: a 1006. Any
  client that reaches for it must die here the way it dies there.
* **History only lands at create.** `conversation.item.create` acks with
  `conversation.item.added` and `session.update` with a `dialog_context` acks
  with `session.updated`, and neither reaches `context`. Only
  `extension.dialog.dialog_context` on `session.create` does. The acks are
  what makes this worth modelling — a client cannot tell success from silence
  by the reply, only by asking her afterwards.
* **Transcription carries the speech edges.** There is no
  `input_audio_buffer.speech_started`; `...transcription.started` is the
  earliest one, its deltas are CUMULATIVE, and `...completed` comes back with
  an empty transcript.

Violations land in `violations` and fail the test from `__aexit__`, the same
machinery mock_volcano.py uses and for the same reason: websockets swallows
whatever a handler raises, so an assert in here would close the socket with
1011 and read as a stray reconnect in an otherwise green test.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

__all__ = ["MockDuplexServer", "Recorded"]

_OURS = "chat_tts_text"
_HERS = "default"


@dataclass
class Recorded:
    """Every client frame, in order."""

    frames: list[dict[str, Any]] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.frames if f.get("type") == kind)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == kind]

    def last(self, kind: str) -> dict[str, Any]:
        found = self.of(kind)
        assert found, f"客户端从没发过 {kind}"
        return found[-1]


class MockDuplexServer:
    """One connection, one session, and the endpoint's own bad manners."""

    def __init__(self, *, refuse_session: bool = False) -> None:
        self.recorded = Recorded()
        self.violations: list[str] = []
        # What the model would answer from, as far as this fake models it:
        # only what arrived in dialog_context at create.
        self.context: list[dict[str, str]] = []
        self.instructions = ""
        self.session_id = "sess-1"
        self._refuse_session = refuse_session
        self._server: Any = None
        self._ws: ServerConnection | None = None
        self._ready = asyncio.Event()
        self._created = False

    async def __aenter__(self) -> MockDuplexServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self.check()

    def check(self) -> None:
        assert not self.violations, "假服务端记下的违规：\n  " + "\n  ".join(self.violations)

    def _refuse(self, rule: str, *, ok: bool) -> bool:
        if not ok:
            self.violations.append(rule)
        return ok

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/api/v3/duplex/realtime/dialogue"

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=5.0)

    # ------------------------------------------------------------ the wire

    async def _handle(self, ws: ServerConnection) -> None:
        self._ws = ws
        try:
            async for raw in ws:
                if not self._refuse("上行必须是 JSON 文本帧，不是二进制", ok=isinstance(raw, str)):
                    continue
                try:
                    frame = json.loads(raw)
                except ValueError:
                    self.violations.append(f"上行不是合法 JSON：{raw[:80]!r}")
                    continue
                self.recorded.frames.append(frame)
                await self._on_frame(ws, frame)
        except websockets.ConnectionClosed:
            return

    async def _on_frame(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")

        if kind == "session.create":
            if self._refuse("一条连接只建一次会话", ok=not self._created):
                await self._on_create(ws, frame)
            return

        if not self._refuse(f"{kind} 发在 session.create 之前", ok=self._created):
            return

        if kind == "session.update":
            session = frame.get("session") or {}
            if "instructions" in session:
                # Instructions DO apply mid-session — the one thing that does.
                self.instructions = str(session["instructions"])
            # dialog_context here is acked and dropped, which is the trap.
            await self._send(ws, {"type": "session.updated"})
        elif kind == "input_audio_buffer.append":
            audio = frame.get("audio")
            self._refuse("上行音频必须是 base64 字符串", ok=isinstance(audio, str))
        elif kind == "input_audio_buffer.commit":
            await self._send(ws, {"type": "input_audio_buffer.committed"})
        elif kind == "conversation.item.create":
            items = frame.get("items") or []
            await self._send(ws, {"type": "conversation.item.added", "items": items})
        elif kind in ("speech_text_buffer.replacement.append", "speech_text_buffer.commit"):
            self._pending = str(frame.get("text") or "")
        elif kind == "speech_text_buffer.replacement.commit":
            await self.say(getattr(self, "_pending", ""), ours=True)
        elif kind == "response.cancel":
            await self._send(ws, {"type": "response.canceled"})
        elif kind == "session.close":
            await self._send(ws, {"type": "session.closed"})
        elif kind == "response.create":
            # The real endpoint does not answer this. It hangs up.
            await ws.close(code=1006)

    async def _on_create(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        if self._refuse_session:
            await self._send(
                ws, {"type": "error", "error": {"type": "invalid_request_error", "message": "拒绝"}}
            )
            return
        session = frame.get("session") or {}
        self.instructions = str(session.get("instructions") or "")
        dialog = ((frame.get("extension") or {}).get("dialog") or {}).get("dialog_context") or []
        self.context = list(dialog)
        self._created = True
        await self._send(ws, {"type": "session.created", "session": {"id": self.session_id}})
        self._ready.set()

    async def _send(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(json.dumps(frame))

    # ------------------------------------------------------------ scripting

    async def say(self, text: str, *, ours: bool, audio: bytes = b"\x01\x02") -> None:
        """One reply. `ours` decides the tts_type, which is the whole game."""
        ws = self._ws
        assert ws is not None
        await self._send(
            ws, {"type": "response.output_audio.started", "tts_type": _OURS if ours else _HERS}
        )
        if not ours:
            # Only her own turns stream text; the TTS path sends none, which
            # is why the adapter has to report the words it supplied.
            for ch in text:
                await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
            await self._send(ws, {"type": "response.output_text.done", "text": text})
        await self._send(
            ws,
            {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        )
        await self._send(ws, {"type": "response.output_audio.done"})
        await self._send(ws, {"type": "response.done"})

    async def hear(self, transcript: str) -> None:
        """The streamer speaks. Deltas are cumulative, `completed` is empty."""
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "conversation.item.input_audio_transcription.started"})
        for i in range(1, len(transcript) + 1):
            await self._send(
                ws,
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "delta": transcript[:i],
                },
            )
        await self._send(
            ws,
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""},
        )

    async def fail(self, code: str = "50000000", message: str = "模型推理出错") -> None:
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "error", "error": {"code": code, "message": message}})
