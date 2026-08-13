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
import contextlib
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.capabilities import Capabilities

__all__ = ["RealtimeClient", "ReplyRecord", "SessionRefused"]

_WATCHDOG_S = 25.0  # plan section 3.3 rule 2


class SessionRefused(ConnectionError):
    """The handshake's first frame was not session.created.

    Almost always the server's one error frame before its 1008 close — a full
    server refuses at connect, not at response.create (the mock's SESSION_LIMIT
    fault models this). The code survives as an attribute so a CLI can turn
    known refusals into targeted advice instead of a traceback; code and detail
    mirror link.LinkError's field names.
    """

    def __init__(self, code: str, detail: str) -> None:
        text = f"服务端第一帧不是 session.created：{code}"
        if detail:
            text += f"（{detail}）"
        super().__init__(text)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class ReplyRecord:
    """Client-side books for one reply, ours or the server's own."""

    handle: link.ReplyHandle
    rid: str | None = None  # learned from response.created, or first frame
    ours: bool = False
    text: list[str] = field(default_factory=list)
    # True once streamed text deltas arrived: transcript.done fragments are
    # then redundant (the beta dialect sends both) and must not double the text.
    saw_stream_text: bool = False
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
        # Settled response ids. A watchdog-abandoned reply's late frames used
        # to re-book as a fresh "implicit" record — a ghost reply with a new
        # handle that the docstring's tombstone promise was supposed to stop.
        self._tombstones: deque[str] = deque(maxlen=64)
        self._tombstone_set: set[str] = set()

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        # close_timeout bounds the goodbye, not the conversation: a wedged
        # server otherwise holds aclose() for the library's 10s default —
        # most of the "Ctrl-C then nothing" exit stall.
        self._ws = await websockets.connect(
            self._url,
            max_size=16 * 1024 * 1024,
            additional_headers=self._headers,
            close_timeout=2.0,
        )
        first = json.loads(await self._ws.recv())
        kind, _ = self.codec.normalize(first)
        if kind is not dia.ServerEvent.SESSION_CREATED:
            # A refused handshake leaves nothing worth keeping: close before
            # raising so a caller that catches and retries does not leak sockets.
            await self._ws.close()
            self._ws = None
            error = first.get("error") or {}
            raise SessionRefused(
                code=str(error.get("type") or first.get("type") or "unknown"),
                detail=str(error.get("message") or ""),
            )
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
            try:
                await self._send_raw(frame)
            except Exception:
                # A create that never left must not hold the slot for 25 s.
                self._awaiting_created.remove(record)
                self._slot_free.set()
                raise
        watchdog = asyncio.create_task(self._watchdog(record), name="realtime:watchdog")
        self._tasks.add(watchdog)
        watchdog.add_done_callback(self._tasks.discard)
        return record.handle

    async def cancel(self, handle: link.ReplyHandle) -> None:
        """response.cancel, bypassing the queue — waiting to interrupt defeats
        the point. The handle goes stale immediately; the server's done event
        settles the books.

        A bare cancel kills whoever holds the slot NOW, so when this handle's
        reply has already ended (the done raced us), sending would murder an
        innocent — typically the streamer's own implicit reply. Skip it.
        """
        handle.stale = True
        record = self._record_of(handle)
        # Settled records are popped from the books, so "gone" IS "ended".
        if record is None or record.done.is_set():
            return
        await self._send_raw({"type": dia.ClientEvent.RESPONSE_CANCEL.value})

    def _record_of(self, handle: link.ReplyHandle) -> ReplyRecord | None:
        for record in (*self._replies.values(), *self._awaiting_created):
            if record.handle is handle:
                return record
        return None

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
            # the only client-side path that can free a stuck in_response. A
            # dead socket must not eat the TIMED_OUT settlement (A9): the local
            # books free either way.
            record.handle.stale = True
            with contextlib.suppress(ConnectionError, OSError, websockets.ConnectionClosed):
                await self._send_raw({"type": dia.ClientEvent.RESPONSE_CANCEL.value})
            self._settle(record, link.ReplyStatus.TIMED_OUT)
        finally:
            for task in (done_task, sleep_task):
                task.cancel()
            await asyncio.gather(done_task, sleep_task, return_exceptions=True)

    # ------------------------------------------------------------ receiving

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        reason = "connection_closed"
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind, payload = self.codec.normalize(frame)
                if kind is not None:
                    await self._dispatch(kind, payload)
        except asyncio.CancelledError:
            return  # aclose(): a deliberate teardown needs no failure theatre
        except websockets.ConnectionClosed as exc:
            reason = f"connection_closed:{exc.code}"
        # The link died underneath us. Silence here used to freeze the whole
        # consumer stack (C4/A9): the active reply never settles, the floor
        # stays pending, and nobody upstairs hears a word about it.
        self._on_disconnect(reason)

    def _on_disconnect(self, reason: str) -> None:
        """Fail every open record, free the slot, tell the consumer."""
        for record in (*self._replies.values(), *self._awaiting_created):
            record.handle.stale = True
            self._settle(record, link.ReplyStatus.FAILED)
        self._slot_free.set()
        self._events.put_nowait(link.LinkError("connection_lost", reason))

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
                record.saw_stream_text = True
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
            # Audio replies on s2s carry their text as transcript.done events —
            # one PER LLM CHUNK, never as deltas (upstream handlers/response.py
            # :362, the response_wants_audio branch; probed live: seven
            # fragments for one reply). Fold every fragment in, so
            # ReplyDone.text carries the whole utterance. saw_stream_text keeps
            # dialects that DO stream deltas (DashScope sends deltas plus one
            # final done) from getting the text twice.
            record = self._record_for(payload)
            if record is not None and not record.handle.stale and not record.saw_stream_text:
                fragment = str(payload.get("transcript") or "")
                if fragment:
                    if not record.text:
                        emit(link.ReplyStarted(record.handle))
                    record.text.append(fragment)
                    emit(link.ReplyTextDelta(record.handle, fragment))
        elif kind is dia.ServerEvent.RESPONSE_DONE:
            self._on_done(payload)
        elif kind is dia.ServerEvent.USER_TRANSCRIPT_DELTA:
            emit(link.UserTranscriptDelta(str(payload.get("delta") or "")))
        elif kind is dia.ServerEvent.USER_TRANSCRIPT_DONE:
            emit(link.UserTranscriptDone(str(payload.get("transcript") or "")))
        elif kind is dia.ServerEvent.ERROR:
            error = payload.get("error") or {}
            emit(link.LinkError(str(error.get("type") or ""), str(error.get("message") or "")))
            # A rejected create gets neither created nor done — without this,
            # the slot we cleared for it stays shut for the full 25 s watchdog
            # (C5). Rule 4's spirit: wrong books wedge US, so free them. On
            # this wire an error while a create is pending is overwhelmingly
            # its rejection; a benign error costing one early FAILED settle is
            # the cheaper mistake.
            if self._awaiting_created:
                record = self._awaiting_created.pop(0)
                self._settle(record, link.ReplyStatus.FAILED)
        # session.updated / item.created / *.done terminators need no upward event.

    def _on_created(self, rid: str) -> None:
        # Any created occupies the single slot, announced-by-us or not: hosted
        # dialects DO announce their implicit VAD replies, and leaving those
        # unbooked let an explicit create race straight into a rejection (C1).
        self._slot_free.clear()
        # Serialisation means the next created after our create is USUALLY
        # ours. An implicit created can interleave and steal the pairing —
        # the wire carries nothing to match on, so this stays FIFO; the floor
        # holding injections during speech (stage-3 fix) shrinks the overlap
        # window to near zero. Residual risk recorded in the backlog.
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
        if rid in self._tombstone_set:
            return None  # a settled reply's late frames stay dead (C6)
        record = self._replies.get(rid)
        if record is None:
            # First frame of a reply that never announced itself: the implicit
            # VAD turn (rule 4). It occupies the slot from its first frame.
            record = ReplyRecord(handle=link.ReplyHandle(), rid=rid)
            self._replies[rid] = record
            self._slot_free.clear()
        return record

    def _bury(self, rid: str) -> None:
        if rid in self._tombstone_set:
            return
        if len(self._tombstones) == self._tombstones.maxlen:
            self._tombstone_set.discard(self._tombstones[0])
        self._tombstones.append(rid)
        self._tombstone_set.add(rid)

    def _on_done(self, payload: dict[str, Any] | Any) -> None:
        response = payload.get("response") or {}
        rid = str(response.get("id") or "")
        status_raw = str(response.get("status") or "completed")
        if rid:
            self._bury(rid)
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
            self._bury(record.rid)
            self._replies.pop(record.rid, None)
        if record in self._awaiting_created:
            self._awaiting_created.remove(record)
        self._slot_free.set()
        self._events.put_nowait(link.ReplyDone(record.handle, status, text="".join(record.text)))
