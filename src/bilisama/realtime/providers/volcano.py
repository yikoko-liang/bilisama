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
import contextlib
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

import websockets

from bilisama.clock import Clock, SystemClock
from bilisama.obs.logging import get_logger
from bilisama.realtime import link
from bilisama.realtime.client import SessionRefused
from bilisama.realtime.errors import ErrorClass, classify_error, describe
from bilisama.realtime.providers import volcano_wire as wire

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from bilisama.config.schema import VolcanoConfig

__all__ = ["VolcanoLink"]

log = get_logger(__name__)


_WATCHDOG_S = 25.0

# A session swap is 114 ms against the real endpoint (measured 2026-08-28).
# Ten seconds is not a tuning knob, it is a ceiling on how long a context
# update may hold before it reports that it did not land.
_SWAP_TIMEOUT_S = 10.0

# Which key the persona goes under, per model generation. The two are not
# aliases: O2.0 takes a plain instruction string, SC2.0 takes a character
# manifest, and sending one under the other's key is accepted and ignored.
_PERSONA_KEY: dict[str, str] = {"1.2.1.1": "system_role", "2.2.0.0": "character_manifest"}


def _error_detail(body: Mapping[str, Any]) -> str:
    """The server's own words about a failure, trimmed.

    Three call sites read this same shape, and the vendor is not consistent
    about which of the two keys it fills.
    """
    return str(body.get("error") or body.get("message") or "")[:200]


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
        bot_name: str = "",
        speaker: str = "",
        model: str = "",
        quiet_window_s: float = 1.8,
        clock: Clock | None = None,
        watchdog_s: float = _WATCHDOG_S,
        swap_timeout_s: float = _SWAP_TIMEOUT_S,
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
        bot_name: What she calls herself. Its own field because on this
            protocol the NAME does not reliably come from the persona text:
            probed 2026-08-28, our real 557-character persona opens by naming
            her and she answered 「豆包」 three times out of three,
            because dialog.bot_name defaults to 豆包 and a structured document
            dilutes the one sentence that names her. A 31-character persona
            saying the same thing DID win, which is why this looked fine in
            early testing. O generation only — SC takes the name from its
            character manifest.
        model: The model generation, overriding the config's. Empty keeps it.
            It decides which key the persona travels under and which voice
            family is legal, so a swallowed override is two silent failures.
        speaker: The voice, overriding the config's. Empty keeps it. The
            model/generation pairing is judged by the factory on whichever of
            the two wins, because both ways of getting it wrong are silent on
            this endpoint.
        quiet_window_s: How long the speaking floor holds after the streamer
            stops. Handed in rather than computed here: the s2s parameters that
            shape the equivalent number live in another process's launch file,
            so one caller assembling it for every adapter is the only shape
            that works for all four.
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
        self._bot_name = bot_name
        # `--voice` beats the config, and the factory has already judged the
        # pairing on this effective value rather than on the config's.
        self._speaker = speaker or config.speaker
        # Same story as the voice: `--model` lands on the resolved endpoint, and
        # reading `config.model` here meant the banner and the logs said one
        # generation while the wire carried the other — including the field that
        # decides which key the persona goes under.
        self._model = model or config.model
        self.quiet_window_s = quiet_window_s
        self._clock: Clock = clock or SystemClock()
        self._watchdog_s = watchdog_s
        self._swap_timeout_s = swap_timeout_s
        # Only the FIRST session on each socket can use a pinned id: a swap
        # has to mint a new one, because the server has retired the old and one
        # connection holds one session at a time. Injectable so tests are not
        # at the mercy of uuid4.
        self._fixed_session_id = session_id or ""
        self._session_id = self._fixed_session_id or str(uuid.uuid4())
        self._auto_reconnect = auto_reconnect
        self._reconnect_backoff_s = reconnect_backoff_s
        self._max_attempts = max_reconnect_attempts

        self._ws: Any = None
        self._recv: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._swap_task: asyncio.Task[None] | None = None
        # Set when SessionStarted lands. A swap sends StartSession while the
        # receive loop owns the socket, so it cannot read the reply itself —
        # without waiting here the next frame goes to a session the server
        # has not built yet, which passes on an idle endpoint and fails
        # under load.
        self._session_ready = asyncio.Event()
        # Set when SessionFinished lands. One connection holds ONE session:
        # starting the next before the server has retired the last draws
        # 「session number limit exceeded: 1」, and the swap then times out
        # with the old persona still in place. Measured 2026-08-28 — the
        # hand probe that made a swap look easy had waited for this event
        # without anyone noticing it mattered.
        self._session_closed = asyncio.Event()
        self._watchdog: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._closing = False
        # The pause gate. Unlike _closing it is reversible: suspend() sets it
        # so the receive loop's death does not read as a lost link, resume()
        # clears it and reconnects.
        self._suspended = False
        # Said once per link, not per event turn: see request_reply.
        self._warned_base_instructions = False

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
        # A context change that arrived mid-reply, waiting for the slot.
        # SC only: it is the generation whose persona cannot be updated in
        # place, so a change there costs a new session.
        self._swap_pending = False

        # One reply at a time, and the model may claim the slot itself after
        # VAD — so this tracks whoever holds it, not just replies we asked for.
        self._active: link.ReplyHandle | None = None
        self._reply_text: list[str] = []
        # Queries of ours that were settled before the server named them. The
        # ack is still coming, and tombstoning the id it carries is exact —
        # guessing from timing would not be.
        self._orphan_queries = 0
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
        # TTSResponse is raw audio with no id of its own, so it belongs to the
        # sentence announced before it. Scoped to that sentence and no further:
        # a single link-wide "muted" flag stayed set until the next settle, so
        # one late frame from a reply we had closed silenced whatever was
        # speaking now, for its whole duration.
        self._audio_question = ""
        # Uplink frames thrown away because no session was live to send them to.
        # Counted rather than silent: a swap that starts costing seconds should
        # show up as a number, not as the streamer sounding clipped.
        self._dropped_uplink = 0
        self._slot_free = asyncio.Event()
        self._slot_free.set()
        self._send_lock = asyncio.Lock()
        # Held across the wait AND the take. Event.set() wakes every waiter and
        # each then clears the event on its own, so without this two callers
        # both walked out holding the one slot — and the loser's watchdog had
        # been cancelled by the winner, so it never settled at all.
        # client.py:254-257 holds _command_lock for the same reason.
        self._slot_lock = asyncio.Lock()
        # One swap at a time, and only when there is something new to carry.
        # None marks the on-wire session as stale regardless of context text
        # (a rename does this): None never equals a str, so the dedupe below
        # cannot swallow the swap.
        self._swap_lock = asyncio.Lock()
        self._swapped_context: str | None = ""

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
        headers = {**wire.resource_headers(), "X-Api-Connect-Id": str(uuid.uuid4())}
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
        self._session_id = self._fixed_session_id or str(uuid.uuid4())
        # The previous socket's reader has no socket left to read. Leaving it
        # around means two receive tasks racing to dispatch onto one adapter
        # the moment a reconnect succeeds — client.py:193-200 cancels its own
        # for the same reason.
        #
        # No test reaches this today, and the reason is worth writing down
        # rather than pretending otherwise: every path into `_open` goes
        # through `_drop_socket` first, so the old reader's `async for` has
        # already ended and this is a no-op. It is three lines of depth
        # against a future path that does not close the socket first.
        if self._recv is not None and not self._recv.done():
            self._recv.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv
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
        # This session was built with the context in hand, so a swap has
        # nothing to carry until someone changes it. Any query settled before
        # the old session died is gone with it — its ack is never coming.
        self._swapped_context = self._context
        self._orphan_queries = 0
        resumed = bool(self._dialog_id)
        self._dialog_id = str(frame.json().get("dialog_id", "")) or self._dialog_id
        log.info(
            "volcano.session_started",
            model=self._model,
            speaker=self._speaker,
            dialog_id=self._dialog_id,
            resumed=resumed,
            context_len=len(self._context),
        )
        # Live: push_audio may address it again. Cleared for the duration of a
        # swap, when it is briefly true that no session exists.
        self._session_ready.set()
        self._recv = asyncio.create_task(self._recv_loop(), name="volcano:recv")
        # No LinkUp here. It means "the link came BACK", which is what
        # client.py:498 uses it for and what dev_talk prints 「已恢复」 on —
        # emitting it from the shared _open announced a recovery on every
        # single startup, twice into the panel feed. The reconnect ladder
        # emits it, with the attempt number it actually took.

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
        dialog: dict[str, Any] = {"extra": {"model": self._model}}
        if self._context:
            dialog[_PERSONA_KEY[self._model]] = self._context
        # O generation only. The vendor documents bot_name as 「只针对O版本生效」
        # and SC reads the name out of its character manifest, so sending it
        # there would put a field in the frame that does nothing — and a field
        # that does nothing is one the next reader assumes does something.
        if self._bot_name and _PERSONA_KEY[self._model] == "system_role":
            dialog["bot_name"] = self._bot_name
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
        if self._speaker:
            body["tts"]["speaker"] = self._speaker
        # Unconditional: `dialog` always carries at least extra.model, which is
        # a required parameter. The `if dialog:` that used to guard this read
        # as though the section were optional.
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
                    detail=_error_detail(body),
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

    async def suspend(self) -> None:
        """The pause gate's link half: park everything, keep the dialogue.

        aclose() is off limits here — its `_closing` is permanent on this
        adapter — so this is the same teardown with the finality left out:
        tasks parked, slot settled, a polite goodbye, socket dropped. The
        dialog_id survives, and resume()'s StartSession carries it, so the
        server hands the conversation back (it keeps 20 QA rounds).
        """
        self._suspended = True
        pending = [
            task
            for task in (self._watchdog, self._reconnect_task, self._swap_task)
            if task is not None
        ]
        for task in pending:
            task.cancel()
        self._reconnect_task = None
        self._swap_task = None
        self._watchdog = None
        self._swap_pending = False
        # A reply still in flight was already panic-killed by the pause
        # sequence upstairs; CANCELLED frees the slot without reading as a
        # provider failure.
        self._settle(link.ReplyStatus.CANCELLED)
        # The uplink gate: frames arriving mid-pause drop at the counter
        # instead of raising into the microphone pump.
        self._session_ready.clear()
        if self._ws is not None and self._started:
            try:
                await self._send(
                    wire.client_request(
                        wire.ClientEvent.FINISH_SESSION, session_id=self._session_id
                    )
                )
                await self._send(wire.client_request(wire.ClientEvent.FINISH_CONNECTION))
            except (ConnectionError, websockets.ConnectionClosed, OSError) as exc:
                log.debug("volcano.goodbye_skipped", detail=str(exc)[:120])
        self._started = False
        if self._recv is not None:
            self._recv.cancel()
            pending.append(self._recv)
        await self._drop_socket()
        await asyncio.gather(*pending, return_exceptions=True)
        log.info("volcano.suspended", dialog_id=self._dialog_id)

    async def resume(self) -> None:
        """Reopen after suspend(). connect() rebuilds both session levels and
        _session_body carries the surviving dialog_id, so this is the same
        continuation the reconnect ladder performs — deliberately, not by
        luck."""
        self._suspended = False
        await self.connect()
        log.info("volcano.resumed", dialog_id=self._dialog_id)

    async def set_bot_name(self, name: str) -> None:
        """Rename her without a reconnect.

        O generation: probed live 2026-08-29 — an UpdateConfig carrying
        dialog.bot_name is ACCEPTED AND IGNORED (asked her name right after,
        she answered the old one), so the only carrier that works mid-stream
        is the next StartSession. Swap sessions instead: same socket, same
        dialog_id, the conversation carries over. The stale marker forces the
        swap even when the persona text itself is unchanged — a rename with
        identical context would otherwise hit the swap dedupe and never leave.
        SC generation carries the name inside the character manifest itself,
        and the persona refresh that renamed her pushes that manifest through
        set_context — which swaps on that generation anyway.
        """
        if name == self._bot_name:
            return
        self._bot_name = name
        if not self._started:
            return  # the next StartSession carries it
        if _PERSONA_KEY[self._model] == "system_role":
            # Under the swap lock: an in-flight swap writes _swapped_context
            # on completion, and a marker set outside the lock is overwritten
            # by exactly that write — the queued rename then loses to the
            # dedupe it was meant to defeat.
            async with self._swap_lock:
                self._swapped_context = None
            await self._swap_session()

    async def aclose(self) -> None:
        """Say goodbye, then stop reading — in that order.

        The reverse looks tidier and breaks the socket: FinishSession draws a
        SessionFinished, and a reply arriving while the receive task is being
        cancelled leaves the connection closing under the next send. The mock
        did not answer that goodbye until it was taught to, and the bug was
        invisible for as long as it did not.

        Everything cancelled here is also AWAITED here. `client.py:206-214`
        records why: cancelling without awaiting left a ladder still climbing
        after aclose returned, which at process exit is a socket nobody owns.
        The window is real on this adapter — a reconnect sitting just past
        `websockets.connect` would assign the new socket back over the one we
        just dropped, set `_started`, and start a receive task, all after we
        said we were done.
        """
        self._closing = True
        # These three never read the socket; only the receive loop does.
        pending = [
            task
            for task in (self._watchdog, self._reconnect_task, self._swap_task)
            if task is not None
        ]
        for task in pending:
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
        if self._recv is not None:
            self._recv.cancel()
            pending.append(self._recv)
        await self._drop_socket()
        # Nothing here re-raises: these were all cancelled on purpose, and a
        # teardown that reports its own cancellations as failures buries the
        # reason the caller is tearing down.
        await asyncio.gather(*pending, return_exceptions=True)
        self._started = False
        # A reply still in flight gets its ReplyDone. The disconnect path
        # already did this; aclose did not, so a scheduler awaiting that handle
        # never woke — and its watchdog, the only other thing that could have
        # ended it, had just been cancelled two lines up.
        self._settle(link.ReplyStatus.FAILED)

    # ------------------------------------------------------------- sending

    async def _send(self, frame: bytes) -> None:
        """One frame out, or a Chinese ConnectionError.

        The check sits INSIDE the lock and reads through a local name. Outside
        it, a caller could pass the check, block on the lock, and acquire it
        after _recv_loop or _drop_socket had nulled the socket — then run
        `None.send` and raise AttributeError, which `cancel` does not catch and
        which escapes a spawned barge-in task instead of taking its intended
        "a dead socket has already stopped her" path.
        """
        async with self._send_lock:
            ws = self._ws
            if ws is None:
                raise ConnectionError("火山语音连接没开，发不出去。")
            await ws.send(frame)

    async def set_context(self, instructions: str) -> None:
        """Push the persona, by whichever channel this generation has.

        Before the session starts this only records it: it rides inside
        StartSession, which is where the persona belongs on this protocol and
        the only place SC will take one.

        Afterwards the two generations diverge, and not in a way either of
        them announces. O takes an UpdateConfig — the vendor documents it as a
        FULL replacement, so the whole body goes rather than a patch. SC
        ignores the manifest in an UpdateConfig entirely while still answering
        ConfigUpdated; probed 2026-08-28, four ways round: a manifest sent
        after the session started never applies, one sent at StartSession
        always does, and a later one cannot even amend it. So SC gets a new
        session instead — 114 ms on the same socket, carrying the same
        dialog_id, and measured to keep both the new context and the old
        conversation.
        """
        self._context = instructions
        if not self._started:
            return
        if _PERSONA_KEY[self._model] == "character_manifest":
            await self._swap_session()
            return
        await self._send(
            wire.client_request(
                wire.ClientEvent.UPDATE_CONFIG,
                session_id=self._session_id,
                body=self._session_body(),
            )
        )
        log.info("volcano.context_pushed", context_len=len(self._context))

    async def _swap_session(self) -> None:
        """End this session and start another carrying the current context.

        Deferred while she is speaking. Swapping mid-reply would cut her off
        for a context change nobody was waiting on, and the slot frees between
        replies anyway — so the pending flag is checked again from _settle.
        """
        if self._active is not None:
            self._swap_pending = True
            log.debug("volcano.swap_deferred", context_len=len(self._context))
            return
        self._swap_pending = False
        # One at a time. Two swaps overlapping used to interleave their frames:
        # each cleared the event the other was waiting on, each generated its
        # own session id, and the real server — concurrent, unlike the fake —
        # answered the second StartSession with 「session number limit
        # exceeded: 1」. The link was then addressing an id the server had never
        # confirmed. It does not take two callers: a context push deferred by
        # _settle and the ticker's next refresh are enough.
        async with self._swap_lock:
            if self._swapped_context == self._context:
                # Someone else already carried this one across while we queued.
                return
            sent = await self._swap_once()
            if sent is not None:
                self._swapped_context = sent

    async def _swap_once(self) -> str | None:
        """Retire the session and open the next one.

        Returns:
            The context that actually went out in the StartSession, or None if
            the swap did not land. What went out rather than what was wanted:
            a second push arriving mid-swap changes `_context` before the body
            is built, so its value is already on the wire and recording the
            older one would send the whole thing round again for nothing.
        """
        self._session_closed.clear()
        # Cleared BEFORE the goodbye goes out, not after the server answers it.
        # The uplink gate reads this flag, and a swap has two halves: the old
        # session is dying from the moment FinishSession leaves, and the new one
        # is unconfirmed until SessionStarted lands. Clearing it only for the
        # second half left every microphone frame in the first half addressed to
        # a session the server was in the middle of retiring.
        self._session_ready.clear()
        await self._send(
            wire.client_request(wire.ClientEvent.FINISH_SESSION, session_id=self._session_id)
        )
        if not await self._wait_or_lose(self._session_closed, "等不到服务端收掉上一个会话"):
            return None
        self._session_id = str(uuid.uuid4())
        # Read together, with no await between them, so what is recorded is
        # exactly what the frame carries.
        sent = self._context
        await self._send(
            wire.client_request(
                wire.ClientEvent.START_SESSION,
                session_id=self._session_id,
                body=self._session_body(),
            )
        )
        if not await self._wait_or_lose(self._session_ready, "换会话之后等不到服务端确认"):
            return None
        log.info(
            "volcano.session_swapped",
            context_len=len(sent),
            dialog_id=self._dialog_id,
            dropped_uplink=self._dropped_uplink,
        )
        self._dropped_uplink = 0
        return sent

    async def _wait_or_lose(self, event: asyncio.Event, what: str) -> bool:
        """Wait for one swap step, or tear the link down so it can be rebuilt.

        Returning quietly — which is what this used to do — left the worst
        state this adapter has: the server has retired the session, `_started`
        is still True, no LinkDown was ever emitted, and every later frame is
        addressed to a session that no longer exists. On the panel that reads
        as a healthy link that has simply stopped talking. The reconnect ladder
        already knows how to rebuild both levels and carries the dialog_id, so
        the honest move is to hand it the job.

        The wait goes through the injected clock rather than asyncio.wait_for,
        which is why these two branches are testable at all — every other timer
        in this class already does.
        """
        waiter = asyncio.ensure_future(event.wait())
        timer = asyncio.ensure_future(self._clock.sleep(self._swap_timeout_s))
        try:
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            timer.cancel()
        if event.is_set():
            return True
        log.warning(
            "volcano.swap_timed_out",
            waited_s=self._swap_timeout_s,
            error_text=f"{what}，这条会话已经不在了，交给重连重建。",
        )
        await self._lose_link(f"换会话没成：{what}")
        return False

    async def _lose_link(self, reason: str, *, exc: BaseException | None = None) -> None:
        """The one place that turns "the session is gone" into events and
        recovery.

        Two callers arrive here: the receive loop when the socket drops, and a
        swap that could not finish. They used to do different things — the
        second did nothing at all — which is how a failed swap became a link
        that looked fine forever.
        """
        if self._closing or self._suspended or not self._started:
            return
        self._started = False
        # A deferred context change dies with the session. Leaving the flag set
        # would strand it: `_settle` below only spawns a swap while the socket
        # is alive, and nothing else clears it. Nothing is lost — `_open`
        # rebuilds the next session from `self._context` — but a flag that
        # outlives its meaning is the next reader's trap.
        self._swap_pending = False
        # Nothing in flight survives the gap; settle first so no reply is left
        # holding the single slot across it.
        self._settle(link.ReplyStatus.FAILED)
        await self._drop_socket()
        # A revoked key or a rejected model answers the same way every time, so
        # `retrying=True` there is a promise we cannot keep — and it buys a
        # minute of a panel reading 「连接中」 before it admits as much.
        fatal = exc is not None and classify_error(exc) is ErrorClass.FATAL
        retrying = self._auto_reconnect and not fatal
        if fatal:
            log.error("volcano.link_fatal", error_text=describe(ErrorClass.FATAL, reason[:120]))
        await self._events.put(link.LinkDown(reason=reason[:120], retrying=retrying))
        if retrying and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect(), name="volcano:reconnect")

    async def push_audio(self, pcm: bytes) -> None:
        """The streamer's microphone, 20 ms at a time.

        Dropped rather than queued while a swap is in flight. For those ~114 ms
        the old session is retired and the new one is unconfirmed, so a frame
        sent now draws a server error that lands in the ERROR branch and
        settles the active reply FAILED — losing a fifth of a second of uplink
        is much the cheaper of the two.

        This is NOT the s2s invariant (plan 3.3 rule 7, never stop sending).
        That rule exists because the speculative reopen window runs on the
        audio clock, and this protocol has no such window.
        """
        if not self._session_ready.is_set():
            self._dropped_uplink += 1
            return
        await self._send(wire.audio_request(pcm, session_id=self._session_id))

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        """Stash it. See the module note: there is no wire event for one user
        message, so it travels as the body of the next request_reply.

        Assistant write-backs are dropped, deliberately: the stash is the
        NEXT query's body, so an assistant line here would overwrite a waiting
        danmaku — and the server already keeps her replies in the dialog's
        own 20-round history, which is what write_history exists to fake on
        the providers that forget.
        """
        if role != "user":
            return
        self._pending_item = text

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        """Ask for a reply: the stashed item plus this turn's ask, as one query.

        Blocks until the slot is free — the model claims it itself after VAD,
        so waiting here is not merely bookkeeping. The watchdog is what
        guarantees the wait ends.
        """
        if spec.base_instructions is not None and not self._warned_base_instructions:
            # No per-response instruction channel on this protocol: the query
            # body carries content only. The Assembly reads the capability bit
            # and folds event rules into the session context here instead, so
            # a spec still carrying one means a wiring bug worth one line.
            self._warned_base_instructions = True
            log.warning(
                "volcano.base_instructions_ignored",
                error_text="这个语音后端没有逐轮指令通道，事件规则应并入会话上下文。",
            )
        handle = link.ReplyHandle()
        item = self._pending_item
        # The wait and the take have to be one step: Event.set() wakes every
        # waiter, so two callers each cleared it and each thought it had the
        # slot — three queries on a single-slot endpoint, and the loser never
        # settled because the winner's _take_slot cancelled its watchdog.
        async with self._slot_lock:
            if not self._slot_free.is_set():
                waited_since = self._clock.monotonic()
                await self._slot_free.wait()
                log.info(
                    "link.slot_waited",
                    purpose="reply",
                    waited_ms=round((self._clock.monotonic() - waited_since) * 1000),
                )
            parts = [part for part in (item, spec.instructions) if part]
            self._pending_item = ""
            self._take_slot(handle)
            try:
                await self._send(
                    wire.client_request(
                        wire.ClientEvent.CHAT_TEXT_QUERY,
                        session_id=self._session_id,
                        body={"content": "\n\n".join(parts)},
                    )
                )
            except Exception:
                # A query that never left must not hold the slot for 25 s: the
                # scheduler retries a paid intent, and the retry would spend
                # the whole watchdog waiting on a reply nobody is producing.
                # Same guard, same reason, as client.py:260-272. The stashed
                # item goes back so the retry still carries the danmaku text.
                self._pending_item = item
                self._release_slot()
                raise
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
        await self._interrupt("cancel", handle_id=handle.handle_id)

    async def _interrupt(self, why: str, *, handle_id: int = 0) -> None:
        """Tell the far end to stop. Best effort, by design.

        Raising out of here would surface inside a barge-in — a spawned task
        whose failure nobody is waiting on — and a dead socket has already
        stopped her more thoroughly than this frame would have.
        """
        try:
            await self._send(
                wire.client_request(
                    wire.ClientEvent.CLIENT_INTERRUPT, session_id=self._session_id, body={}
                )
            )
        except (websockets.ConnectionClosed, OSError, ConnectionError) as exc:
            log.debug("volcano.interrupt_unsent", why=why, detail=str(exc)[:120])
            return
        log.info("volcano.interrupted", why=why, handle_id=handle_id)

    async def end_protection(self) -> None:
        """No-op, like the hosted adapter's.

        Nothing was disarmed: this protocol has no barge-in switch to turn off.
        A paired no-op keeps the scheduler's lifecycle uniform across adapters.
        """
        return

    # ------------------------------------------------------------ receiving

    def _take_slot(self, handle: link.ReplyHandle, reason: str = "request") -> None:
        # Same three event names RealtimeClient uses. They are the log line
        # 「为什么她刚才没说话」 gets answered from, and this adapter reaching
        # for its own vocabulary would have split that answer in two.
        log.info("link.slot_taken", reason=reason)
        self._active = handle
        self._reply_text = []
        self._slot_free.clear()
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = asyncio.create_task(self._watch(handle), name="volcano:watchdog")

    def _release_slot(self, reason: str = "send_failed") -> None:
        """Hand the slot back without announcing a reply.

        _settle's counterpart for the one case where there is nothing to
        settle: the request never made it onto the wire, so no ReplyDone is
        owed to anyone and emitting one would tell L3 about a reply that never
        existed.
        """
        self._active = None
        self._reply_text = []
        self._slot_free.set()
        log.info("link.slot_freed", reason=reason)
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

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
            # Before the settle, not after: _settle cancels this very task, so
            # anything awaited afterwards would be eaten by our own cancel.
            # And it has to happen at all — giving up locally left the server
            # generating, which is the same wasted spend `cancel` exists to
            # stop, just reached by the other road.
            await self._interrupt("watchdog")
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
        elif status is not link.ReplyStatus.COMPLETED:
            # Settled before the server ever named it — a cancel or a watchdog
            # timeout landing ahead of ChatTextQueryConfirmed. We cannot
            # tombstone an id we were never told, but the ack is still on its
            # way and carries it, so the id is claimed there instead. Exact,
            # rather than a guess from timing.
            self._orphan_queries += 1
            log.debug("volcano.settled_unnamed", status=status.value)
        self._question = ""
        self._audio_question = ""
        self._active = None
        if self._swap_pending and self._started and self._ws is not None:
            # A context push landed while she was talking. Now that the slot is
            # free the swap can happen — as a task, because settling is called
            # from the receive loop and must not wait on the wire. Held in an
            # attribute rather than let loose: CPython only keeps a weak
            # reference to a running task, so a bare create_task can be
            # collected mid-flight and the swap simply never happens.
            #
            # Guarded on the socket being alive because this also runs on the
            # disconnect path, where _recv_loop nulls _ws in the very next
            # statement: the swap task then raised ConnectionError into nobody,
            # every time, and swallowed _swap_pending on the way out.
            self._swap_task = asyncio.create_task(self._swap_session(), name="volcano:swap")
        self._slot_free.set()
        log.info("link.slot_freed", reason=status.value)
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
            self._take_slot(handle, reason="implicit")
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
                kind = classify_error(exc)
                log.warning(
                    "volcano.reconnect_failed",
                    attempt=attempt,
                    error_text=str(exc)[:160],
                    error_class=type(exc).__name__,
                    error_kind=kind.value,
                )
                if kind is ErrorClass.FATAL:
                    # A revoked key answers the same way six times. Climbing the
                    # whole ladder for it costs about a minute of a panel that
                    # says 「连接中」 and then a message about retries, when the
                    # real reason arrived on the first rung.
                    log.error(
                        "volcano.reconnect_gave_up",
                        attempts=attempt,
                        error_text=describe(kind, str(exc)[:160]),
                    )
                    await self._events.put(
                        link.LinkDown(reason=describe(kind, str(exc)[:80]), retrying=False)
                    )
                    return
                if kind is ErrorClass.BACKOFF:
                    # Full rather than broken: give it longer than the ladder's
                    # own step before asking again.
                    delay = min(delay * 2, 30.0)
                continue
            log.info("volcano.reconnected", attempt=attempt, dialog_id=self._dialog_id)
            await self._events.put(link.LinkUp(attempts=attempt))
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
                        stage="decode",
                        error_text=str(exc),
                        error_class="VolcanoProtocolError",
                    )
                    continue
                try:
                    self._dispatch(frame)
                except wire.VolcanoProtocolError as exc:
                    # Frame.json() raises by design on a body that is not JSON,
                    # and _dispatch calls it in nine places. Outside this guard
                    # one such frame killed the receive task with an exception
                    # nobody retrieved: socket still open, no LinkDown, the
                    # reconnect ladder never armed, and a link that had gone
                    # deaf while every later request_reply waited out its full
                    # watchdog. Survivable for the same reason the decode above
                    # is — one bad body is not a dead session.
                    log.warning(
                        "volcano.frame_unreadable",
                        stage="body",
                        wire_event=frame.event,
                        error_text=str(exc),
                        error_class="VolcanoProtocolError",
                    )
                    continue
        except asyncio.CancelledError:
            raise
        except (websockets.ConnectionClosed, OSError) as exc:
            await self._lose_link(str(exc), exc=exc)

    def _dispatch(self, frame: wire.Frame) -> None:
        """One decoded frame to zero or more normalised events.

        The mapping is the whole point of this adapter: above this line nobody
        knows an event is a number, and L3 sees the same vocabulary it sees
        from every other provider.
        """
        if frame.kind is wire.MessageKind.ERROR:
            # A connection-level error carries no event number, so a dispatch
            # keyed purely off `event` dropped it into the ignore branch and
            # the streamer got silence. That is how a wrong voice presented:
            # session started, query acked, then nothing at all, while the
            # server had said 「ClientError:InvalidSpeaker」 the whole time.
            body = frame.json()
            self._events.put_nowait(
                link.LinkError(code=str(frame.error_code or "unknown"), detail=_error_detail(body))
            )
            log.warning(
                "volcano.error_frame",
                error_code=frame.error_code,
                error_text=str(body.get("error") or "")[:200],
            )
            if self._active is not None:
                self._settle(link.ReplyStatus.FAILED)
            return

        event = frame.event
        if event == wire.ServerEvent.ASR_INFO:
            # The vendor's own words for this one are 「用于打断客户端的播报」 —
            # it IS the barge-in signal. There is no separate speech_started.
            #
            # What it is NOT is a verdict. Whether the reply ends is L3's call,
            # because only L3 knows whether this one is a paid thank-you inside
            # its protected window (director/scheduler.py's _barge_in returns
            # without cancelling when _protection_active). Settling here made
            # that decision for it — the log read 「扛住了打断」 for a reply this
            # adapter had killed two lines earlier — and cleared _active, so the
            # cancel L3 did send hit cancel()'s own guard, never reached the
            # wire, and the model went on generating at our expense.
            self._events.put_nowait(link.SpeechStarted())
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
            question = str(frame.json().get("question_id", ""))
            if self._orphan_queries:
                # That query was already cancelled or timed out, before this
                # ack could name it. Now it has a name: tombstone it, so its
                # tail cannot walk into _ensure_active and mint a ghost.
                self._orphan_queries -= 1
                if question:
                    self._done.append(question)
                log.debug("volcano.orphan_tombstoned", question=question)
                return
            self._ensure_active(question)
            return
        if event == wire.ServerEvent.CHAT_RESPONSE:
            # One decode. Frame is frozen+slots, so json() re-parses every call
            # and this is the highest-frequency JSON event on the link.
            body = frame.json()
            handle = self._ensure_active(str(body.get("question_id", "")))
            if handle is None:
                return
            text = str(body.get("content", ""))
            if text:
                self._reply_text.append(text)
                self._events.put_nowait(link.ReplyTextDelta(handle, text))
            return
        if event == wire.ServerEvent.TTS_RESPONSE:
            # Raw audio, no id of its own — it belongs to the sentence
            # announced before it, and to nothing else. Two rules, each of
            # which was a bug once:
            #
            # Scoped to that sentence: the answer used to live in one
            # link-wide flag that survived until the next settle, so a single
            # late frame from a closed reply silenced whatever was speaking
            # now, for its whole duration — subtitles moving, no sound.
            #
            # And it never mints. Every reply this endpoint starts announces
            # itself with a frame that DOES carry a question_id, so minting
            # from an id-less frame only ever resurrected one we had just
            # cancelled: a fresh handle, the interrupted audio played after
            # all, and the single slot held until the watchdog let go.
            if self._audio_question and self._audio_question in self._done:
                return
            handle = self._active
            if handle is not None and frame.payload:
                self._events.put_nowait(link.ReplyAudioDelta(handle, frame.payload))
            return
        if event == wire.ServerEvent.TTS_SENTENCE_START:
            self._audio_question = str(frame.json().get("question_id", ""))
            self._ensure_active(self._audio_question)
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
        if event == wire.ServerEvent.SESSION_FINISHED:
            self._session_closed.set()
            return
        if event == wire.ServerEvent.SESSION_STARTED:
            # Only a swap gets here; the first session is confirmed by _expect
            # before this loop exists.
            self._dialog_id = str(frame.json().get("dialog_id", "")) or self._dialog_id
            self._session_ready.set()
            return
        if event in (wire.ServerEvent.DIALOG_COMMON_ERROR, wire.ServerEvent.SESSION_FAILED):
            body = frame.json()
            self._events.put_nowait(
                link.LinkError(
                    code=str(body.get("error_code") or frame.error_code or event),
                    detail=_error_detail(body),
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
