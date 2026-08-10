"""The Realtime transport every adapter shares.

One client, three providers: the dialect differences live in the Codec and the
behaviour differences in Capabilities, so this file contains no `if provider`
branch anywhere — the day one appears, every function grows one within a
quarter (plan section 3.1).

What lives here, and the section 3.3 rule it implements:

- Command serialisation (rule 5 by discipline): session.update, item.create and
  response.create wait until no reply is generating. The server's own guard
  misses the pending window, so waiting is ours to do. Audio append and cancel
  bypass the queue — one exists to keep the audio clock alive (rule 7), the
  other exists to interrupt.
- Slot bookkeeping (rule 4): busy when we send a create or when an unknown
  response id produces its first frame (implicit replies never announce with
  response.created); free on any response.done, matched or not — an unmatched
  done means our books were wrong, and holding the slot on wrong books wedges
  us, not the server.
- The watchdog (rule 2): a create that produces no done within `watchdog_s`
  gets a response.cancel — the only client-side path that can free a stuck
  in_response.
- Staleness: a cancelled or superseded reply flips its handle's `stale`; frames
  that arrive later carrying that reply's id are dropped without ceremony
  (qwen-audio-agent's tombstone idea, realtime-gateway.mjs:627,668).

Retries never re-enter the command queue: the queue's tail may be waiting on
the very reply whose rejection we would be retrying — the deadlock upstream
warns about at realtime-provider.mjs:568-575.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.capabilities import Capabilities

__all__ = ["RealtimeClient", "ReplyRecord"]

_WATCHDOG_S = 25.0  # plan section 3.3 rule 2


@dataclass(slots=True)
class ReplyRecord:
    """Client-side books for one reply, ours or the server's own."""

    handle: link.ReplyHandle
    rid: str | None = None  # learned from response.created, or first frame
    ours: bool = False
    text: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class RealtimeClient:
    """Transport, slot and staleness — everything below the adapter."""

    def __init__(
        self,
        url: str,
        *,
        caps: Capabilities,
        codec: dia.Codec,
        clock: Clock | None = None,
        watchdog_s: float = _WATCHDOG_S,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.caps = caps
        self.codec = codec
        self._url = url
        self._headers = headers or {}
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._ws: Any = None
        self._events: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # The command queue is a lock, not a worker: holders send their own
        # frame so a failure surfaces at the call site, not in a background task.
        self._command_lock = asyncio.Lock()
        self._replies: dict[str, ReplyRecord] = {}
        self._slot_free = asyncio.Event()
        self._slot_free.set()
        self._awaiting_created: list[ReplyRecord] = []

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self._url, max_size=16 * 1024 * 1024, additional_headers=self._headers
        )
        first = json.loads(await self._ws.recv())
        kind, _ = self.codec.normalize(first)
        if kind is not dia.ServerEvent.SESSION_CREATED:
            raise ConnectionError(f"服务端第一帧不是 session.created：{first.get('type')}")
        self._recv_task = asyncio.create_task(self._recv_loop(), name="realtime:recv")

    async def aclose(self) -> None:
        for task in (self._recv_task, *self._tasks):
            if task is not None:
                task.cancel()
        pending = [t for t in (self._recv_task, *self._tasks) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._drain()

    async def _drain(self) -> AsyncIterator[link.LinkEvent]:
        while True:
            yield await self._events.get()

    # ------------------------------------------------------------ sending

    async def push_audio(self, pcm: bytes) -> None:
        """Append mic audio. Never serialised: the reopen window runs on the
        audio clock (vad_handler.py:255-259), and a starved stream freezes it."""
        await self._send_raw(
            {
                "type": dia.ClientEvent.AUDIO_APPEND.value,
                "audio": base64.b64encode(pcm).decode(),
            }
        )

    async def send_command(self, frame: dict[str, Any]) -> None:
        """Send a control frame after the current reply, if any, has finished."""
        async with self._command_lock:
            await self._wait_for_slot()
            await self._send_raw(frame)

    async def request_reply(self, frame: dict[str, Any]) -> link.ReplyHandle:
        """Send a response.create and take the slot.

        The frame comes from the adapter (via Codec.response_create), because
        what goes in it — out-of-band or not, text or audio — is provider
        policy, not transport.
        """
        record = ReplyRecord(handle=link.ReplyHandle(), ours=True)
        async with self._command_lock:
            await self._wait_for_slot()
            self._slot_free.clear()
            self._awaiting_created.append(record)
            await self._send_raw(frame)
        watchdog = asyncio.create_task(self._watchdog(record), name="realtime:watchdog")
        self._tasks.add(watchdog)
        watchdog.add_done_callback(self._tasks.discard)
        return record.handle

    async def cancel(self, handle: link.ReplyHandle) -> None:
        """response.cancel, bypassing the queue — waiting to interrupt defeats
        the point. The handle goes stale immediately; the server's done event
        settles the books."""
        handle.stale = True
        await self._send_raw({"type": dia.ClientEvent.RESPONSE_CANCEL.value})

    async def _send_raw(self, frame: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("还没连接")
        await self._ws.send(json.dumps(frame))

    async def _wait_for_slot(self) -> None:
        if not self.caps.single_response_slot:
            return
        await self._slot_free.wait()

    async def _watchdog(self, record: ReplyRecord) -> None:
        # Sleep on the injected clock so tests can hold time still.
        done_task = asyncio.create_task(record.done.wait())
        sleep_task = asyncio.create_task(self._clock.sleep(self._watchdog_s))
        try:
            finished, _ = await asyncio.wait(
                {done_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if done_task in finished:
                return
            # No done inside the window: the reply is presumed wedged. Cancel is
            # the only client-side path that can free a stuck in_response.
            record.handle.stale = True
            await self._send_raw({"type": dia.ClientEvent.RESPONSE_CANCEL.value})
            self._settle(record, link.ReplyStatus.TIMED_OUT)
        finally:
            for task in (done_task, sleep_task):
                task.cancel()
            await asyncio.gather(done_task, sleep_task, return_exceptions=True)

    # ------------------------------------------------------------ receiving

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind, payload = self.codec.normalize(frame)
                if kind is not None:
                    await self._dispatch(kind, payload)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            return

    async def _dispatch(self, kind: dia.ServerEvent, payload: dict[str, Any] | Any) -> None:
        emit = self._events.put_nowait
        if kind is dia.ServerEvent.SPEECH_STARTED:
            emit(link.SpeechStarted(audio_ms=payload.get("audio_start_ms")))
        elif kind is dia.ServerEvent.SPEECH_STOPPED:
            emit(link.SpeechStopped(audio_ms=payload.get("audio_end_ms")))
        elif kind is dia.ServerEvent.RESPONSE_CREATED:
            self._on_created(str((payload.get("response") or {}).get("id")))
        elif kind in (dia.ServerEvent.TEXT_DELTA, dia.ServerEvent.TRANSCRIPT_DELTA):
            record = self._record_for(payload)
            if record is not None and not record.handle.stale:
                delta = str(payload.get("delta") or "")
                record.text.append(delta)
                if len(record.text) == 1:
                    emit(link.ReplyStarted(record.handle))
                emit(link.ReplyTextDelta(record.handle, delta))
        elif kind is dia.ServerEvent.AUDIO_DELTA:
            record = self._record_for(payload)
            if record is not None and not record.handle.stale:
                if not record.text:
                    record.text.append("")
                    emit(link.ReplyStarted(record.handle))
                pcm = base64.b64decode(str(payload.get("delta") or ""))
                emit(link.ReplyAudioDelta(record.handle, pcm))
        elif kind is dia.ServerEvent.FUNCTION_ARGS_DONE:
            record = self._record_for(payload)
            if record is not None and not record.handle.stale:
                emit(
                    link.ToolCall(
                        record.handle,
                        call_id=str(payload.get("call_id") or ""),
                        name=str(payload.get("name") or ""),
                        arguments=str(payload.get("arguments") or ""),
                    )
                )
        elif kind is dia.ServerEvent.TRANSCRIPT_DONE:
            # Audio replies on s2s carry their text as ONE done event with the
            # full transcript and no deltas (upstream handlers/response.py:362,
            # the response_wants_audio branch). Fold it into the record so
            # ReplyDone.text is never empty for a spoken reply. The emptiness
            # check keeps dialects that DO stream transcript deltas (DashScope)
            # from getting the text twice.
            record = self._record_for(payload)
            if record is not None and not record.handle.stale:
                transcript = str(payload.get("transcript") or "")
                if transcript and not "".join(record.text):
                    if not record.text:
                        emit(link.ReplyStarted(record.handle))
                    record.text.append(transcript)
                    emit(link.ReplyTextDelta(record.handle, transcript))
        elif kind is dia.ServerEvent.RESPONSE_DONE:
            self._on_done(payload)
        elif kind is dia.ServerEvent.USER_TRANSCRIPT_DELTA:
            emit(link.UserTranscriptDelta(str(payload.get("delta") or "")))
        elif kind is dia.ServerEvent.USER_TRANSCRIPT_DONE:
            emit(link.UserTranscriptDone(str(payload.get("transcript") or "")))
        elif kind is dia.ServerEvent.ERROR:
            error = payload.get("error") or {}
            emit(link.LinkError(str(error.get("type") or ""), str(error.get("message") or "")))
        # session.updated / item.created / *.done terminators need no upward event.

    def _on_created(self, rid: str) -> None:
        # Serialisation means the next created after our create is ours.
        if self._awaiting_created:
            record = self._awaiting_created.pop(0)
        else:  # a created we never asked for — book it so its frames land somewhere
            record = ReplyRecord(handle=link.ReplyHandle())
        record.rid = rid
        self._replies[rid] = record

    def _record_for(self, payload: dict[str, Any] | Any) -> ReplyRecord | None:
        rid = payload.get("response_id")
        if not isinstance(rid, str):
            return None
        record = self._replies.get(rid)
        if record is None:
            # First frame of a reply that never announced itself: the implicit
            # VAD turn (rule 4). It occupies the slot from its first frame.
            record = ReplyRecord(handle=link.ReplyHandle(), rid=rid)
            self._replies[rid] = record
            self._slot_free.clear()
        return record

    def _on_done(self, payload: dict[str, Any] | Any) -> None:
        response = payload.get("response") or {}
        rid = str(response.get("id") or "")
        status_raw = str(response.get("status") or "completed")
        record = self._replies.pop(rid, None)
        if record is None:
            # A done we cannot match frees the slot anyway: wrong books held
            # open wedge us, not the server (rule 4).
            self._slot_free.set()
            return
        try:
            status = link.ReplyStatus(status_raw)
        except ValueError:
            status = link.ReplyStatus.FAILED
        self._settle(record, status)

    def _settle(self, record: ReplyRecord, status: link.ReplyStatus) -> None:
        if record.done.is_set():
            return
        record.done.set()
        if status is not link.ReplyStatus.COMPLETED:
            record.handle.stale = True
        if record.rid is not None:
            self._replies.pop(record.rid, None)
        if record in self._awaiting_created:
            self._awaiting_created.remove(record)
        self._slot_free.set()
        self._events.put_nowait(link.ReplyDone(record.handle, status, text="".join(record.text)))
