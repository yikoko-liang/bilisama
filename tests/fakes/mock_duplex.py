"""A fake Seeduplex server, as unhelpful as the real one.

Every rule below was measured against the real endpoint on 2026-09-16, and
every one of them refuses something a kinder fake would let through. That is
the point: this repo has twice shipped a green suite over a mock gentler than
the endpoint it stood in for.

What it insists on:

* **The client speaks first.** No greeting until `session.create` arrives.
  A client that waits for one waits forever, which is exactly what
  RealtimeClient does and why this provider has its own adapter.
* **Replies never announce, and hers start before they announce.** There is
  no `response.created`. `response.output_audio.started` carries the
  `tts_type` that says whose words these are — `chat_tts_text` for text we
  were handed, `default` for one she started herself — and a client that
  cannot tell them apart puts her own turns and ours in the same basket,
  where the voice gate then holds the wrong ones. But on HER turns the first
  text deltas arrive ahead of that frame, so a client that waits for it to
  book the turn silently drops the opening of every sentence.
* **An unknown event name closes the socket.** Not an error frame: a 1006.
  Measured 2026-09-16 across fifteen guessed names — `response.create`,
  `chat.text.query`, `ChatTextQuery` and the rest all hang up identically.
  The uplink vocabulary is closed, and a client that reaches outside it dies
  here the way it dies there.
* **History only lands at create, and only in even-length pairs.**
  `conversation.item.create` acks with `conversation.item.added` and
  `session.update` with a `dialog_context` acks with `session.updated`, and
  neither reaches `context`. Only `extension.dialog.dialog_context` on
  `session.create` does — and the real endpoint reads that array positionally
  in pairs without looking at `role`, so an odd length discards the WHOLE
  context while still answering `session.created`. This fake discards it the
  same way. It additionally records a violation for an array that is even but
  out of order: there the endpoint keeps the text and hangs the wrong speaker
  on it, which is harder to notice and worse to ship. The acks are what makes
  all this worth modelling — a client cannot tell success from silence by the
  reply, only by asking her afterwards.
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
# How much of her turn streams before the audio frame announces it. Measured
# at 11 characters over 5 deltas; 2 is enough to keep a client honest.
_AHEAD = 2

# The closed uplink vocabulary. Anything else and the real endpoint hangs up.
_KNOWN = frozenset(
    {
        "session.create",
        "session.update",
        "session.close",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        # The keepalive pair. Documented, and confirmed present 2026-09-16 —
        # neither hangs up. Neither starts a turn either.
        "input_audio_mute.commit",
        "input_audio_unmute.commit",
        "conversation.item.create",
        "conversation.item.retrieve",
        "conversation.item.delete",
        "response.cancel",
        "speech_text_buffer.append",
        "speech_text_buffer.commit",
        "speech_text_buffer.replacement.append",
        "speech_text_buffer.replacement.commit",
    }
)


def _in_order(context: list[dict[str, str]]) -> bool:
    """user, assistant, user, assistant — what the positional read assumes."""
    return all(
        turn.get("role") == ("user" if i % 2 == 0 else "assistant")
        for i, turn in enumerate(context)
    )


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

    def __init__(self, *, refuse_session: bool = False, deaf_to_tts: bool = False) -> None:
        self.recorded = Recorded()
        self.violations: list[str] = []
        # What the model would answer from, as far as this fake models it:
        # only what arrived in dialog_context at create.
        self.context: list[dict[str, str]] = []
        # True when a dialog_context arrived and was thrown away for shape.
        self.context_refused = False
        # What came back as a function-call result, in arrival order.
        self.tool_results: list[str] = []
        self.tools: list[dict[str, Any]] = []
        self.instructions = ""
        self.session_id = "sess-1"
        self._refuse_session = refuse_session
        # Takes a speech_text_buffer.replacement and plays nothing: no error,
        # no refusal, no frames. Seen live on 2026-09-17, twice in a row, and
        # the slot it holds is the one her own turns need.
        self._deaf_to_tts = deaf_to_tts
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
            first = items[0] if items else {}
            if "call_id" in first:
                # A function-call result. The real endpoint is unforgiving
                # about this shape — five near-misses were dropped whole, with
                # neither an ack nor an error — so anything else is a
                # violation here rather than a silent pass.
                self._refuse(
                    "FC 回传要用文档的形状：role=tool、content=[{type:input_text,text}]",
                    ok=first.get("role") == "tool"
                    and [c.get("type") for c in first.get("content") or []] == ["input_text"],
                )
                self.tool_results.append(str((first.get("content") or [{}])[0].get("text") or ""))
                return
            await self._send(ws, {"type": "conversation.item.added", "items": items})
        elif kind == "speech_text_buffer.commit":
            # 打招呼 — the vendor's turn-start primitive, mapped from the legacy
            # SayHello(300). It speaks on its own frame, no append needed, and
            # it speaks whatever the uplink is doing: measured on the real
            # endpoint 2026-09-21 in four conditions (no uplink, silent uplink,
            # and twice after a real exchange), audio every time with
            # tts_type chat_tts_text. Deliberately NOT gated on _deaf_to_tts:
            # that fault models the replacement channel, which is a different
            # event with a different failure.
            await self.say(str(frame.get("text") or ""), ours=True)
        elif kind == "speech_text_buffer.replacement.append":
            self._pending = str(frame.get("text") or "")
        elif kind == "speech_text_buffer.replacement.commit":
            if not self._deaf_to_tts:
                await self.say(getattr(self, "_pending", ""), ours=True)
        elif kind == "response.cancel":
            await self._send(ws, {"type": "response.canceled"})
        elif kind == "session.close":
            await self._send(ws, {"type": "session.closed"})
        elif kind not in _KNOWN:
            # Outside the vocabulary: an error frame, and then the hang-up,
            # 0.0-0.1 s later (measured 2026-09-21, polling once a second for
            # 25 s with and without uplink keep-alive). Both halves are real
            # and the repo only ever recorded one each: duplex.py's header said
            # "Not an error frame: a 1006", volcano.py:32-35 said the names all
            # answered `unknown event name` and said nothing about the close.
            # The error frame is why a client can log WHICH name it got wrong;
            # the close is why it still costs the session.
            await self._send(
                ws,
                {
                    "type": "error",
                    "event_id": str(frame.get("event_id") or ""),
                    "error": {
                        "type": "Bad Request",
                        "code": "45000000",
                        "message": f"unknown event name:{kind}",
                    },
                },
            )
            await ws.close(code=1006)

    async def _on_create(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        if self._refuse_session:
            await self._send(
                ws, {"type": "error", "error": {"type": "invalid_request_error", "message": "拒绝"}}
            )
            return
        session = frame.get("session") or {}
        self.instructions = str(session.get("instructions") or "")
        self.tools = list(session.get("tools") or [])
        dialog = list(
            ((frame.get("extension") or {}).get("dialog") or {}).get("dialog_context") or []
        )
        # Odd length and the endpoint keeps NONE of it, silently. The session
        # still comes up, so a client only finds out by asking her.
        self.context_refused = bool(dialog) and len(dialog) % 2 == 1
        self.context = [] if self.context_refused else dialog
        self._refuse(
            "dialog_context 顺序错了：服务端按位置配对，不看 role，会把话安到另一个人头上",
            ok=_in_order(self.context),
        )
        self._created = True
        await self._send(ws, {"type": "session.created", "session": {"id": self.session_id}})
        self._ready.set()

    async def _send(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(json.dumps(frame))

    # ------------------------------------------------------------ scripting

    async def say(self, text: str, *, ours: bool, audio: bytes = b"\x01\x02") -> None:
        """One reply. `ours` decides the tts_type, which is the whole game.

        On her own turns the first characters arrive BEFORE
        `response.output_audio.started` — eleven of them in one measured run —
        so a client that books the turn on the audio frame throws away the
        start of everything she says. This fake used to send the audio frame
        first, which is precisely why it certified a client that did.
        """
        ws = self._ws
        assert ws is not None
        head, tail = (text[:_AHEAD], text[_AHEAD:]) if not ours else ("", "")
        for ch in head:
            await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
        await self._send(
            ws, {"type": "response.output_audio.started", "tts_type": _OURS if ours else _HERS}
        )
        if not ours:
            # Only her own turns stream text; the TTS path sends none, which
            # is why the adapter has to report the words it supplied.
            for ch in tail:
                await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
            await self._send(ws, {"type": "response.output_text.done", "text": text})
        await self._send(
            ws,
            {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        )
        await self._send(ws, {"type": "response.output_audio.done"})
        await self._send(ws, {"type": "response.done"})

    async def wants_events(self, call_id: str = "call-1") -> None:
        """She calls the pull tool, the way she does after a streamer turn.

        Only meaningful when the client declared one: `tools` is recorded off
        session.create so a test can assert the declaration went out at all.
        """
        ws = self._ws
        assert ws is not None
        self._refuse("没声明 tools 就不该收到工具调用", ok=bool(self.tools))
        await self._send(
            ws,
            {
                "type": "response.function_call_arguments.done",
                "items": [{"call_id": call_id, "name": "get_live_events", "arguments": "{}"}],
            },
        )

    async def hear(self, transcript: str, *, flap: str = "") -> None:
        """The streamer speaks. Deltas are cumulative, `completed` is empty.

        `flap` replays the tail the real transcriber changes its mind about:
        measured as 「…游戏」「…游戏？」「…游戏」「…游戏？」, punctuation going
        back and forth after the words have settled. A client that measures
        the next increment against the LAST value sends that mark twice.
        """
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "conversation.item.input_audio_transcription.started"})
        steps = [transcript[:i] for i in range(1, len(transcript) + 1)]
        if flap:
            steps += [transcript + flap, transcript, transcript + flap]
        for step in steps:
            await self._send(
                ws,
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "delta": step,
                },
            )
        await self._send(
            ws,
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""},
        )

    async def hear_without_ending(self, transcript: str) -> None:
        """The streamer speaks and the transcriber never says he stopped.

        Seen in a live session: the started frame and the deltas arrive, the
        completed frame does not, the socket stays healthy, and nothing on the
        wire says anything is wrong. On this protocol that is the end of her
        talking for the rest of the session.
        """
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

    async def still_hearing(self, so_far: str) -> None:
        """One more cumulative delta on a turn already open — the streamer
        carrying on. A client timing its backstop from the START of the turn
        cuts him off here; one timing it from the last delta does not."""
        ws = self._ws
        assert ws is not None
        await self._send(
            ws,
            {"type": "conversation.item.input_audio_transcription.delta", "delta": so_far},
        )

    async def hear_tail(self, transcript: str) -> None:
        """Close a turn the client may already have given up on. The endpoint
        has no idea we timed out, so the ending looks entirely ordinary."""
        ws = self._ws
        assert ws is not None
        await self._send(
            ws,
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""},
        )

    async def fail(self, code: str = "50000000", message: str = "模型推理出错") -> None:
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "error", "error": {"code": code, "message": message}})
