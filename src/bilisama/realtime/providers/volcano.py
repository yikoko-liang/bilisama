"""SpeechLink over Volcengine's end-to-end dialogue model (Doubao S2S).

Why this does not reuse RealtimeClient, which already owns slot accounting, a
watchdog, tombstones and reconnect: that client does `json.loads` on every
inbound frame (client.py:165 and :370) because all three existing providers
speak an OpenAI dialect. This endpoint speaks binary frames with numbered
events. Threading a second wire format through it would dirty both, so the
policies it owns are rewritten here at the size this provider needs, and the
duplication is recorded in the backlog rather than papered over. Two points do
not draw a line — the extraction is worth doing when a third non-OpenAI
provider shows up, not before.

Four places where this protocol has no equivalent of something SpeechLink
assumes. None of them are hidden:

1. **No response.create.** The model answers on its own after VAD. Our
   two-step inject (add_context_item, then request_reply) collapses into one
   ChatTextQuery: the item text is stashed locally and sent as the query.
2. **No cancel.** `cancel()` is local only — the handle goes stale and late
   frames stop being forwarded, so the barge-in the audience hears is correct,
   but the model may still be generating on its side.
3. **No per-turn instructions slot.** They ride in the query text. Note this
   does NOT use compose_instructions: that helper exists because the OpenAI
   protocol makes response.instructions REPLACE the session's, and here the
   persona sits in the session config where nothing displaces it. Re-sending
   it every turn would be waste, not safety.
4. **No session cap, but a 10-minute idle timeout** (error 45000003). Our
   uplink never stops — plan section 3.3 rule 7 makes that an invariant, silent
   frames and all — so it is not reachable from here.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.obs.logging import get_logger
from bilisama.realtime import link
from bilisama.realtime.client import SessionRefused
from bilisama.realtime.providers import volcano_wire as wire

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bilisama.config.schema import VolcanoConfig

__all__ = ["VolcanoLink"]

log = get_logger(__name__)

# Fixed by the vendor's docs for the dialogue resource. Not a secret and not a
# credential — it identifies the API, the way a path would.
_APP_KEY = "PlgvMymc7f3tQnJ6"
_RESOURCE_ID = "volc.speech.dialog"

_WATCHDOG_S = 25.0

# Which key the persona goes under, per model generation. The two are not
# aliases: O2.0 takes a plain instruction string, SC2.0 takes a character
# manifest, and sending one under the other's key is accepted and ignored.
_PERSONA_KEY: dict[str, str] = {"1.2.1.1": "system_role", "2.2.0.0": "character_manifest"}


class VolcanoLink:
    """SpeechLink over the Volcengine realtime dialogue endpoint."""

    def __init__(
        self,
        url: str,
        *,
        app_id: str,
        access_key: str,
        config: VolcanoConfig,
        clock: Clock | None = None,
        watchdog_s: float = _WATCHDOG_S,
        session_id: str | None = None,
    ) -> None:
        """Args:
        session_id: The id we choose for this session — the client picks it,
            not the server. Injectable so tests are not at the mercy of uuid4.
        """
        self._url = url
        self._app_id = app_id
        self._access_key = access_key
        self._cfg = config
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._session_id = session_id or str(uuid.uuid4())

        self._ws: Any = None
        self._recv: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._closing = False

        self._context = ""
        self._started = False
        # What add_context_item was handed, waiting for a request_reply to
        # carry it. There is no wire event that writes one user message on its
        # own: ConversationCreate wants complete QA pairs, an even number of
        # them, which is a different thing entirely.
        self._pending_item = ""

        # One reply at a time, and the model may claim the slot itself after
        # VAD — so this tracks whoever holds it, not just replies we asked for.
        self._active: link.ReplyHandle | None = None
        self._reply_text: list[str] = []
        self._slot_free = asyncio.Event()
        self._slot_free.set()
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-App-ID": self._app_id,
            "X-Api-Access-Key": self._access_key,
            "X-Api-Resource-Id": _RESOURCE_ID,
            "X-Api-App-Key": _APP_KEY,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    async def connect(self) -> None:
        """Open the socket and climb both session levels.

        Two levels, unlike every other provider here: a connection first, then
        a session inside it. Each is acknowledged, and each acknowledgement is
        waited for — starting a session on a connection the server has not
        confirmed draws an error whose text is about the session.

        Raises:
            SessionRefused: The server declined the connection or the session.
        """
        self._closing = False
        # close_timeout bounds the goodbye, not the conversation — same reason
        # RealtimeClient sets it: a wedged server otherwise holds aclose() for
        # the library's 10s default.
        self._ws = await websockets.connect(
            self._url,
            max_size=16 * 1024 * 1024,
            additional_headers=self._headers(),
            close_timeout=2.0,
        )
        await self._send(wire.client_request(wire.ClientEvent.START_CONNECTION))
        await self._expect(
            wire.ServerEvent.CONNECTION_STARTED,
            (wire.ServerEvent.CONNECTION_FAILED,),
            what="连接",
        )
        await self._send(
            wire.client_request(
                wire.ClientEvent.START_SESSION,
                session_id=self._session_id,
                body=self._session_body(),
            )
        )
        frame = await self._expect(
            wire.ServerEvent.SESSION_STARTED,
            (wire.ServerEvent.SESSION_FAILED,),
            what="会话",
        )
        self._started = True
        log.info(
            "volcano.session_started",
            model=self._cfg.model,
            speaker=self._cfg.speaker,
            dialog_id=str(frame.json().get("dialog_id", "")),
            context_len=len(self._context),
        )
        self._recv = asyncio.create_task(self._recv_loop(), name="volcano:recv")
        await self._events.put(link.LinkUp())

    def _session_body(self) -> dict[str, Any]:
        """The StartSession config: what it hears, what it sounds like, who it is.

        The audio shape is asked for explicitly rather than taken as given:
        the downlink default is Ogg Opus, and every consumer above us wants
        raw PCM.
        """
        dialog: dict[str, Any] = {}
        if self._context:
            dialog[_PERSONA_KEY[self._cfg.model]] = self._context
        body: dict[str, Any] = {
            "asr": {"extra": {"end_smooth_window_ms": self._cfg.end_smooth_window_ms}},
            "tts": {
                "audio_config": {
                    "channel": 1,
                    "format": "pcm_s16le",
                    "sample_rate": 24000,
                }
            },
        }
        if self._cfg.speaker:
            body["tts"]["speaker"] = self._cfg.speaker
        if dialog:
            body["dialog"] = dialog
        return body

    async def _expect(
        self,
        wanted: wire.ServerEvent,
        failures: tuple[wire.ServerEvent, ...],
        *,
        what: str,
    ) -> wire.Frame:
        """Read until the handshake step answers, one way or the other.

        Raises:
            SessionRefused: The server sent one of `failures`, closed the
                socket, or said something this step has no meaning for.
        """
        while True:
            try:
                raw = await self._ws.recv()
            except (websockets.ConnectionClosed, OSError) as exc:
                await self._drop_socket()
                raise SessionRefused(
                    code=f"{what}_closed", detail=str(exc)[:120], summary=f"{what}没建起来"
                ) from exc
            try:
                frame = wire.decode(raw if isinstance(raw, bytes) else str(raw).encode())
            except wire.VolcanoProtocolError as exc:
                await self._drop_socket()
                raise SessionRefused(
                    code="not_volcano",
                    detail=str(exc)[:120],
                    summary="对面回的不是火山协议的帧，地址多半指错了",
                ) from exc
            if frame.event == wanted:
                return frame
            if frame.event in failures or frame.kind is wire.MessageKind.ERROR:
                await self._drop_socket()
                body = frame.json()
                raise SessionRefused(
                    code=str(frame.error_code or frame.event or "unknown"),
                    detail=str(body.get("error") or body.get("message") or "")[:200],
                    summary=f"服务端拒了这次{what}",
                )
            # Anything else this early is noise from a step we already passed.
            log.debug("volcano.handshake_skipped", step=what, wire_event=frame.event)

    async def _drop_socket(self) -> None:
        """Close and forget, so a caller that retries does not leak sockets."""
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def aclose(self) -> None:
        self._closing = True
        for task in (self._watchdog, self._recv):
            if task is not None:
                task.cancel()
        if self._ws is not None and self._started:
            # Best effort: a socket already gone is the normal way to arrive
            # here, and failing to say goodbye must not hide the real reason
            # for the teardown.
            try:
                await self._send(
                    wire.client_request(
                        wire.ClientEvent.FINISH_SESSION, session_id=self._session_id
                    )
                )
                await self._send(wire.client_request(wire.ClientEvent.FINISH_CONNECTION))
            except (websockets.ConnectionClosed, OSError) as exc:
                log.debug("volcano.goodbye_skipped", detail=str(exc)[:120])
        await self._drop_socket()
        self._started = False

    # ------------------------------------------------------------- sending

    async def _send(self, frame: bytes) -> None:
        if self._ws is None:
            raise ConnectionError("火山语音连接没开，发不出去。")
        async with self._send_lock:
            await self._ws.send(frame)

    async def set_context(self, instructions: str) -> None:
        """Push the persona.

        Before the session starts this only records it — it goes out inside
        StartSession, where the persona belongs on this protocol. Afterwards it
        needs UpdateConfig, which the vendor documents as a FULL replacement of
        the session config, so the whole body is resent rather than a patch.
        """
        self._context = instructions
        if not self._started:
            return
        await self._send(
            wire.client_request(
                wire.ClientEvent.UPDATE_CONFIG,
                session_id=self._session_id,
                body=self._session_body(),
            )
        )
        log.info("volcano.context_pushed", context_len=len(instructions))

    async def push_audio(self, pcm: bytes) -> None:
        await self._send(wire.audio_request(pcm, session_id=self._session_id))

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        """Stash it. See the module note: there is no wire event for one user
        message, so it travels as the body of the next request_reply."""
        del role  # only "user" is meaningful here, and it is the only caller
        self._pending_item = text

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        """Ask for a reply: the stashed item plus this turn's ask, as one query.

        Blocks until the slot is free — the model claims it itself after VAD,
        so waiting here is not merely bookkeeping. The watchdog is what
        guarantees the wait ends.
        """
        await self._slot_free.wait()
        parts = [part for part in (self._pending_item, spec.instructions) if part]
        self._pending_item = ""
        handle = link.ReplyHandle()
        self._take_slot(handle)
        await self._send(
            wire.client_request(
                wire.ClientEvent.CHAT_TEXT_QUERY,
                session_id=self._session_id,
                body={"content": "\n\n".join(parts)},
            )
        )
        log.info(
            "volcano.reply_requested",
            handle_id=handle.handle_id,
            protected=spec.protected,
            content_len=sum(len(part) for part in parts),
        )
        return handle

    async def cancel(self, handle: link.ReplyHandle) -> None:
        """Drop it on our side. There is no wire event that stops generation.

        The audience hears the right thing — the handle goes stale and both
        audio consumers drop its frames — but the model may keep generating,
        and the tokens are spent either way.
        """
        if self._active is None or self._active is not handle:
            return
        log.info("volcano.cancel_local_only", handle_id=handle.handle_id)
        self._settle(link.ReplyStatus.CANCELLED)

    async def end_protection(self) -> None:
        """No-op, like the hosted adapter's.

        Nothing was disarmed: this protocol has no barge-in switch to turn off.
        A paired no-op keeps the scheduler's lifecycle uniform across adapters.
        """
        return

    # ------------------------------------------------------------ receiving

    def _take_slot(self, handle: link.ReplyHandle) -> None:
        self._active = handle
        self._reply_text = []
        self._slot_free.clear()
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = asyncio.create_task(self._watch(handle), name="volcano:watchdog")

    async def _watch(self, handle: link.ReplyHandle) -> None:
        """The only thing that ends a reply nobody ever ended.

        Without it a lost TTSEnded holds the single slot forever, and every
        later request_reply waits on a slot that is never coming back.
        """
        await self._clock.sleep(self._watchdog_s)
        if self._active is handle:
            log.warning(
                "volcano.reply_timed_out",
                handle_id=handle.handle_id,
                waited_s=self._watchdog_s,
                error_text="等不到回复结束，先把说话名额收回来。",
            )
            self._settle(link.ReplyStatus.TIMED_OUT)

    def _settle(self, status: link.ReplyStatus) -> None:
        """Close the active reply and hand the slot back."""
        handle = self._active
        if handle is None:
            return
        if status is not link.ReplyStatus.COMPLETED:
            # Late frames carrying it stop being forwarded — the same
            # mechanism the other adapters use, and what makes a barge-in
            # sound clean even though nothing on the wire was cancelled.
            handle.stale = True
        self._active = None
        self._slot_free.set()
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self._events.put_nowait(link.ReplyDone(handle, status, "".join(self._reply_text)))
        self._reply_text = []

    def _ensure_active(self) -> link.ReplyHandle:
        """The handle for whatever is speaking, minting one if the model
        started on its own. VAD-triggered replies arrive without anyone here
        having asked, and they still need a handle to be cancellable."""
        if self._active is None:
            handle = link.ReplyHandle()
            self._take_slot(handle)
            self._events.put_nowait(link.ReplyStarted(handle))
            log.debug("volcano.reply_implicit", handle_id=handle.handle_id)
        assert self._active is not None
        return self._active

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    frame = wire.decode(raw if isinstance(raw, bytes) else str(raw).encode())
                except wire.VolcanoProtocolError as exc:
                    # One unreadable frame is not a dead session: log it and
                    # keep reading, the way a dropped video frame is survivable.
                    log.warning(
                        "volcano.frame_unreadable",
                        error_text=str(exc),
                        error_class="VolcanoProtocolError",
                    )
                    continue
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except (websockets.ConnectionClosed, OSError) as exc:
            if not self._closing:
                self._settle(link.ReplyStatus.FAILED)
                await self._events.put(link.LinkDown(reason=str(exc)[:120], retrying=False))

    def _dispatch(self, frame: wire.Frame) -> None:
        """One decoded frame to zero or more normalised events.

        The mapping is the whole point of this adapter: above this line nobody
        knows an event is a number, and L3 sees the same vocabulary it sees
        from every other provider.
        """
        event = frame.event
        if event == wire.ServerEvent.ASR_INFO:
            # The vendor's own words for this one are 「用于打断客户端的播报」 —
            # it IS the barge-in signal. There is no separate speech_started.
            self._events.put_nowait(link.SpeechStarted())
            if self._active is not None:
                self._settle(link.ReplyStatus.CANCELLED)
            return
        if event == wire.ServerEvent.ASR_ENDED:
            self._events.put_nowait(link.SpeechStopped())
            return
        if event == wire.ServerEvent.ASR_RESPONSE:
            self._on_transcript(frame)
            return
        if event == wire.ServerEvent.CHAT_RESPONSE:
            handle = self._ensure_active()
            text = str(frame.json().get("content", ""))
            if text:
                self._reply_text.append(text)
                self._events.put_nowait(link.ReplyTextDelta(handle, text))
            return
        if event == wire.ServerEvent.TTS_RESPONSE:
            handle = self._ensure_active()
            if frame.payload:
                self._events.put_nowait(link.ReplyAudioDelta(handle, frame.payload))
            return
        if event == wire.ServerEvent.TTS_SENTENCE_START:
            self._ensure_active()
            return
        if event == wire.ServerEvent.TTS_ENDED:
            # Audio finished, so the AUDIENCE is finished — which is what
            # "done" has to mean. ChatEnded only says the model stopped
            # generating, and settling there would free the slot while she is
            # still mid-sentence.
            self._settle(link.ReplyStatus.COMPLETED)
            return
        if event == wire.ServerEvent.CHAT_ENDED:
            log.debug("volcano.chat_ended", handle_id=self._handle_id())
            return
        if event in (wire.ServerEvent.DIALOG_COMMON_ERROR, wire.ServerEvent.SESSION_FAILED):
            body = frame.json()
            self._events.put_nowait(
                link.LinkError(
                    code=str(body.get("error_code") or frame.error_code or event),
                    detail=str(body.get("error") or body.get("message") or "")[:200],
                )
            )
            if self._active is not None:
                self._settle(link.ReplyStatus.FAILED)
            return
        log.debug("volcano.event_ignored", wire_event=event)

    def _handle_id(self) -> int:
        return self._active.handle_id if self._active is not None else 0

    def _on_transcript(self, frame: wire.Frame) -> None:
        """What the STREAMER said. Interim results are deltas, final is done."""
        results = frame.json().get("results") or []
        if not isinstance(results, list):
            return
        for item in results:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", ""))
            if not text:
                continue
            if item.get("is_interim"):
                self._events.put_nowait(link.UserTranscriptDelta(text))
            else:
                self._events.put_nowait(link.UserTranscriptDone(text))

    async def events(self) -> AsyncIterator[link.LinkEvent]:
        while True:
            yield await self._events.get()
