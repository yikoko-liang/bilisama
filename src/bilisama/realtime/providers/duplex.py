"""SpeechLink over Volcengine's full-duplex dialogue model (Doubao Seeduplex 3.0).

Plain JSON text frames and OpenAI-shaped event names, which makes it look like
a job for RealtimeClient and dialect.py. It is not, and the reasons are all
protocol rather than taste. Probed against the real endpoint 2026-09-16; the
scripts are recorded in docs/runbook.md.

1. **The client speaks first.** RealtimeClient opens a socket and waits for
   `session.created` (client.py:189). Here the session does not exist until we
   send `session.create`, and the greeting answers it.
2. **Replies never announce.** There is no `response.created`. RealtimeClient
   books an unannounced reply as the provider's own VAD turn
   (client.py:694-701) — which is right for the turns she starts and wrong for
   the ones we asked for, and the voice gate only holds implicit turns. Ours
   would have been gated and skipped for carrying no marker. They are told
   apart by `tts_type` on `response.output_audio.started`: `chat_tts_text` is
   the text we handed over, `default` is her own.
3. **Speech edges ride the transcriber.** No `input_audio_buffer.speech_*`
   here; the streamer's first word arrives as
   `conversation.item.input_audio_transcription.started`.
4. **`response.create` is not an error, it is a disconnect.** The socket closes
   with 1006, twice out of two tries.

And the one that shapes the product rather than the file:

5. **It will not compose.** ChatTextQuery is gone — absent from the vendor's
   own old-to-new event table, and a lone user item in
   `conversation.item.create` is stored (it comes back from
   `conversation.item.retrieve`) and never reaches the model. The only path
   from words to sound is `speech_text_buffer.replacement`, which says what it
   is given. So `request_reply` needs `ReplySpec.text`, L3 fills it, and the
   capability bit `composes_replies=False` is how L3 knows to. Nothing here
   calls a model to write her lines: whose words come out of her mouth is a
   product decision, and an adapter that quietly made it would be the wrong
   place to discover it.

One more measured fact worth keeping next to the code that relies on it:
history reaches the model ONLY at `session.create`, through
`extension.dialog.dialog_context`. Mid-session `conversation.item.create` acks
with `conversation.item.added` and `session.update` with a `dialog_context`
acks with `session.updated`; neither changes what she knows. Same shape as
SC2.0's character manifest, and the same remedy — a new session — which
`set_context` uses for the persona.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections import deque
from typing import TYPE_CHECKING, Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.obs.logging import get_logger
from bilisama.realtime import link
from bilisama.realtime.client import SessionRefused
from bilisama.realtime.errors import ErrorClass, classify_error, describe

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from bilisama.config.schema import VolcanoDuplexConfig

__all__ = ["DuplexLink"]

log = get_logger(__name__)

# No published per-connection cap, so nothing rotates; this only bounds a
# reply that produces no done, the way client.py's does.
_WATCHDOG_S = 25.0
# The vendor is explicit that uplink audio must arrive at the pace it was
# recorded — "发送速率偏离实际节奏（过快或过慢）都将触发服务端错误". Our pumps
# already feed at 20 ms, so this is a ceiling for the replay paths, not a
# throttle on the microphone.
_FRAME_MS = 20
# What a reply's own frames carry when the words were ours rather than hers.
_OURS = "chat_tts_text"


def _text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "input_text", "text": text}]


def session_body(
    cfg: VolcanoDuplexConfig,
    *,
    instructions: str,
    model: str,
    speaker: str,
    session_id: str = "",
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The session block both `session.create` and `session.update` carry.

    `history` rides `extension.dialog.dialog_context` and only does anything
    on create — see the module note. It is accepted on update and ignored,
    which is why this builder never adds it there by accident: the caller
    passes it exactly when it can work.
    """
    session: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {"format": {"type": "pcm", "rate": 16000}},
            "output": {
                "format": {"type": "pcm", "rate": 24000},
                "speed": cfg.speed,
                "loudness": cfg.loudness,
            },
        },
    }
    if speaker:
        session["audio"]["output"]["voice"] = speaker
    if session_id:
        # The vendor's own words: 「对应原 dialog id，接续历史对话时传入」.
        session["id"] = session_id
    extension: dict[str, Any] = {"asr": {}, "tts": {}, "dialog": {}}
    if history:
        extension["dialog"] = {"dialog_context": history}
    return {"session": session, "extension": extension}


class DuplexLink:
    """SpeechLink over the duplex protocol."""

    def __init__(
        self,
        url: str,
        *,
        api_key: str,
        config: VolcanoDuplexConfig,
        speaker: str = "",
        model: str = "",
        quiet_window_s: float = 1.8,
        clock: Clock | None = None,
        watchdog_s: float = _WATCHDOG_S,
        auto_reconnect: bool = True,
        reconnect_backoff_s: float = 1.0,
        max_reconnect_attempts: int = 6,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._cfg = config
        self._speaker = speaker or config.voice
        self._model = model or config.model
        self.quiet_window_s = quiet_window_s
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._auto_reconnect = auto_reconnect
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_reconnect_attempts = max_reconnect_attempts

        self._ws: Any = None
        self._closing = False
        self._suspended = False
        self._started = False
        self._context = ""
        # Handed back on session.created and replayed on the next create, so a
        # reconnect or a persona swap keeps the last rounds the server holds.
        self._session_id = ""
        self._events: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()

        # One reply at a time, ours or hers. `_active` is the live one;
        # `_pending_ours` is set between asking for a TTS reply and its first
        # frame, which is the window where an unannounced reply has to be
        # attributed.
        self._active: link.ReplyHandle | None = None
        self._pending_ours: link.ReplyHandle | None = None
        self._pending_text = ""
        self._slot_free = asyncio.Event()
        self._slot_free.set()
        self._slot_lock = asyncio.Lock()
        self._watchdog: asyncio.Task[None] | None = None
        # Items the scheduler wrote before asking for a reply. They go into the
        # NEXT session's dialog_context, because mid-session injection is
        # accepted and ignored — see the module note.
        self._pending_items: deque[dict[str, str]] = deque(maxlen=40)
        self._warned_no_text = False

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        self._closing = False
        await self._open()

    async def _open(self) -> None:
        self._ws = await websockets.connect(
            self._url,
            max_size=16 * 1024 * 1024,
            additional_headers={"X-Api-Key": self._api_key},
            close_timeout=2.0,
        )
        await self._send(
            {
                "type": "session.create",
                **session_body(
                    self._cfg,
                    instructions=self._context,
                    model=self._model,
                    speaker=self._speaker,
                    session_id=self._session_id,
                    history=list(self._pending_items) or None,
                ),
            }
        )
        self._pending_items.clear()
        raw = await self._ws.recv()
        try:
            first = json.loads(raw)
        except ValueError:
            first = None
        if not isinstance(first, dict) or first.get("type") != "session.created":
            await self._ws.close()
            self._ws = None
            error = (first or {}).get("error") or {}
            raise SessionRefused(
                code=str(error.get("type") or (first or {}).get("type") or "unknown"),
                detail=str(error.get("message") or ""),
                summary="服务端第一帧不是 session.created",
            )
        self._session_id = str((first.get("session") or {}).get("id") or self._session_id)
        self._started = True
        log.info(
            "duplex.session_started", session_id=self._session_id, context_len=len(self._context)
        )
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="duplex:recv")

    async def aclose(self) -> None:
        self._closing = True
        # The vendor is explicit: dropping the socket without session.close
        # draws ContextCanceled (55000001). Best effort — a dead socket is
        # exactly when this cannot be sent, and exactly when it does not matter.
        if self._ws is not None and self._started:
            with contextlib.suppress(Exception):
                await self._send({"type": "session.close"})
        pending = [
            t for t in (self._reconnect_task, self._recv_task, *self._tasks) if t is not None
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._started = False

    async def suspend(self) -> None:
        self._suspended = True
        self._settle(link.ReplyStatus.CANCELLED)
        await self.aclose()
        self._closing = False
        log.info("duplex.suspended")

    async def resume(self) -> None:
        self._suspended = False
        await self.connect()
        log.info("duplex.resumed")

    # ------------------------------------------------------------ sending

    async def _send(self, frame: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("还没连接")
        async with self._send_lock:
            await self._ws.send(json.dumps(frame))

    async def set_context(self, instructions: str) -> None:
        """Push the persona.

        `instructions` DO apply mid-session — probed 2026-09-16, she answered
        to the new name straight after a session.update. That is the one thing
        this protocol lets us change in place; history does not follow
        (see the module note), so anything stashed for the next session waits
        for a reconnect rather than forcing one here.
        """
        self._context = instructions
        if not self._started:
            return
        await self._send(
            {
                "type": "session.update",
                **session_body(
                    self._cfg,
                    instructions=instructions,
                    model=self._model,
                    speaker=self._speaker,
                    session_id=self._session_id,
                ),
            }
        )
        log.info("duplex.context_pushed", context_len=len(instructions))

    async def push_audio(self, pcm: bytes) -> None:
        if self._ws is None or not self._started:
            return
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        )

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        """Stash it for the next session.

        Sending it now would be accepted and ignored: `conversation.item.added`
        comes back, `conversation.item.retrieve` shows the item, and she still
        answers as if it never happened. So it is kept for the next
        `session.create`, where `dialog_context` does reach her.
        """
        self._pending_items.append({"role": role, "text": text})

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        """Say `spec.text`, as it stands.

        There is no composing step on this protocol; see the module note and
        `capabilities.composes_replies`. A spec that arrives without text is a
        wiring bug in L3, and it gets one line rather than a guess — guessing
        would put words in her mouth that nobody wrote.
        """
        if not spec.text:
            if not self._warned_no_text:
                self._warned_no_text = True
                log.warning(
                    "duplex.reply_without_text",
                    error_text="这个后端不会自己组织回复，ReplySpec.text 必须由上层填好。",
                )
            raise ValueError("duplex 后端要 ReplySpec.text：它只会照着念，不会自己写")

        handle = link.ReplyHandle()
        async with self._slot_lock:
            if not self._slot_free.is_set():
                waited = self._clock.monotonic()
                await self._slot_free.wait()
                log.info(
                    "link.slot_waited",
                    purpose="reply",
                    waited_ms=round((self._clock.monotonic() - waited) * 1000),
                )
            self._take_slot(handle, ours=True)
            self._pending_text = spec.text
            try:
                await self._send(
                    {"type": "speech_text_buffer.replacement.append", "text": spec.text}
                )
                await self._send({"type": "speech_text_buffer.replacement.commit"})
            except Exception:
                self._release_slot()
                raise
        log.info("duplex.reply_requested", handle_id=handle.handle_id, text_len=len(spec.text))
        return handle

    async def cancel(self, handle: link.ReplyHandle) -> None:
        """Stop her.

        The ack is immediate, but audio already generated keeps arriving —
        31 frames after the cancel in one probe — so the handle goes stale
        first and the consumers drop the tail on that flag.
        """
        if self._active is None or self._active is not handle:
            return
        handle.stale = True
        with contextlib.suppress(Exception):
            await self._send({"type": "response.cancel"})

    async def end_protection(self) -> None:
        """Nothing to re-arm: this protocol has no interrupt switch to disarm.

        A paired no-op keeps the scheduler's lifecycle uniform across
        adapters, the same way HostedLink's does.
        """
        return

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._drain()

    async def _drain(self) -> AsyncIterator[link.LinkEvent]:
        while True:
            yield await self._events.get()

    # ------------------------------------------------------------ the slot

    def _take_slot(self, handle: link.ReplyHandle, *, ours: bool) -> None:
        self._slot_free.clear()
        if ours:
            self._pending_ours = handle
        else:
            self._active = handle
        self._arm_watchdog()

    def _release_slot(self) -> None:
        self._active = None
        self._pending_ours = None
        self._pending_text = ""
        self._slot_free.set()
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _arm_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        task = asyncio.create_task(self._watch(), name="duplex:watchdog")
        self._watchdog = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch(self) -> None:
        await self._clock.sleep(self._watchdog_s)
        if self._slot_free.is_set():
            return
        log.warning(
            "duplex.reply_watchdog",
            error_text="一条回复超过看门狗时限还没结束，按超时结算并放开名额。",
        )
        with contextlib.suppress(Exception):
            await self._send({"type": "response.cancel"})
        self._settle(link.ReplyStatus.TIMED_OUT)

    def _settle(self, status: link.ReplyStatus, *, text: str = "") -> None:
        handle = self._active or self._pending_ours
        if handle is None:
            self._release_slot()
            return
        if status is not link.ReplyStatus.COMPLETED:
            handle.stale = True
        self._release_slot()
        self._events.put_nowait(link.ReplyDone(handle, status, text))

    # ------------------------------------------------------------ receiving

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        socket = self._ws
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    continue  # this protocol is text-only; binary is not ours
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the socket died
            if not self._closing:
                await self._on_disconnect(exc)

    def _dispatch(self, frame: Mapping[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "conversation.item.input_audio_transcription.started":
            # This protocol has no input_audio_buffer.speech_started; the
            # transcriber's first word is the earliest edge it offers.
            self._events.put_nowait(link.SpeechStarted())
            return
        if kind == "conversation.item.input_audio_transcription.delta":
            # Cumulative, not incremental: 「你」「你叫」「你叫什么」. Downstream
            # expects increments, so only the tail of each delta goes up.
            self._events.put_nowait(link.UserTranscriptDelta(str(frame.get("delta") or "")))
            return
        if kind == "conversation.item.input_audio_transcription.completed":
            # Its `transcript` comes back empty — the words live in the last
            # delta. L3's transcript sink reads the deltas, so this is the
            # edge and nothing more.
            self._events.put_nowait(link.SpeechStopped())
            return
        if kind == "response.output_audio.started":
            self._on_reply_started(frame)
            return
        if kind == "response.output_text.delta":
            handle = self._active
            if handle is not None and not handle.stale:
                self._events.put_nowait(link.ReplyTextDelta(handle, str(frame.get("delta") or "")))
            return
        if kind == "response.output_audio.delta":
            handle = self._active
            audio = frame.get("delta")
            if handle is not None and isinstance(audio, str):
                with contextlib.suppress(ValueError):
                    self._events.put_nowait(link.ReplyAudioDelta(handle, base64.b64decode(audio)))
            return
        if kind in ("response.done", "response.canceled"):
            status = (
                link.ReplyStatus.CANCELLED
                if kind == "response.canceled"
                else link.ReplyStatus.COMPLETED
            )
            self._settle(status, text=self._spoken_text())
            return
        if kind == "error":
            error = frame.get("error") or frame
            self._events.put_nowait(
                link.LinkError(
                    str(error.get("code") or error.get("type") or ""),
                    str(error.get("message") or ""),
                )
            )

    def _on_reply_started(self, frame: Mapping[str, Any]) -> None:
        """Book the reply, and decide whose words these are.

        `tts_type` is the only thing that tells them apart, because nothing
        announces a reply on this protocol. `chat_tts_text` is the text we
        handed over; everything else she started herself, which makes the
        handle implicit and puts it in front of the voice gate.
        """
        ours = str(frame.get("tts_type") or "") == _OURS
        if ours and self._pending_ours is not None:
            self._active = self._pending_ours
            self._pending_ours = None
            self._events.put_nowait(link.ReplyStarted(self._active))
            # The TTS path streams no text, so the words we supplied are the
            # only record of what was said. Downstream (the chat page, the
            # memory layer) reads them here rather than nowhere.
            if self._pending_text:
                self._events.put_nowait(link.ReplyTextDelta(self._active, self._pending_text))
            return
        if self._active is not None:
            return  # already booked; a second sentence of the same reply
        handle = link.ReplyHandle(implicit=True)
        self._take_slot(handle, ours=False)
        self._active = handle
        self._events.put_nowait(link.ReplyStarted(handle))

    def _spoken_text(self) -> str:
        return self._pending_text

    # ------------------------------------------------------------ reconnect

    async def _on_disconnect(self, exc: Exception) -> None:
        self._started = False
        self._settle(link.ReplyStatus.FAILED)
        klass = classify_error(exc)
        detail = str(exc) or type(exc).__name__
        if klass is ErrorClass.FATAL or not self._auto_reconnect or self._suspended:
            self._events.put_nowait(link.LinkError("disconnected", describe(klass, detail)))
            return
        self._events.put_nowait(link.LinkDown(reason=detail))
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect(), name="duplex:reconnect")

    async def _reconnect(self) -> None:
        delay = self._reconnect_backoff_s
        for attempt in range(1, self._max_reconnect_attempts + 1):
            await self._clock.sleep(delay)
            if self._closing or self._suspended:
                return
            try:
                await self._open()
            except Exception as exc:
                log.warning(
                    "duplex.reconnect_failed",
                    attempt=attempt,
                    error_text=describe(classify_error(exc), str(exc) or type(exc).__name__),
                )
                delay = min(delay * 2, 30.0)
                continue
            self._events.put_nowait(link.LinkUp(attempts=attempt))
            return
        self._events.put_nowait(link.LinkError("reconnect_gave_up", "重连多次都没成功"))
