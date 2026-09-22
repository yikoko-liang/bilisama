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
4. **The uplink vocabulary is closed, and stepping outside it costs the
   session.** The endpoint names the offence and then drops the socket:
   `45000000 unknown event name:<name>` followed 0.0-0.1 s later by the
   hang-up (measured 2026-09-21, polled for 25 s with and without uplink
   keep-alive). This used to say "Not an error frame: a 1006", which was half
   right and hid the useful half — the frame tells you WHICH name was wrong.
   `response.create` draws it like any other unknown name, with or without a
   legal body, so it is absent rather than specially refused.

   The expensive half still holds, so names must come from the vendor's event
   reference, not from a hunch. The 2026-09-16 sweep guessed fifteen and
   recorded four; the eleven it did not write down are not evidence of
   anything, and one of the names it never tried is the subject of fact 5.

And the one that shapes the product rather than the file:

5. **She composes, but not on demand — and a turn CAN be started.** The
   distinction took four rounds of probes to get right and three of them were
   wrong, so it is worth stating precisely.

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

   What does not exist is a way to make her COMPOSE one on demand.
   ChatTextQuery is gone from the vendor's own old-to-new table, and
   `response.create` is absent from the uplink vocabulary (fact 4). Measured,
   each on its own socket: a legal context and 25 s of nothing → silence; an
   empty assistant placeholder so the last turn looks unanswered → silence;
   `input_audio_buffer.commit` → silence; two seconds of pushed silence →
   silence; `extension.dialog.extra.input_mod` `"text"`, the old protocol's
   text-input mode → accepted, then silence; `input_audio_mute.commit` /
   `input_audio_unmute.commit`, which do exist → silence.

   STARTING a turn, though, is a thing we CAN do, and this header said
   otherwise until 2026-09-21. `speech_text_buffer.commit` — 打招呼, the
   vendor's mapping of the legacy SayHello(300) — carries text in one frame
   and makes her speak it. Measured in four conditions: no uplink ever pushed
   (332 ms to first audio), silent uplink (440 ms), and twice after the
   streamer really spoke and she really answered. `tts_type` is
   `chat_tts_text`, the same value `_book_her_turn` already keys on.

   Why it was missed: the sweep tried `speech_text_buffer.APPEND`, a guessed
   name (it answers `chat_tts_text` too — it reads, it does not write), and
   never tried `.commit`. The only place that string existed in this repo was
   the fake, which had assumed it into the vocabulary and modelled it as a
   silent no-op, so no test could have caught the gap.

   The function-call channel still does not get around the COMPOSE limit, and
   the reason is worth keeping: the pending call has to be one SHE issued. A
   fabricated `call_id` draws nothing, and the pushed audio that finally
   flushed the real one moves nothing on its own (control: same silence,
   empty context, no reply).

   So a reply that has to go out at a moment of OUR choosing arrives with its
   words already written: `request_reply` needs `ReplySpec.text`, L3 fills it,
   and `composes_on_demand=False` is how L3 knows to. What changed is only
   the delivery: those words now go out on the greeting event instead of
   `speech_text_buffer.replacement`, which is 干预模型回复 — it edits a reply
   already in flight, and with the microphone open there is rarely one to
   edit (forty sends, zero audio, eight live sessions). Nothing here calls a
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
# How long the transcriber may say NOTHING about an open turn before we call
# it over ourselves. Quiet since the last delta, not elapsed since the start:
# a long sentence keeps producing deltas and keeps resetting this — it only
# catches the ending that never arrives. Measured 2026-09-16/17: the endings
# go missing about fifteen times across an hour of talking, in healthy
# stretches as much as sick ones, and each one costs the streamer a window of
# her ignoring him completely.
#
# The shipped value lives in VolcanoDuplexConfig.speech_edge_s (1.0 since
# 2026-09-22; it was 3.0 here). This constant is only the default for a link
# built without a config — tests, mostly — and tracks the config's default so
# the two cannot silently disagree. At 1.0 a mid-sentence pause longer than a
# second splits the turn; the config comment spells out that trade.
_SPEECH_EDGE_S = 1.0
# How long a reply we asked for may stay silent before we give up on it. The
# endpoint answers a replacement in about 84 ms when it is going to answer at
# all, so six seconds is not a race — it is the gap between "slow" and "never",
# and the cost of guessing wrong is her ignoring the streamer for the rest of
# the wait.
_UNSPOKEN_S = 6.0
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
            # `pcm_s16le`, not `pcm`. Both are accepted, both come back with a
            # plain `session.created`, and they mean different things: `pcm`
            # is FLOAT32 on the downlink. Read as the signed 16-bit everything
            # downstream expects, that is full-scale white noise — the sound a
            # streamer described as 刺耳噪声 rather than anything recognisable.
            # Nothing on the wire says so, which is why seventeen rounds of
            # frame-counting probes missed it; test_duplex_contract.py now
            # listens to the waveform instead of counting frames.
            "input": {"format": {"type": "pcm_s16le", "rate": 16000}},
            "output": {
                "format": {"type": "pcm_s16le", "rate": 24000},
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
        unspoken_s: float = _UNSPOKEN_S,
        speech_edge_s: float = _SPEECH_EDGE_S,
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
        self._unspoken_s = min(unspoken_s, watchdog_s)
        self._speech_edge_s = speech_edge_s
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
        # Her streamed words for the turn in flight. Ours never stream, so
        # this stays empty on the replies we drive — see _spoken_text.
        self._heard: list[str] = []
        # The streamer's words as the transcriber last had them, and how much
        # of that has already gone up as increments. Two fields because the
        # tail flaps: the latest value is what the done reports, the longest
        # sent is what the next increment is measured against.
        self._asr_text = ""
        self._asr_sent = ""
        # A transcription is open; the floor is closed until it ends.
        self._speech_open = False
        self._speech_edge: asyncio.Task[None] | None = None
        # When the transcriber last said anything about this turn. The backstop
        # measures quiet from here, not from the start.
        self._asr_at = 0.0
        # How much of this utterance has already gone out in a done. Only
        # moves when a timeout splits one sentence across two turns, and the
        # continuation's cumulative deltas would otherwise repeat the opening.
        self._asr_reported = ""
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
                # 打招呼, one frame carrying the words. The vendor maps it from
                # the legacy SayHello(300), and it STARTS a turn rather than
                # editing one in flight.
                #
                # This used to send speech_text_buffer.replacement.append +
                # .commit, which is 干预模型回复 — it rewrites a reply already
                # being generated. With the microphone open there is usually no
                # such reply, and it produced audio zero times in forty sends
                # across eight live sessions. The sweep that concluded "nothing
                # starts a turn" tried speech_text_buffer.APPEND, a guessed
                # name, and never tried .commit; the only place that string
                # appeared in this repo was our own fake.
                #
                # Measured on the real endpoint 2026-09-21, four conditions:
                # no uplink (332 ms to first audio), silent uplink (440 ms),
                # and twice after the streamer really spoke and she really
                # answered. Audio every time, tts_type chat_tts_text — which is
                # what _book_her_turn already keys on, so the gate needs no
                # change.
                await self._send({"type": "speech_text_buffer.commit", "text": spec.text})
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

        A reply we asked for but that has not announced yet is cancellable
        too, and used to be dropped here: `_active` is only set when
        `response.output_audio.started` arrives, so a barge-in landing before
        that returned without sending anything AND without settling. The slot
        then sat taken until the 25-second watchdog, which is a quarter of a
        minute of her answering nothing at all — measured in a live session on
        2026-09-17, request at :47 and watchdog at :12.
        """
        mine = self._active is handle or self._pending_ours is handle
        if not mine:
            return
        handle.stale = True
        with contextlib.suppress(Exception):
            await self._send({"type": "response.cancel"})
        if self._pending_ours is handle:
            # Never announced, so no response.canceled is coming for it: the
            # server has nothing booked under that name. Settle it here or
            # nothing will until the watchdog.
            log.info("duplex.cancel_before_start", handle_id=handle.handle_id)
            self._settle(link.ReplyStatus.CANCELLED)

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
        self._heard = []
        self._slot_free.set()
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    # ------------------------------------------------------------ speech edges

    def _close_speech(self, *, timed_out: bool = False) -> None:
        """End the streamer's turn: report what was said, then drop the floor.

        Both halves matter and only one of them is obvious. The transcript is
        the streamer's line in the chat page; `SpeechStopped` is what lets her
        answer anything ever again, because until it arrives the floor holds
        `streamer_speaking` and every intent is gated `gate.host_speaking`.
        """
        if not self._speech_open:
            return
        self._speech_open = False
        if self._speech_edge is not None:
            self._speech_edge.cancel()
            self._speech_edge = None
        # Only the part no earlier done has carried. The transcriber's deltas
        # are cumulative from the start of the utterance and it does not know
        # we gave up, so after a timeout the continuation still arrives with
        # the whole thing — reporting that verbatim would put the opening of
        # the sentence into the chat page twice.
        fresh = self._asr_text[len(self._asr_reported) :]
        if fresh:
            self._events.put_nowait(link.UserTranscriptDone(fresh))
            self._asr_reported = self._asr_text
        self._asr_text = ""
        self._asr_sent = ""
        if timed_out:
            log.warning(
                "duplex.speech_edge_missing",
                quiet_ms=round((self._clock.monotonic() - self._asr_at) * 1000),
                error_text=(
                    "转写不再更新，但结束帧一直没来，按这一句说完处理。"
                    "端点偶尔会漏这一帧；不自己收尾的话说话权就一直关着。"
                ),
            )
        self._events.put_nowait(link.SpeechStopped())

    def _arm_speech_edge(self) -> None:
        """A start with no end wedges the gate shut, so bound the wait.

        This protocol's only 「the streamer stopped」 signal is the
        transcriber's completed frame, and that frame goes missing — fifteen
        times across the corpus of 2026-09-16/17, scattered through healthy
        stretches as much as sick ones, and self-healing on the next
        utterance. Every other provider has a VAD edge that does not depend on
        transcription, so this backstop is this file's alone.

        Timed from the last DELTA rather than from the start, which is the
        difference between a two-second hiccup and a twenty-second one. A long
        sentence with pauses keeps producing deltas and keeps pushing the
        deadline out, so this never cuts a streamer off mid-thought; a lost
        ending shows up as deltas simply stopping, which is exactly what it
        now measures. Timing it from the start instead cost a flat twenty
        seconds of her ignoring everything, three or four times per five
        minutes — the shape the streamer described as 「聊一会儿就不行了」.
        """
        self._speech_open = True
        self._asr_at = self._clock.monotonic()
        if self._speech_edge is not None:
            self._speech_edge.cancel()
        task = asyncio.create_task(self._await_speech_edge(), name="duplex:speech-edge")
        self._speech_edge = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _await_speech_edge(self) -> None:
        # Re-check rather than sleep once: every delta pushes _asr_at forward,
        # and the turn is only over when the stream has actually gone quiet.
        while True:
            quiet_for = self._clock.monotonic() - self._asr_at
            if quiet_for >= self._speech_edge_s:
                break
            await self._clock.sleep(self._speech_edge_s - quiet_for)
        self._close_speech(timed_out=True)

    def _arm_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        task = asyncio.create_task(self._watch(), name="duplex:watchdog")
        self._watchdog = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _watch(self) -> None:
        # Two deadlines, because the two failures are nothing alike. A reply
        # that started and will not stop is rare and gets the long wait. A
        # reply that never SAYS anything is the common one here — the endpoint
        # takes a speech_text_buffer.replacement, answers nothing, and plays
        # nothing — and it costs far more, because the slot it holds is also
        # the slot her own microphone turns need. Twenty-five seconds of that
        # is twenty-five seconds of her ignoring the streamer entirely.
        await self._clock.sleep(self._unspoken_s)
        if self._slot_free.is_set():
            return
        if self._pending_ours is not None:
            log.warning(
                "duplex.reply_never_spoken",
                waited_s=self._unspoken_s,
                error_text=(
                    "点了一条回复，服务端既不开口也不报错，按取消结算并放开名额。"
                    "占着名额时她连自己的轮次都起不了。"
                ),
            )
            self._settle(link.ReplyStatus.CANCELLED)
            return
        await self._clock.sleep(max(0.0, self._watchdog_s - self._unspoken_s))
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
            self._asr_text = ""
            self._asr_sent = ""
            self._asr_reported = ""  # a genuinely new utterance
            self._arm_speech_edge()
            self._events.put_nowait(link.SpeechStarted())
            return
        if kind == "conversation.item.input_audio_transcription.delta":
            # CUMULATIVE here, incremental everywhere else: 「今天」「今天打算」
            # 「今天打算打」…. The contract downstream is an increment, so only
            # the growth goes up.
            #
            # And the tail flaps: 「…游戏」「…游戏？」「…游戏」「…游戏？」 in one
            # measured run, the transcriber changing its mind about the
            # punctuation. Emitting against the LAST value would send 「？」
            # twice; emitting against the longest already sent sends it once
            # and stays quiet through the revision, which the done then
            # settles. Every delta is a prefix of the last one, so nothing is
            # lost by waiting.
            whole = str(frame.get("delta") or "")
            if not self._speech_open:
                # We gave up on this turn and he is still talking, so the
                # ending was late rather than lost. Without this the rest of
                # the sentence falls outside any turn: the floor stays open
                # while he speaks, and the `completed` that eventually does
                # arrive finds the turn already closed and reports nothing —
                # so everything after the timeout never reaches the chat page
                # or memory at all.
                self._arm_speech_edge()
                self._events.put_nowait(link.SpeechStarted())
            self._asr_text = whole
            self._asr_at = self._clock.monotonic()  # the turn is still going
            if whole.startswith(self._asr_sent) and len(whole) > len(self._asr_sent):
                self._events.put_nowait(link.UserTranscriptDelta(whole[len(self._asr_sent) :]))
                self._asr_sent = whole
            return
        if kind == "conversation.item.input_audio_transcription.completed":
            # Its own `transcript` comes back EMPTY — the words only ever live
            # in the last delta. Sending the edge alone left the streamer's own
            # side of the conversation out of the chat page, the console and
            # `on_transcript` entirely (ui/events.py:199, dev_talk.py:1180 both
            # key on the done), which looked like a UI bug and was this line.
            self._close_speech()
            return
        if kind == "response.function_call_arguments.done":
            self._on_pull(frame)
            return
        if kind == "response.output_audio.started":
            self._on_reply_started(frame)
            return
        if kind == "response.output_text.delta":
            # Her words arrive BEFORE the audio frame that used to book the
            # turn — 11 characters ahead of it in one measured run — so
            # booking only on audio threw away the start of everything she
            # said. A streamed delta can only be hers: the TTS path we drive
            # streams no text at all (measured: audio.started, audio deltas,
            # done, and not one text frame).
            handle = self._active or self._book_her_turn()
            if handle is None or handle.stale:
                return
            text = str(frame.get("delta") or "")
            self._heard.append(text)
            self._events.put_nowait(link.ReplyTextDelta(handle, text))
            return
        if kind == "response.output_text.done":
            # Carries the whole turn, byte-identical to the deltas joined.
            # Kept as the authority for what she said, so a delta lost to a
            # race cannot quietly shorten what memory and the chat page get.
            whole = str(frame.get("text") or "")
            if whole:
                self._heard = [whole]
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
            return  # already booked — by a text delta, or a second sentence
        self._book_her_turn()

    def _book_her_turn(self) -> link.ReplyHandle | None:
        """Open a turn of her own, from whichever frame announced it first.

        A reply of OURS still waiting to be spoken does not outrank this. Hers
        is happening — the frames are here — and ours is hypothetical: the
        endpoint accepts a `speech_text_buffer.replacement` and sometimes never
        plays it, with no error and no refusal. Holding the slot for the
        hypothetical one muted her for a full 25 seconds at a time in a live
        session on 2026-09-17, so the one that is actually making sound wins.
        """
        if self._pending_ours is not None:
            log.warning(
                "duplex.turn_overlapped",
                error_text="我们点的回复还没开口，她自己先说上了——让给她，那条按取消结算。",
            )
            self._settle(link.ReplyStatus.CANCELLED)
        handle = link.ReplyHandle(implicit=True)
        self._take_slot(handle, ours=False)
        self._events.put_nowait(link.ReplyStarted(handle))
        return handle

    def _spoken_text(self) -> str:
        """What this turn actually said.

        Hers streams and is collected in `_heard`; ours never streams, so the
        words we handed over are the only record. Reading `_pending_text` for
        both was the bug that made her own turns arrive at memory, the chat
        page and the log carrying an empty string — audible, and invisible
        everywhere else.
        """
        return "".join(self._heard) or self._pending_text

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
