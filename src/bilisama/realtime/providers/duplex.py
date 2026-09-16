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
4. **The uplink vocabulary is closed, and stepping outside it is a
   disconnect.** Not an error frame: a 1006. `response.create` is not
   specially rejected — it simply is not in the protocol, and fifteen guessed
   names (`chat.text.query`, `ChatTextQuery`, `input_text_buffer.commit`, …)
   all hang up identically. So a wrong event name costs the session.

And the one that shapes the product rather than the file:

5. **She composes. She cannot be made to START.** The distinction took three
   rounds of probes to get right, and the first two readings of it were wrong,
   so it is worth stating precisely.

   She composes: a danmaku sitting in a legal `dialog_context` is answered in
   her own words the next time the streamer says anything at all — 「欢迎回来呀，
   刚才观众小王问我播到几点，我打算看大家状态，聊得久就多待会儿」 off the
   streamer saying 「我回来了」, with `tts_type` `default`.

   She composes from mid-session text too, through exactly one channel: a
   function-call result, in the vendor's published shape — `items[0] =
   {call_id, role: "tool", content: [{type: "input_text", text}]}`. Hand her
   a danmaku that way and she answers it in her own words. The shape is
   unforgiving: five near-misses were dropped whole, with no ack and no error,
   and one drew `45000000 invalid event payload`.

   What does not exist is a way to START a turn. ChatTextQuery is gone from
   the vendor's own old-to-new table and `response.create` is outside the
   uplink vocabulary (fact 4). Measured, each on its own socket: a legal
   context and 25 s of nothing → silence; an empty assistant placeholder so
   the last turn looks unanswered → silence; `input_audio_buffer.commit` →
   silence; two seconds of pushed silence → silence;
   `extension.dialog.extra.input_mod` `"text"`, the old protocol's text-input
   mode → accepted, then silence; `input_audio_mute.commit` /
   `input_audio_unmute.commit`, which do exist → silence.
   `speech_text_buffer.append`, the one guessed name in the sweep that did
   not hang up, answers with `tts_type` `chat_tts_text` — it reads, it does
   not write.

   The function-call channel does not get around this, and the reason is
   worth keeping: the pending call has to be one SHE issued. A fabricated
   `call_id` draws nothing, and the pushed audio that finally flushed the
   real one moves nothing on its own (control: same silence, empty context,
   no reply). So the sequence always starts with the streamer's microphone.

   So a reply that has to go out at a moment of OUR choosing arrives with its
   words already written: `request_reply` needs `ReplySpec.text`, L3 fills it,
   and `composes_on_demand=False` is how L3 knows to. Nothing here calls a
   model to write her lines — whose words come out of her mouth is a product
   decision, and an adapter that quietly made it would be the wrong place to
   discover it.

Two more measured facts worth keeping next to the code that relies on them.

History reaches the model ONLY at `session.create`, through
`extension.dialog.dialog_context`. Mid-session `conversation.item.create` acks
with `conversation.item.added` and `session.update` with a `dialog_context`
acks with `session.updated`; neither changes what she knows. Same shape as
SC2.0's character manifest, and the same remedy — a new session — which
`set_context` uses for the persona.

And `dialog_context` must have an EVEN number of turns, because the endpoint
reads it positionally in pairs and does not look at `role` at all. An odd
length discards the whole context — not the odd turn, the whole thing — while
`session.created` comes back exactly as it does on success, so nothing on the
wire tells you. An even length in the wrong order is worse than useless rather
than rejected: `[user, user, assistant, assistant]` came back with the second
line attributed to her. Measured 2026-09-16, one session each. `pair_up` below
therefore emits even-length, correctly-ordered pairs and reports what it had
to drop to get there.
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
    from collections.abc import AsyncIterator, Mapping, Sequence

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


def pair_up(
    items: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Shape a run of context items into what `dialog_context` actually keeps.

    The endpoint reads the array positionally in pairs and never looks at
    `role`, so two rules fall out: the length must be even (an odd one
    discards the whole context, silently) and the order must really alternate
    (a wrong order is accepted and comes back with the wrong speaker attached
    to the line). L3 hands us neither: three danmaku arrive in a row before she
    answers any of them, and a mirrored assistant line can turn up with nothing
    in front of it.

    So consecutive same-role items merge into one turn — three danmaku become
    one user turn of three lines, which keeps the text rather than dropping it
    — and a leading assistant or trailing user, having nothing to pair with,
    comes back to the caller instead of poisoning the array. Returns the pairs
    and the leftovers, so the caller can say what went missing rather than lose
    it quietly.
    """
    merged: list[dict[str, str]] = []
    for item in items:
        role = "assistant" if item.get("role") == "assistant" else "user"
        text = str(item.get("text") or "")
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["text"] += "\n" + text
        else:
            merged.append({"role": role, "text": text})
    dropped: list[dict[str, str]] = []
    if merged and merged[0]["role"] == "assistant":
        dropped.append(merged.pop(0))  # nothing prompted it
    if len(merged) % 2:
        dropped.append(merged.pop())  # trailing user, still unanswered
    return merged, dropped


# The tool she pulls pending viewer events with, when the danmaku route is
# "tool" rather than "ghostwriter". The name and the argument-free signature
# are this adapter's business: L3 decides WHETHER she answers her own danmaku,
# never how the frames spell it.
PULL_TOOL = {
    "type": "function",
    "name": "get_live_events",
    "description": "拉取直播间里还没回过的观众消息（弹幕、礼物、进房）",
    "parameters": {"type": "object", "properties": {}, "required": []},
}
# Appended to the persona when the tool is declared. It is a statement about
# what this endpoint can and cannot see rather than about who she is, which is
# why it lives here and not in config/personas: on the other three providers
# the events arrive in the prompt and none of this is true.
PULL_RULE = (
    "\n\n观众的弹幕、礼物和进房你看不到，要调用 get_live_events 才能拿到。"
    "和主播说话时，如果有还没回的观众消息，顺口带一句再接着聊；没有就正常说话，"
    "不要提这个工具，也不要说自己去查了。"
)

_NO_EVENTS = "（暂时没有新的观众消息）"


def session_body(
    cfg: VolcanoDuplexConfig,
    *,
    instructions: str,
    model: str,
    speaker: str,
    session_id: str = "",
    history: list[dict[str, str]] | None = None,
    pull_tool: bool = False,
) -> dict[str, Any]:
    """The session block both `session.create` and `session.update` carry.

    `history` rides `extension.dialog.dialog_context` and only does anything
    on create — see the module note. It is accepted on update and ignored,
    which is why this builder never adds it there by accident: the caller
    passes it exactly when it can work.
    """
    session: dict[str, Any] = {
        "model": model,
        "instructions": instructions + (PULL_RULE if pull_tool else ""),
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
    if pull_tool:
        session["tools"] = [PULL_TOOL]
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
        pull_tool: bool = False,
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
        self._pull_tool = pull_tool
        # Viewer events filed but not yet handed to her. Only fills when the
        # tool is declared; in ghostwriter mode L3 answers these itself and
        # nothing would ever drain the queue. Bounded because a quiet streamer
        # is exactly when it grows: she can only pull on a turn she takes.
        self._unpulled: deque[str] = deque(maxlen=20)

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
        history, dropped = pair_up(self._pending_items)
        await self._send(
            {
                "type": "session.create",
                **session_body(
                    self._cfg,
                    instructions=self._context,
                    model=self._model,
                    speaker=self._speaker,
                    session_id=self._session_id,
                    history=history or None,
                    pull_tool=self._pull_tool,
                ),
            }
        )
        if dropped:
            log.info(
                "duplex.context_unpaired",
                dropped=len(dropped),
                kept=len(history),
                error_text="这些上下文条目凑不成一问一答，没随会话带过去——带上会让整段历史被丢弃。",
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
                    pull_tool=self._pull_tool,
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

        Kept raw, in arrival order. `pair_up` does the shaping at create time,
        because only then is it known whether the last danmaku ever got an
        answer — and an unanswered one on the end voids the whole context.

        With the pull tool declared this is also the filing step: a viewer
        event waits here until she asks for it on a turn of her own. Her own
        lines are not viewer events and never queue.
        """
        self._pending_items.append({"role": role, "text": text})
        if self._pull_tool and role != "assistant":
            self._unpulled.append(text)

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        """Say `spec.text`, as it stands.

        There is no composing step on this protocol; see the module note and
        `capabilities.composes_on_demand`. A spec that arrives without text is a
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
                    try:
                        self._dispatch(frame)
                    except Exception as exc:  # a bug here, not a dead socket
                        # Letting this reach the handler below would report a
                        # healthy link as disconnected and quietly reconnect,
                        # which is how a NameError in a frame handler spent an
                        # afternoon looking like a flaky endpoint.
                        log.exception(
                            "duplex.frame_handler_failed",
                            kind=str(frame.get("type") or ""),
                            error_text=str(exc)[:200],
                        )
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
        if kind == "response.function_call_arguments.done":
            self._on_pull(frame)
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

    def _on_pull(self, frame: Mapping[str, Any]) -> None:
        """She asked for the pending viewer events. Hand them over.

        The reply shape is the vendor's published one and nothing near it
        works: five near-misses were dropped whole, with neither an ack nor an
        error, so there is no runtime signal to tell a typo from a refusal.
        `call_id` must be the one she issued — a fabricated one draws nothing.
        """
        items = frame.get("items") or []
        # `dict`, not `Mapping`: that name is a typing-only import here, and an
        # isinstance against it raises inside the receive loop — which reads as
        # a dropped socket and a reconnect, not as the bug it is.
        first = items[0] if items and isinstance(items[0], dict) else {}
        call_id = str(first.get("call_id") or frame.get("call_id") or "")
        if not call_id:
            log.warning(
                "duplex.pull_without_call_id",
                error_text="她调了工具但帧里没有 call_id，这一轮回传不了。",
            )
            return
        pending = list(self._unpulled)
        self._unpulled.clear()
        log.info("duplex.events_pulled", count=len(pending))
        payload = "\n".join(pending) if pending else _NO_EVENTS
        # _dispatch runs inside the receive loop, so the send goes on a task
        # of its own rather than blocking the next frame.
        task = asyncio.create_task(
            self._send(
                {
                    "type": "conversation.item.create",
                    "items": [
                        {
                            "call_id": call_id,
                            "role": "tool",
                            "content": [{"type": "input_text", "text": payload}],
                        }
                    ],
                }
            ),
            name="duplex:pull",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
