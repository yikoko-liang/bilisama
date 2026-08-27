"""A fake Volcengine dialogue server, modelled uglier than the real one.

This repo has been burned twice by a fake kinder than the endpoint it stood
in for: the mock accepted a bare session.update that the real s2s server threw
out whole, and it invented transcript deltas DashScope never sends. Both bugs
lived inside a green test suite. So every rule below refuses something, and the
refusals are what the tests are for.

What it insists on, and why each one can really bite:

* **The handshake climbs in order.** StartSession before ConnectionStarted is
  an error, not a shortcut. A client that fires both without waiting works by
  luck on a fast loopback and fails on a real network.
* **Session-level events carry the session id.** Sending 0 for the length —
  the shape you get from a client that forgot the field exists — shifts every
  byte after it, so the failure has to be loud here.
* **Uplink audio is Raw, never JSON.** A JSON serialization bit on PCM means
  the client filled the header in from the wrong branch.
* **Barge-in is ASRInfo and nothing else.** There is no speech_started to fall
  back on. A client that waits for one waits forever.
* **It sends events we have no name for.** UsageResponse arrives unasked; a
  decoder that treats an unknown event as fatal takes the session down.
* **One session per connection.** A second StartSession before the first is
  retired is refused, the way the real one answers 「session number limit
  exceeded: 1」.

One thing this fake cannot model, said out loud so nobody assumes otherwise:
it handles frames strictly in order, while the real server is concurrent. So
a client that sends FinishSession and StartSession back to back without
waiting for SessionFinished looks identical here to one that waits — the two
frames never cross. That race is real (it broke the SC context swap under
load, green on an idle box) and only the real-endpoint contract tests see it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from bilisama.realtime.providers import volcano_wire as wire

__all__ = ["MockVolcanoServer", "Recorded"]


@dataclass
class Recorded:
    """Everything the client sent, for tests to assert against."""

    events: list[int] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    session_ids: list[str] = field(default_factory=list)

    def count(self, event: wire.ClientEvent) -> int:
        return self.events.count(int(event))

    def body_for(self, event: wire.ClientEvent) -> dict[str, Any]:
        """The last body sent under this event, or {} if it never was."""
        for sent, body in zip(reversed(self.events), reversed(self.bodies), strict=True):
            if sent == int(event):
                return body
        return {}


class MockVolcanoServer:
    """A fake dialogue endpoint on an ephemeral loopback port.

    Usage::

        async with MockVolcanoServer() as server:
            volcano = VolcanoLink(server.url, app_id="a", access_key="k", config=cfg)
            await volcano.connect()
            await server.say("你好呀", audio=b"\\x01\\x02")
    """

    def __init__(self, *, refuse_connection: bool = False, refuse_session: bool = False) -> None:
        self.recorded = Recorded()
        # Handed out on the first session and expected back on the next. A fake
        # that invented a new one each time would let a client forget to resume
        # and still look correct.
        self.dialog_id = "dlg-abc"
        self.resumed_with: list[str] = []
        self.interrupted = 0
        # Every text and lifecycle frame carries one; the client keys its
        # tombstones off it.
        self.question_id = "q1"
        self.refuse_connection = refuse_connection
        self.refuse_session = refuse_session
        self._server: Any = None
        self._conn: ServerConnection | None = None
        self._port = 0
        self._session_id = ""
        self._connected = False
        self._speaking = False
        self._ready = asyncio.Event()

    async def __aenter__(self) -> MockVolcanoServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self._port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/api/v3/realtime/dialogue"

    # ---------------------------------------------------------------- inbound

    async def _handle(self, ws: ServerConnection) -> None:
        self._conn = ws
        # A session belongs to the connection that opened it. A reconnect
        # therefore starts with none, which is what lets its StartSession past
        # the one-session-per-connection rule below.
        self._session_id = ""
        self.recorded.headers = dict(ws.request.headers) if ws.request else {}
        async for message in ws:
            data = message if isinstance(message, bytes) else str(message).encode()
            frame = wire.decode(data)
            self.recorded.events.append(frame.event or 0)
            self.recorded.session_ids.append(frame.session_id)
            if frame.kind is wire.MessageKind.AUDIO_CLIENT:
                self.recorded.audio.append(frame.payload)
                self.recorded.bodies.append({})
                # Raw, never JSON. A client that set the JSON bit here built
                # its header from the wrong branch, and the real server would
                # try to parse PCM as text.
                assert data[2] >> 4 == 0b0000, "上行音频不是 Raw 序列化"
                continue
            self.recorded.bodies.append(frame.json())
            await self._on_event(frame)

    async def _on_event(self, frame: wire.Frame) -> None:
        event = frame.event
        if event == wire.ClientEvent.START_CONNECTION:
            if self.refuse_connection:
                await self._emit(wire.ServerEvent.CONNECTION_FAILED, {"error": "拒绝连接"})
                return
            self._connected = True
            await self._emit(wire.ServerEvent.CONNECTION_STARTED, {})
            return
        if event == wire.ClientEvent.START_SESSION:
            # Order matters, and a fast loopback is exactly where skipping it
            # would go unnoticed.
            assert self._connected, "还没建连接就开会话"
            # One session per connection. The real endpoint answers a second
            # one with 「session number limit exceeded: 1」, which is how a
            # context swap that did not wait for SessionFinished presented:
            # green on an idle box, timed out under load with the old persona
            # still in place.
            assert not self._session_id, "上一个会话还没收掉就开新的（真端点会拒）"
            assert frame.session_id, "会话级事件没带 session id"
            if self.refuse_session:
                await self._emit(
                    wire.ServerEvent.SESSION_FAILED,
                    {"error": "会话建不起来"},
                    session_id=frame.session_id,
                )
                return
            self._session_id = frame.session_id
            dialog = frame.json().get("dialog") or {}
            if dialog.get("dialog_id"):
                self.resumed_with.append(str(dialog["dialog_id"]))
            # Both `extra` objects are required; the real server answers a null
            # one with 42000020 rather than defaulting it.
            assert (frame.json().get("asr") or {}).get("extra") is not None, "asr.extra 是空的"
            assert (frame.json().get("tts") or {}).get("extra") is not None, "tts.extra 是空的"
            assert dialog.get("extra", {}).get("model"), "dialog.extra.model 没传，它是必传参数"
            await self._emit(
                wire.ServerEvent.SESSION_STARTED,
                {"dialog_id": self.dialog_id},
                session_id=frame.session_id,
            )
            # Unasked-for and undocumented in our event table. A decoder that
            # treats an unknown event as fatal drops the session right here.
            await self._emit(
                wire.ServerEvent.USAGE_RESPONSE,
                {"usage": {"tokens": 0}},
                session_id=frame.session_id,
            )
            self._ready.set()
            return
        if event == wire.ClientEvent.FINISH_SESSION:
            # The connection survives; the docs are explicit that the socket
            # is reusable, which is what makes a context swap cheap on SC.
            finished, self._session_id = self._session_id, ""
            await self._emit(wire.ServerEvent.SESSION_FINISHED, {}, session_id=finished)
            return
        if event == wire.ClientEvent.CLIENT_INTERRUPT:
            # The real one stops sending within a frame or two. Modelled as an
            # immediate stop so a client that never sends this shows up as
            # audio that keeps arriving.
            self.interrupted += 1
            self._speaking = False
            return
        if event in (wire.ClientEvent.TASK_REQUEST, wire.ClientEvent.UPDATE_CONFIG):
            assert frame.session_id, "会话级事件没带 session id"
        # UpdateConfig gets no acknowledgement at all — there is no
        # session.updated here. A client that waits for one hangs.

    # --------------------------------------------------------------- outbound

    async def _emit(
        self, event: wire.ServerEvent, body: dict[str, Any], *, session_id: str = ""
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode()
        await self._send(event, payload, session_id=session_id, raw=False)

    async def _send(self, event: int, payload: bytes, *, session_id: str, raw: bool) -> None:
        assert self._conn is not None
        kind = wire.MessageKind.AUDIO_SERVER if raw else wire.MessageKind.FULL_SERVER
        header = bytes((0x11, (kind << 4) | 0b0100, 0b0000_0000 if raw else 0b0001_0000, 0))
        parts = [header, struct.pack(">i", int(event))]
        # Connection-level replies carry no id; session-level ones do. The
        # decoder keys off the event number, so getting this wrong here would
        # hide a decoder that keys off something else.
        if event >= wire.ServerEvent.SESSION_STARTED:
            ident = (session_id or self._session_id).encode()
            parts.append(struct.pack(">I", len(ident)))
            parts.append(ident)
        parts.append(struct.pack(">I", len(payload)))
        parts.append(payload)
        # The client may have hung up first. A real server drops the frame and
        # moves on; crashing here would turn every teardown race into a red
        # test about nothing. Not kindness — kindness would be accepting a
        # frame the real one refuses, and every rule above still refuses.
        with contextlib.suppress(ConnectionClosed):
            await self._conn.send(b"".join(parts))

    async def wait_for(self, event: wire.ClientEvent, *, count: int = 1) -> None:
        """Block until the client's frame has actually landed here.

        Reading `recorded` straight after a send is a race the loopback loses
        often enough to be flaky and wins often enough to look fine — and when
        it loses, `body_for` quietly returns the PREVIOUS frame's body, so the
        assertion passes against the wrong data.
        """
        for _ in range(200):
            if self.recorded.count(event) >= count:
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"等不到客户端发 {event.name}（收到的是 {self.recorded.events}）")

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=2.0)

    async def drop(self) -> None:
        """Lose the socket mid-session.

        1011 rather than 1006: the latter is what the client OBSERVES on an
        abnormal close and is reserved — sending it is a protocol error. Either
        way the client sees ConnectionClosed, which is the thing under test.
        """
        assert self._conn is not None
        self._connected = False
        self._speaking = False
        self._session_id = ""
        self._ready = asyncio.Event()
        await self._conn.close(code=1011)

    async def say_slowly(self, chunks: int = 20, *, gap: float = 0.02) -> None:
        """Keep talking until something stops us. What a client that never
        sends ClientInterrupt would go on hearing."""
        self._speaking = True
        for _ in range(chunks):
            if not self._speaking:
                return
            await self._send(
                wire.ServerEvent.TTS_RESPONSE, b"\x01\x02" * 160, session_id="", raw=True
            )
            await asyncio.sleep(gap)

    async def confirm_query(self, question: str = "q1") -> None:
        """The ack for a ChatTextQuery, which is where the server first names
        the question_id everything after it carries."""
        await self._emit(wire.ServerEvent.CHAT_TEXT_QUERY_CONFIRMED, {"question_id": question})

    async def late_tail(self, question: str = "q1", *, audio: bytes = b"\x03\x04") -> None:
        """What a server that has not stopped yet keeps sending after we gave
        up on a reply — a watchdog timeout does not reach the far end."""
        await self._emit(
            wire.ServerEvent.TTS_SENTENCE_START,
            {"question_id": question, "reply_id": "r1", "tts_type": "default"},
        )
        await self._send(wire.ServerEvent.TTS_RESPONSE, audio, session_id="", raw=True)
        await self._emit(
            wire.ServerEvent.CHAT_RESPONSE, {"content": "迟到的", "question_id": question}
        )
        await self._emit(wire.ServerEvent.TTS_ENDED, {"question_id": question, "reply_id": "r1"})

    async def say(self, text: str, *, audio: bytes = b"") -> None:
        """One complete reply: text, then audio, then the two enders.

        ChatEnded before TTSEnded, which is the order that matters — the model
        stops generating well before the audience stops hearing, and a client
        that closes the reply on the first of them frees the slot mid-sentence.
        """
        question = self.question_id
        for chunk in text:
            await self._emit(
                wire.ServerEvent.CHAT_RESPONSE, {"content": chunk, "question_id": question}
            )
        await self._emit(wire.ServerEvent.CHAT_ENDED, {"question_id": question})
        if audio:
            await self._emit(
                wire.ServerEvent.TTS_SENTENCE_START,
                {"question_id": question, "tts_type": "default"},
            )
            await self._send(wire.ServerEvent.TTS_RESPONSE, audio, session_id="", raw=True)
            await self._emit(wire.ServerEvent.TTS_SENTENCE_END, {"question_id": question})
        await self._emit(wire.ServerEvent.TTS_ENDED, {"question_id": question})

    async def barge_in(self, transcript: str = "等一下") -> None:
        """The streamer starts talking.

        ASRInfo is the whole signal — the vendor's own words for it are 「用于
        打断客户端的播报」. There is no speech_started event to fall back on,
        so a client waiting for one waits forever.
        """
        await self._emit(wire.ServerEvent.ASR_INFO, {})
        await self._emit(
            wire.ServerEvent.ASR_RESPONSE,
            {"results": [{"text": transcript, "is_interim": True}]},
        )
        await self._emit(
            wire.ServerEvent.ASR_RESPONSE,
            {"results": [{"text": transcript, "is_interim": False}]},
        )
        await self._emit(wire.ServerEvent.ASR_ENDED, {})

    async def fail(self, code: int = 45000001, message: str = "并发超限") -> None:
        await self._emit(
            wire.ServerEvent.DIALOG_COMMON_ERROR, {"error_code": code, "message": message}
        )

    async def fail_hard(self, code: int, message: str) -> None:
        """A connection-level error: message type ERROR, an error code in the
        optional fields, and NO event number. That last part is the whole
        point — a client dispatching on the event number alone never sees it.
        """
        assert self._conn is not None
        payload = json.dumps({"error": message}, ensure_ascii=False).encode()
        header = bytes((0x11, (wire.MessageKind.ERROR << 4) | 0b0000, 0b0001_0000, 0))
        await self._conn.send(
            header + struct.pack(">I", code) + struct.pack(">I", len(payload)) + payload
        )

    async def send_garbage(self) -> None:
        """A frame no decoder can read. One of these is not a dead session."""
        assert self._conn is not None
        await self._conn.send(b"\xff\xff")
