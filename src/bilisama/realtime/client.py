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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.obs.logging import get_logger
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.capabilities import Capabilities
from bilisama.realtime.errors import ErrorClass, classify_error, close_code, describe

__all__ = ["RealtimeClient", "ReplyRecord", "SessionRefused"]

log = get_logger(__name__)

_WATCHDOG_S = 25.0  # plan section 3.3 rule 2


class SessionRefused(ConnectionError):
    """The handshake did not get through.

    Almost always the server's one error frame before its 1008 close — a full
    server refuses at connect, not at response.create (the mock's SESSION_LIMIT
    fault models this). The code survives as an attribute so a CLI can turn
    known refusals into targeted advice instead of a traceback; code and detail
    mirror link.LinkError's field names.
    """

    def __init__(self, code: str, detail: str, *, summary: str = "") -> None:
        """Args:
        summary: What went wrong, in this protocol's own words. The default
            describes the OpenAI-dialect handshake, which is what every caller
            here means — but naming session.created to someone dialing a
            provider that has no such event sends them looking for it.
        """
        text = f"{summary or '服务端第一帧不是 session.created'}：{code}"
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
    # True once a response.cancel went out for this reply, by cancel() or the
    # watchdog. A second cancel would hit whoever holds the slot by then — the
    # bare frame names no reply — so one is all a record ever sends. Keyed here
    # rather than on handle.stale: cancel() marks the handle stale on its first
    # line and the watchdog before its own send, so a stale-based short-circuit
    # would eat the one cancel that had to go.
    cancel_sent: bool = False
    # Monotonic seconds at the moment this reply entered the books. Read only by
    # the log: "first token after 380 ms" and "done after 24 s" are the two
    # numbers a stall investigation starts from, and neither is recoverable
    # afterwards from the events alone.
    opened_at: float = 0.0


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
        auto_reconnect: bool = False,
        reconnect_backoff_s: float = 1.0,
        max_reconnect_attempts: int = 6,
    ) -> None:
        self.caps = caps
        self.codec = codec
        self._url = url
        self._headers = headers or {}
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._auto_reconnect = auto_reconnect
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_reconnect_attempts = max_reconnect_attempts
        # Set by the adapter: replays whatever is per-connection state up
        # there (a bootstrap frame, the session instructions). The client owns
        # the socket and knows nothing about session shape, so restoring one
        # has to be a callback rather than a method here.
        self.on_resume: Callable[[], Awaitable[None]] | None = None
        # aclose() must not look like a failure worth reconnecting from.
        self._closing = False
        self._reconnect_task: asyncio.Task[None] | None = None
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
        self._closing = False
        await self._open_socket()

    async def _open_socket(self) -> None:
        # close_timeout bounds the goodbye, not the conversation: a wedged
        # server otherwise holds aclose() for the library's 10s default —
        # most of the "Ctrl-C then nothing" exit stall.
        self._ws = await websockets.connect(
            self._url,
            max_size=16 * 1024 * 1024,
            additional_headers=self._headers,
            close_timeout=2.0,
        )
        raw = await self._ws.recv()
        try:
            first = json.loads(raw)
        except ValueError:
            first = None
        if not isinstance(first, dict):
            # Not a realtime endpoint at all (wrong port, an echo/HMR socket):
            # its greeting is not even a JSON object. Refuse like any other bad
            # handshake instead of letting JSONDecodeError — or normalize()'s
            # AttributeError on a list — escape as a raw traceback.
            await self._ws.close()
            self._ws = None
            raise SessionRefused(code="not_realtime", detail=str(raw)[:120])
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
        if self._recv_task is not None and not self._recv_task.done():
            # Whatever this socket replaces must stop being read. An orphaned
            # recv task keeps dispatching a dead session's frames into the
            # shared queue, and its own death would run _on_disconnect against
            # the socket we are opening right now — failing ITS records and
            # nulling _ws. Cancelling is enough: _recv_loop treats cancellation
            # as a deliberate teardown and reports nothing.
            self._recv_task.cancel()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="realtime:recv")

    async def aclose(self) -> None:
        self._closing = True
        # The reconnect loop waits with the rest: cancelling without awaiting
        # left a ladder still climbing after aclose returned, which at process
        # exit is a socket nobody owns.
        pending = [
            t for t in (self._reconnect_task, self._recv_task, *self._tasks) if t is not None
        ]
        for task in pending:
            task.cancel()
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
            await self._wait_for_slot("command")
            await self._send_raw(frame)

    async def request_reply(self, frame: dict[str, Any]) -> link.ReplyHandle:
        """Send a response.create and take the slot.

        The frame comes from the adapter (via Codec.response_create), because
        what goes in it — out-of-band or not, text or audio — is provider
        policy, not transport.
        """
        record = ReplyRecord(
            handle=link.ReplyHandle(), ours=True, opened_at=self._clock.monotonic()
        )
        async with self._command_lock:
            await self._wait_for_slot("reply")
            self._take_slot("request")
            self._awaiting_created.append(record)
            try:
                await self._send_raw(frame)
            except Exception:
                # A create that never left must not hold the slot for 25 s.
                # The record may already be gone: a send on a closing socket
                # waits for the close to finish before raising (websockets
                # asyncio/connection.py:873-876), and _on_disconnect settles
                # and clears the books inside that wait. Removing it blindly
                # raised ValueError OVER the real ConnectionClosed, so the
                # scheduler logged "list.remove(x): x not in list" where it
                # should have said the link went down. Same guard as _settle.
                if record in self._awaiting_created:
                    self._awaiting_created.remove(record)
                self._free_slot("create_send_failed")
                raise
        log.debug("link.reply_requested", handle_id=record.handle.handle_id)
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
        if record is None or record.done.is_set() or record.cancel_sent:
            return
        record.cancel_sent = True
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

    async def _wait_for_slot(self, purpose: str) -> None:
        if not self.caps.single_response_slot:
            return
        if self._slot_free.is_set():
            return
        # Only the contended path says anything. This is the whole answer to
        # "the SC thank-you took nine seconds and nothing failed": it sat behind
        # the reply that was already speaking. An uncontended wait is every
        # session.update and every create, which at info would be a flood.
        waited_since = self._clock.monotonic()
        await self._slot_free.wait()
        log.info(
            "link.slot_waited",
            purpose=purpose,
            waited_ms=round((self._clock.monotonic() - waited_since) * 1000),
        )

    def _take_slot(self, reason: str) -> None:
        """Mark the single reply slot busy, logging only a real state flip.

        All three callers can fire while it is already taken — a create whose
        response.created then arrives takes it twice — and a flip that did not
        happen is not worth a line.
        """
        if not self._slot_free.is_set():
            return
        self._slot_free.clear()
        log.info("link.slot_taken", reason=reason)

    def _free_slot(self, reason: str) -> None:
        """Hand the slot back. Guarded for the same reason, and one more:
        _on_disconnect settles every open record at once, and one dead link is
        one flip, not one line per record."""
        if self._slot_free.is_set():
            return
        self._slot_free.set()
        log.info("link.slot_freed", reason=reason)

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
            if not record.cancel_sent:
                record.cancel_sent = True
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
        cause: BaseException | None = None
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
            # close_code, not exc.code: the streamer's report reads
            # "connection_closed:1008", so the number stays — but it comes off
            # the attribute websockets still supports (errors.close_code).
            reason = f"connection_closed:{close_code(exc)}"
            cause = exc
        # The link died underneath us. Silence here used to freeze the whole
        # consumer stack (C4/A9): the active reply never settles, the floor
        # stays pending, and nobody upstairs hears a word about it.
        self._on_disconnect(reason, cause)

    def _on_disconnect(self, reason: str, cause: BaseException | None = None) -> None:
        """Fail every open record, free the slot, tell the consumer.

        The books are cleared BEFORE anyone hears about it, so a consumer that
        reacts to LinkDown by draining its queue is looking at a settled world.
        """
        for record in (*self._replies.values(), *self._awaiting_created):
            record.handle.stale = True
            self._settle(record, link.ReplyStatus.FAILED)
        self._awaiting_created.clear()
        self._free_slot("link_down")
        self._ws = None
        if self._closing:
            return
        cls = classify_error(cause) if cause is not None else ErrorClass.RETRYABLE
        retrying = self._auto_reconnect and cls is not ErrorClass.FATAL
        self._events.put_nowait(link.LinkDown(reason, retrying=retrying))
        if not retrying:
            if cls is ErrorClass.FATAL:
                log.error("link.fatal", error_text=describe(cls, reason))
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            # A ladder is already climbing, and this drop is most likely ITS
            # own fresh socket dying inside the resume window (_reconnect_loop
            # reopens, then replays the session). A second loop would race the
            # first: two sockets against one set of books, and whichever dies
            # later settles the survivor's records and nulls _ws, leaving a
            # live session aclose cannot reach. The running loop notices the
            # socket is gone and keeps climbing.
            log.debug("link.reconnect_already_running", reason=reason)
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="realtime:reconnect"
        )

    async def rotate(self, reason: str = "session_cap") -> None:
        """Retire this socket and come back on a fresh one.

        Break-before-make on purpose (see module note). The in-flight books go
        through the same settle path a real drop takes, so callers see one
        vocabulary for both: every reply FAILED, then LinkDown, then LinkUp.
        Cheaper than a second code path, and it is the path already under
        test.
        """
        ws = self._ws
        self._ws = None
        if self._recv_task is not None:
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)
            self._recv_task = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        log.info("link.rotating", reason=reason)
        # Settles records, frees the slot, emits LinkDown, starts the loop that
        # reopens and resumes. _closing stays False, so this is a reconnect.
        self._on_disconnect(f"rotate:{reason}")

    async def _reconnect_loop(self) -> None:
        """Reopen the socket, restore the session, say so — or give up loudly.

        Backoff runs on the injected clock so a test can walk a whole retry
        ladder without waiting. The same shape as SupervisedSource
        (ingest/sources.py): bounded attempts, exponential delay, and a give-up
        that is visible rather than silent.
        """
        delay = self._reconnect_backoff_s
        for attempt in range(1, self._max_reconnect_attempts + 1):
            await self._clock.sleep(delay)
            if self._closing:
                return
            try:
                await self._open_socket()
                if self.on_resume is not None:
                    await self.on_resume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cls = classify_error(exc)
                log.warning(
                    "link.reconnect_failed",
                    attempt=attempt,
                    error_class=str(cls),
                    error_text=str(exc)[:200],
                )
                if cls is ErrorClass.FATAL:
                    self._events.put_nowait(link.LinkDown(str(exc)[:120], retrying=False))
                    log.error("link.fatal", error_text=describe(cls, str(exc)[:120]))
                    return
                # A busy backend deserves a longer wait than a dropped packet.
                delay = min(delay * (4.0 if cls is ErrorClass.BACKOFF else 2.0), 30.0)
                continue
            if self._ws is None:
                # The socket we just opened died again without raising here —
                # its recv task ran _on_disconnect, which deferred to this loop
                # instead of starting a second one. Announcing success now
                # would leave the link down with nobody climbing. No await
                # between the check and the LinkUp below, so this cannot race
                # a drop into the gap.
                log.warning("link.reconnect_died_on_arrival", attempt=attempt)
                delay = min(delay * 2.0, 30.0)
                continue
            log.info("link.reconnected", attempt=attempt)
            self._events.put_nowait(link.LinkUp(attempts=attempt))
            return
        log.error("link.reconnect_gave_up", attempts=self._max_reconnect_attempts)
        self._events.put_nowait(
            link.LinkDown(f"重连 {self._max_reconnect_attempts} 次都没成功", retrying=False)
        )

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
                    self._note_first_frame(record, "text")
                    emit(link.ReplyStarted(record.handle))
                emit(link.ReplyTextDelta(record.handle, delta))
        elif kind is dia.ServerEvent.AUDIO_DELTA:
            record = self._record_for(payload)
            if record is not None and not record.handle.stale:
                if not record.text:
                    record.text.append("")
                    self._note_first_frame(record, "audio")
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
                        self._note_first_frame(record, "transcript")
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

    def _note_first_frame(self, record: ReplyRecord, frame_kind: str) -> None:
        """The one line the frame stream is allowed.

        Deltas arrive every 20 ms and must never be logged one by one, so the
        FIRST one carries the whole question: did this reply ever start
        speaking, and how long after the create did it take to.
        """
        log.debug(
            "link.reply_first_frame",
            handle_id=record.handle.handle_id,
            rid=record.rid,
            frame_kind=frame_kind,
            first_frame_ms=round((self._clock.monotonic() - record.opened_at) * 1000),
        )

    def _on_created(self, rid: str) -> None:
        # Any created occupies the single slot, announced-by-us or not: hosted
        # dialects DO announce their implicit VAD replies, and leaving those
        # unbooked let an explicit create race straight into a rejection (C1).
        self._take_slot("response_created")
        # Serialisation means the next created after our create is USUALLY
        # ours. An implicit created can interleave and steal the pairing, so
        # this stays FIFO; the floor holding injections during speech (stage-3
        # fix) shrinks the overlap window to near zero. Residual risk recorded
        # in the backlog (item 23) — it misattributes events, never the slot.
        #
        # The protocol does define a discriminator we could use one day:
        # response.conversation_id is null for a conversation="none" reply and
        # an id for a VAD-triggered one (openai/types/beta/realtime/
        # realtime_response.py:19-27), so an implicit created would be
        # recognisable on the s2s path, where every create of ours is
        # out-of-band. It is not wired here because no live endpoint has been
        # checked for it, and a server that fills the field in for out-of-band
        # replies too would leave our record unpaired until the 25 s watchdog —
        # trading a rare misattribution for a routine stall. Probe first.
        if self._awaiting_created:
            record = self._awaiting_created.pop(0)
        else:  # a created we never asked for — book it so its frames land somewhere
            record = ReplyRecord(
                handle=link.ReplyHandle(implicit=True), opened_at=self._clock.monotonic()
            )
        record.rid = rid
        self._replies[rid] = record
        # `ours` separates our create from the streamer's own VAD turn, which is
        # the first thing to check when a reply's text landed on the wrong
        # intent — the FIFO pairing above can be stolen by an implicit created.
        log.debug(
            "link.reply_created",
            rid=rid,
            handle_id=record.handle.handle_id,
            ours=record.ours,
            ack_ms=round((self._clock.monotonic() - record.opened_at) * 1000),
        )

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
            record = ReplyRecord(
                handle=link.ReplyHandle(implicit=True), rid=rid, opened_at=self._clock.monotonic()
            )
            self._replies[rid] = record
            self._take_slot("implicit_frame")
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
            self._free_slot("unmatched_done")
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
        self._free_slot("reply_done")
        # The transport's own end of the reply, not the intent's — the scheduler
        # already writes one scheduler.verdict per intent. This one answers the
        # questions that verdict cannot: which wire id, how long the server took,
        # and whether the status came from the server or from our watchdog.
        log.debug(
            "link.reply_done",
            handle_id=record.handle.handle_id,
            rid=record.rid,
            status=status.value,
            elapsed_ms=round((self._clock.monotonic() - record.opened_at) * 1000),
            reply_chars=sum(len(chunk) for chunk in record.text),
        )
        self._events.put_nowait(link.ReplyDone(record.handle, status, text="".join(record.text)))
