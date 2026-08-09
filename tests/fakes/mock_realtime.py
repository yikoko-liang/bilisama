"""In-process fake Realtime server.

The point is not that it accepts a connection. The point is that each provider
quirk we have to code around becomes a reproducible failure mode here, so the
rules that avoid them are covered by tests rather than by good intentions in a
document.

Constructed from a Capabilities plus a Codec, so one test class can run against
all three provider shapes.

Loads no models, opens no sockets to the outside, and emits canned PCM.

Only three of the eight client-side rules are genuinely modelled today. The two
marked NOT IMPLEMENTED below are tracked in the backlog; the rest are not
represented at all yet.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.capabilities import Capabilities


class Fault(StrEnum):
    """Scriptable failure modes, one per provider quirk we code around."""

    # An in-band injection lands while a speculative turn is still open: the reply
    # is swallowed, no response.done is ever sent, and in_response stays stuck.
    WEDGE_ON_INJECTION = "wedge_on_injection"
    # response.cancel does nothing while a reply is merely pending.
    CANCEL_IS_NOOP = "cancel_is_noop"
    # Implicit replies never announce themselves with response.created.
    NO_RESPONSE_CREATED = "no_response_created"
    # item.create is silently deferred and acknowledged only once the reply ends.
    DEFER_ITEM_CREATE = "defer_item_create"
    # The next two are scenarios we are supposed to cover and do not yet. They stay
    # here so the gap is recorded rather than forgotten; see backlog item 9.
    #
    # NOT IMPLEMENTED: speech_stopped arriving with no preceding speech_started.
    ORPHAN_SPEECH_STOPPED = "orphan_speech_stopped"
    # NOT IMPLEMENTED: a cancelled text reply never sends output_text.done.
    NO_TEXT_DONE_ON_CANCEL = "no_text_done_on_cancel"
    # Reply hangs forever, to exercise the client-side watchdog.
    STALL_RESPONSE = "stall_response"
    # Server is at capacity.
    SESSION_LIMIT = "session_limit"
    # Emit a tool call.
    EMIT_TOOL_CALL = "emit_tool_call"


@dataclass(slots=True)
class Script:
    """What one test wants to reproduce."""

    faults: set[Fault] = field(default_factory=set)
    reply_text: str = "好的，我看到了。"
    delta_chunks: int = 3
    # Gap between deltas, so a test can slip an interruption in mid-reply.
    delta_interval_s: float = 0.0
    audio_ms_per_delta: int = 40

    def has(self, fault: Fault) -> bool:
        return fault in self.faults


@dataclass(slots=True)
class Recorded:
    """What the server received. Used to assert on client behaviour."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]

    def count(self, wire_type: str) -> int:
        return sum(1 for e in self.events if e.get("type") == wire_type)

    def last(self, wire_type: str) -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e.get("type") == wire_type:
                return e
        return None


def _pcm(ms: int, *, rate: int = 24000, freq: float = 220.0) -> bytes:
    n = int(rate * ms / 1000)
    return struct.pack(
        f"<{n}h", *(int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
    )


class MockRealtimeServer:
    """A fake Realtime server on an ephemeral loopback port.

    Usage::

        async with MockRealtimeServer(caps=capabilities.S2S) as server:
            ...  # connect to server.url
            assert server.recorded.count("response.create") == 1
    """

    def __init__(
        self,
        *,
        caps: Capabilities | None = None,
        codec: dia.Codec | None = None,
        script: Script | None = None,
    ) -> None:
        self.caps = caps or caps_mod.S2S
        self.codec = codec or dia.GA
        self.script = script or Script()
        self.recorded = Recorded()
        self._server: Any = None
        self._conn: ServerConnection | None = None
        self._port = 0
        # Server state, deliberately named after upstream so the two read together.
        self._in_response = False
        self._response_pending = False
        self._response_id = 0
        self._deferred: list[dict[str, Any]] = []
        self._speculative_open = False
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> MockRealtimeServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self._port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/v1/realtime"

    # ------------------------------------------------------------ server-side pushes

    async def send(self, event: dia.ServerEvent, **payload: Any) -> None:
        """Send an event, translated into the current dialect's wire name."""
        if self._conn is None:
            raise RuntimeError("no client has connected yet")
        body = {"type": self.codec.wire_name(event), **payload}
        await self._conn.send(json.dumps(body))

    async def speech_started(self) -> None:
        self._speculative_open = True
        await self.send(dia.ServerEvent.SPEECH_STARTED)

    async def speech_stopped(self) -> None:
        """Streamer stops talking. The speculative window stays open a moment longer."""
        await self.send(dia.ServerEvent.SPEECH_STOPPED)

    async def close_speculative_window(self) -> None:
        self._speculative_open = False

    async def barge_in(self) -> None:
        """Simulate the streamer talking over the assistant.

        Note the order: response.done(cancelled) arrives *before* speech_started.
        That reads backwards, but it is what upstream does — websocket_router.py:745-785
        snapshots in_response and calls finish_response before dispatching the
        speech_started event.
        """
        if self._in_response:
            await self._finish_response(status="cancelled", reason="turn_detected")
        self._speculative_open = True
        await self.send(dia.ServerEvent.SPEECH_STARTED)

    async def emit_implicit_reply(self) -> None:
        """The turn the server's own VAD starts. Sends no response.created."""
        self._response_pending = True
        task = asyncio.create_task(self._run_response(implicit=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ internals

    async def _handle(self, conn: ServerConnection) -> None:
        self._conn = conn
        await self.send(dia.ServerEvent.SESSION_CREATED, session={"id": "mock"})
        try:
            async for raw in conn:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.recorded.events.append(event)
                await self._dispatch(event)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conn = None

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == dia.ClientEvent.SESSION_UPDATE.value:
            if self.caps.acknowledges_session_update:
                await self.send(dia.ServerEvent.SESSION_UPDATED, session=event.get("session", {}))
        elif kind == dia.ClientEvent.AUDIO_APPEND.value:
            pass  # audio is recorded, never answered
        elif kind == dia.ClientEvent.ITEM_CREATE.value:
            await self._on_item_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CREATE.value:
            await self._on_response_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CANCEL.value:
            await self._on_cancel()
        elif kind == dia.ClientEvent.ITEM_TRUNCATE.value:
            if self.caps.item_truncate:
                await self.send(
                    dia.ServerEvent.ITEM_TRUNCATED,
                    item_id=event.get("item_id"),
                    content_index=event.get("content_index", 0),
                    audio_end_ms=event.get("audio_end_ms", 0),
                )
            else:
                await self._error("unknown_or_invalid_event", "不支持 conversation.item.truncate")

    async def _on_item_create(self, event: dict[str, Any]) -> None:
        # While a reply is generating, item.create is deferred: nothing comes back
        # now, and the ack arrives once the reply finishes.
        if self._in_response and self.script.has(Fault.DEFER_ITEM_CREATE):
            self._deferred.append(event)
            return
        await self.send(dia.ServerEvent.ITEM_CREATED, item=event.get("item", {}))

    async def _on_response_create(self, event: dict[str, Any]) -> None:
        out_of_band = (event.get("response") or {}).get("conversation") == "none"

        if self.script.has(Fault.SESSION_LIMIT):
            await self._error("session_limit_reached", "会话容量已满")
            return

        occupies_slot = not (out_of_band and self.caps.out_of_band_exempt_from_slot)
        if self.caps.single_response_slot and self._in_response and occupies_slot:
            await self._error("conversation_already_has_active_response", "已经有一个回复在生成了")
            return

        # In-band injection against an open speculative turn: the whole reply is
        # swallowed, not even a done event comes back.
        if self.script.has(Fault.WEDGE_ON_INJECTION) and not out_of_band and self._speculative_open:
            self._in_response = True  # wedged: every later response.create is refused
            return

        task = asyncio.create_task(self._run_response(implicit=False, event=event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_cancel(self) -> None:
        # Only a reply that has started can be cancelled; a pending one is a no-op.
        if self.script.has(Fault.CANCEL_IS_NOOP) and not self._in_response:
            return
        if not self._in_response:
            return
        await self._finish_response(status="cancelled", reason="client_cancelled")

    async def _run_response(self, *, implicit: bool, event: dict[str, Any] | None = None) -> None:
        self._response_id += 1
        rid = f"resp_{self._response_id}"
        self._in_response = True
        self._response_pending = False

        # Implicit replies never announce themselves with response.created.
        skip_created = implicit or self.script.has(Fault.NO_RESPONSE_CREATED)
        if not skip_created:
            await self.send(dia.ServerEvent.RESPONSE_CREATED, response={"id": rid})

        if self.script.has(Fault.STALL_RESPONSE):
            await asyncio.sleep(3600)  # let the client watchdog deal with it
            return

        if self.script.has(Fault.EMIT_TOOL_CALL):
            await self.send(
                dia.ServerEvent.FUNCTION_ARGS_DONE,
                response_id=rid,
                call_id="call_1",
                name="get_stream_status",
                arguments='{"key": "uptime"}',
            )
            await self._finish_response(status="completed")
            return

        text = self.script.reply_text
        step = max(1, len(text) // max(1, self.script.delta_chunks))
        pieces = [text[i : i + step] for i in range(0, len(text), step)]

        for piece in pieces:
            if not self._in_response:
                return  # cancelled mid-reply
            if self.caps.owns_tts:
                await self.send(dia.ServerEvent.TRANSCRIPT_DELTA, response_id=rid, delta=piece)
                await self.send(
                    dia.ServerEvent.AUDIO_DELTA,
                    response_id=rid,
                    delta=base64.b64encode(_pcm(self.script.audio_ms_per_delta)).decode(),
                )
            else:
                await self.send(dia.ServerEvent.TEXT_DELTA, response_id=rid, delta=piece)
            if self.script.delta_interval_s:
                await asyncio.sleep(self.script.delta_interval_s)

        if not self._in_response:
            return
        if not self.caps.owns_tts:
            await self.send(dia.ServerEvent.TEXT_DONE, response_id=rid, text=text)
        await self._finish_response(status="completed")

    async def _finish_response(self, *, status: str, reason: str | None = None) -> None:
        if not self._in_response:
            return
        self._in_response = False
        self._response_pending = False

        payload: dict[str, Any] = {
            "response": {"id": f"resp_{self._response_id}", "status": status}
        }
        if reason:
            payload["response"]["status_details"] = {"reason": reason}
        await self.send(dia.ServerEvent.RESPONSE_DONE, **payload)

        # Deferred item.create calls are acknowledged now. The client has to cope
        # with an ack arriving long after the request.
        while self._deferred:
            pending = self._deferred.pop(0)
            await self.send(dia.ServerEvent.ITEM_CREATED, item=pending.get("item", {}))

    async def _error(self, code: str, message: str) -> None:
        await self.send(dia.ServerEvent.ERROR, error={"type": code, "message": message})
