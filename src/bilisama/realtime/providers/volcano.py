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
2. **No per-turn instructions slot.** They ride in the query text. Note this
   does NOT use compose_instructions: that helper exists because the OpenAI
   protocol makes response.instructions REPLACE the session's, and here the
   persona sits in the session config where nothing displaces it. Re-sending
   it every turn would be waste, not safety.
3. **No session cap, but a 10-minute idle timeout** (error 45000003). Our
   uplink never stops — plan section 3.3 rule 7 makes that an invariant, silent
   frames and all — so it is not reachable from here.

There used to be a fourth: "no cancel". That was our misreading, not the
protocol's gap — ClientInterrupt exists and works, see `cancel`.

What this endpoint genuinely cannot do is take a text query on the CURRENT
full-duplex protocol, which is why we stay on this one. Probed 2026-08-27: the
duplex API drops ChatTextQuery entirely (it is absent from the vendor's own
old-to-new event mapping table, ten plausible names all answer "unknown event
name", and a lone user item in conversation.item.create is silently dropped).
Injecting a danmaku and getting a reply she composed herself is the product's
core loop, so the newer protocol cannot serve it. Backlog item 79.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
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
        api_key: str = "",
        app_id: str = "",
        access_key: str = "",
        config: VolcanoConfig,
        clock: Clock | None = None,
        watchdog_s: float = _WATCHDOG_S,
        session_id: str | None = None,
        dialog_id: str = "",
        auto_reconnect: bool = True,
        reconnect_backoff_s: float = 1.0,
        max_reconnect_attempts: int = 6,
    ) -> None:
        """Args:
        api_key: The console's API Key, which authenticates on its own — the
            vendor's own words are 「在任意接口中，填入 header 即可，不用填写
            appid」. Preferred when present.
        app_id: The older pair's first half. Only consulted when no api_key
            was supplied, because the two are alternatives rather than
            complements: probed live 2026-08-27, an API Key put in the
            X-Api-Access-Key position draws 401 "requested grant not found".
        access_key: The older pair's second half, an Access Token.
        session_id: The id we choose for this session — the client picks it,
            not the server. Injectable so tests are not at the mercy of uuid4.
        dialog_id: A conversation to pick back up, from an earlier session's
            SessionStarted. Empty starts a new one. The reconnect ladder fills
            this in for itself; a caller only needs it to resume across a
            process restart.
        """
        self._url = url
        self._api_key = api_key
        self._app_id = app_id
        self._access_key = access_key
        self._cfg = config
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._session_id = session_id or str(uuid.uuid4())
        self._pinned_session_id = session_id is not None
        self._auto_reconnect = auto_reconnect
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_attempts = max_reconnect_attempts

        self._ws: Any = None
        self._recv: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._closing = False

        self._context = ""
        self._started = False
        # Handed back by SessionStarted. Sending it on a later StartSession
        # reloads that conversation — the server keeps the last 20 QA rounds
        # against it — which is what makes a reconnect resume rather than
        # start over. Verified live 2026-08-27: a passphrase set on one
        # connection came back on the next.
        self._dialog_id = dialog_id
        self._attempts = 0
        # What add_context_item was handed, waiting for a request_reply to
        # carry it. There is no wire event that writes one user message on its
        # own: ConversationCreate wants complete QA pairs, an even number of
        # them, which is a different thing entirely.
        self._pending_item = ""

        # One reply at a time, and the model may claim the slot itself after
        # VAD — so this tracks whoever holds it, not just replies we asked for.
        self._active: link.ReplyHandle | None = None
        self._reply_text: list[str] = []
        # Which server-side question the active reply answers, and the ones
        # already finished. Every text and lifecycle frame carries a
        # question_id (ChatResponse, TTSSentenceStart, TTSEnded, ASRInfo), and
        # ChatTextQueryConfirmed hands back the one our own query was given.
        #
        # Without this, a frame arriving after a cancel or a watchdog timeout
        # walked into _ensure_active and minted a GHOST reply: fresh handle,
        # ReplyStarted emitted, the single slot taken again, and nothing to end
        # it but another 25-second timeout. RealtimeClient carries tombstones
        # for exactly this (client.py:143-145); this is the same idea keyed on
        # what this protocol actually gives us.
        self._question = ""
        self._done: deque[str] = deque(maxlen=32)
        # TTSResponse is raw audio with no id of its own, so it inherits the
        # judgement made on the TTSSentenceStart that preceded it.
        self._audio_muted = False
        self._slot_free = asyncio.Event()
        self._slot_free.set()
        self._send_lock = asyncio.Lock()

    @property
    def dialog_id(self) -> str:
        """The conversation this link is on, once a session has started.

        Exposed so a caller can carry it across a process restart — the
        adapter only reuses it within its own reconnect ladder.
        """
        return self._dialog_id

    # ------------------------------------------------------------ lifecycle

    def _headers(self) -> dict[str, str]:
        """Two credential shapes, one of which is going away.

        Probed against the real endpoint 2026-08-27, all four combinations:

        * `x-api-key` alone → 403 「get resource id empty」. The resource id
          is NOT optional just because the key is self-contained.
        * `x-api-key` + resource headers → handshake accepted.
        * an API Key in the X-Api-Access-Key position → 401 「load grant:
          requested grant not found」. They are different credentials, and the
          error says nothing about which — hence preferring one explicitly
          rather than filling in whichever fields happen to be non-empty.
        * the App ID / Access Token pair → the documented older way.
        """
        headers = {
            "X-Api-Resource-Id": _RESOURCE_ID,
            "X-Api-App-Key": _APP_KEY,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        else:
            headers["X-Api-App-ID"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_key
        return headers

    async def connect(self) -> None:
        """Open the socket and climb both session levels.

        Raises:
            SessionRefused: The server declined the connection or the session.
        """
        self._closing = False
        self._attempts = 0
        await self._open()

    async def _open(self) -> None:
        """One attempt at a live session. Shared by connect and the reconnect
        ladder, so a resumed session is assembled exactly like a fresh one —
        the only difference being the dialog_id _session_body puts in.

        Two levels, unlike every other provider here: a connection first, then
        a session inside it. Each is acknowledged, and each acknowledgement is
        waited for — starting a session on a connection the server has not
        confirmed draws an error whose text is about the session.
        """
        # A fresh session id per attempt: it identifies the socket-level
        # session, while dialog_id is what carries the conversation across.
        # Reusing a spent one is asking the server to resume the wrong thing.
        if not self._pinned_session_id:
            self._session_id = str(uuid.uuid4())
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
        resumed = bool(self._dialog_id)
        self._dialog_id = str(frame.json().get("dialog_id", "")) or self._dialog_id
        log.info(
            "volcano.session_started",
            model=self._cfg.model,
            speaker=self._cfg.speaker,
            dialog_id=self._dialog_id,
            resumed=resumed,
            context_len=len(self._context),
        )
        self._recv = asyncio.create_task(self._recv_loop(), name="volcano:recv")
        await self._events.put(link.LinkUp(attempts=self._attempts + 1))

    def _session_body(self) -> dict[str, Any]:
        """The StartSession config: what it hears, what it sounds like, who it is.

        The audio shape is asked for explicitly rather than taken as given:
        the downlink default is Ogg Opus, and every consumer above us wants
        raw PCM.

        Both `extra` objects are always present, even empty. The vendor lists
        「StartSession event payload asr extra is null」 and the tts twin as
        error 42000020 — a null there is a documented refusal, not a default.

        `dialog.extra.model` is documented as required. It also carries real
        meaning: it picks the model generation whose persona key this session
        is about to use, so sending the key without the version is asking two
        different questions.
        """
        dialog: dict[str, Any] = {"extra": {"model": self._cfg.model}}
        if self._context:
            dialog[_PERSONA_KEY[self._cfg.model]] = self._context
        if self._dialog_id:
            # Reconnecting. The server keeps the last 20 QA rounds against this
            # id, so a dropped socket comes back with the conversation intact
            # rather than with amnesia halfway through a stream.
            dialog["dialog_id"] = self._dialog_id
        body: dict[str, Any] = {
            "asr": {"extra": {"end_smooth_window_ms": self._cfg.end_smooth_window_ms}},
            "tts": {
                "extra": {},
                "audio_config": {
                    "channel": 1,
                    "format": "pcm_s16le",
                    "sample_rate": 24000,
                },
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
        for task in (self._watchdog, self._recv, self._reconnect_task):
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
        """Stop her, on the wire and locally.

        This used to be local-only, on the reading that the protocol had no
        cancel. It has one: ClientInterrupt, which the docs qualify with
        「在麦克风按键输入模式下」 — a qualification that turns out not to be a
        restriction. Probed live 2026-08-27 in plain server_vad mode: 94 audio
        frames before the interrupt, one in-flight frame after, arriving 0.05 s
        later. So the tokens stop being spent, not just being played.

        The local settle still happens, and still first: the frames already in
        flight are ours to drop, and marking the handle stale is what both
        audio consumers key on.
        """
        if self._active is None or self._active is not handle:
            return
        self._settle(link.ReplyStatus.CANCELLED)
        try:
            await self._send(
                wire.client_request(
                    wire.ClientEvent.CLIENT_INTERRUPT, session_id=self._session_id, body={}
                )
            )
        except (websockets.ConnectionClosed, OSError, ConnectionError) as exc:
            # A dead socket has already stopped her more thoroughly than this
            # frame would have. Worth a line, not worth raising into a barge-in.
            log.debug("volcano.interrupt_unsent", detail=str(exc)[:120])
            return
        log.info("volcano.interrupted", handle_id=handle.handle_id)

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
        if self._question:
            self._done.append(self._question)
        else:
            # Nothing to tombstone: this reply was settled before the server
            # ever named it, which is what a watchdog timeout on a silent
            # endpoint looks like. A tail arriving later can then still mint an
            # implicit reply — but a server that never named the question is
            # also one that sent no tail, so the two cases do not overlap in
            # practice. Left as a note rather than a guess at a heuristic.
            log.debug("volcano.settled_unnamed", status=status.value)
        self._question = ""
        self._audio_muted = False
        self._active = None
        self._slot_free.set()
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self._events.put_nowait(link.ReplyDone(handle, status, "".join(self._reply_text)))
        self._reply_text = []

    def _ensure_active(self, question: str = "") -> link.ReplyHandle | None:
        """The handle for whatever is speaking, minting one if the model
        started on its own.

        VAD-triggered replies arrive without anyone here having asked, and they
        still need a handle to be cancellable. But a frame from a question we
        already finished is not a new reply — it is the tail of an old one, and
        minting for it is how a cancelled reply came back as a ghost holding
        the slot.

        Returns:
            The handle to attribute this frame to, or None to drop the frame.
        """
        if question and question in self._done:
            return None
        if self._active is None:
            handle = link.ReplyHandle()
            self._take_slot(handle)
            self._question = question
            self._events.put_nowait(link.ReplyStarted(handle))
            log.debug("volcano.reply_implicit", handle_id=handle.handle_id, question=question)
        elif question and not self._question:
            # Our own query, now that the server has named it.
            self._question = question
        return self._active

    async def _reconnect(self) -> None:
        """Climb back, with the conversation intact.

        Bounded on purpose: a ladder that never gives up turns a dead account
        or a revoked key into an infinite quiet retry, and the streamer sees a
        panel that says "connecting" forever. Six tries matches the hosted
        client's budget.

        The session resumes rather than restarts because StartSession carries
        the dialog_id from last time (see _session_body), so she comes back
        knowing what was already said instead of with amnesia mid-stream.
        """
        delay = self._reconnect_backoff_s
        for attempt in range(1, self._max_attempts + 1):
            if self._closing:
                return
            self._attempts = attempt
            await self._clock.sleep(delay)
            delay = min(delay * 2, 30.0)
            try:
                await self._open()
            except (SessionRefused, OSError, websockets.WebSocketException) as exc:
                log.warning(
                    "volcano.reconnect_failed",
                    attempt=attempt,
                    error_text=str(exc)[:160],
                    error_class=type(exc).__name__,
                )
                continue
            log.info("volcano.reconnected", attempt=attempt, dialog_id=self._dialog_id)
            return
        log.error(
            "volcano.reconnect_gave_up",
            attempts=self._max_attempts,
            error_text="重连试满了还是连不上，这条语音链路不会自己回来了。",
        )
        await self._events.put(
            link.LinkDown(reason=f"重连 {self._max_attempts} 次都没成功", retrying=False)
        )

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
            if self._closing:
                return
            # Everything in flight is gone with the socket. Settle first so no
            # reply is left holding the single slot across the gap.
            self._settle(link.ReplyStatus.FAILED)
            await self._events.put(link.LinkDown(reason=str(exc)[:120], retrying=True))
            self._ws = None
            if self._auto_reconnect:
                self._reconnect_task = asyncio.create_task(
                    self._reconnect(), name="volcano:reconnect"
                )

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
        if event == wire.ServerEvent.CHAT_TEXT_QUERY_CONFIRMED:
            # The ack for our own query, and the only place the server tells us
            # which question_id it filed it under.
            self._ensure_active(str(frame.json().get("question_id", "")))
            return
        if event == wire.ServerEvent.CHAT_RESPONSE:
            handle = self._ensure_active(str(frame.json().get("question_id", "")))
            if handle is None:
                return
            text = str(frame.json().get("content", ""))
            if text:
                self._reply_text.append(text)
                self._events.put_nowait(link.ReplyTextDelta(handle, text))
            return
        if event == wire.ServerEvent.TTS_RESPONSE:
            # Raw audio, no id of its own — it inherits the judgement made on
            # the TTSSentenceStart that came before it.
            if self._audio_muted:
                return
            handle = self._ensure_active()
            if handle is not None and frame.payload:
                self._events.put_nowait(link.ReplyAudioDelta(handle, frame.payload))
            return
        if event == wire.ServerEvent.TTS_SENTENCE_START:
            question = str(frame.json().get("question_id", ""))
            self._audio_muted = self._ensure_active(question) is None
            return
        if event == wire.ServerEvent.TTS_ENDED:
            if str(frame.json().get("question_id", "")) in self._done:
                # The tail of a reply we already closed. Settling again would
                # end whatever took the slot after it.
                return
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
