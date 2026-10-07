"""A fake Seeduplex server, as unhelpful as the real one.

Every rule below was measured against the real endpoint on 2026-09-16, and
every one of them refuses something a kinder fake would let through. That is
the point: this repo has twice shipped a green suite over a mock gentler than
the endpoint it stood in for.

What it insists on:

* **The client speaks first.** No greeting until `session.create` arrives.
  A client that waits for one waits forever, which is exactly what
  RealtimeClient does and why this provider has its own adapter.
* **Replies never announce, and hers start before they announce.** There is
  no `response.created`. `response.output_audio.started` carries the
  `tts_type` that says whose words these are — `chat_tts_text` for text we
  were handed, `default` for one she started herself — and a client that
  cannot tell them apart puts her own turns and ours in the same basket,
  where the voice gate then holds the wrong ones. But on HER turns the first
  text deltas arrive ahead of that frame, so a client that waits for it to
  book the turn silently drops the opening of every sentence.
* **An unknown event name closes the socket.** Not an error frame: a 1006.
  Measured 2026-09-16 across fifteen guessed names — `response.create`,
  `chat.text.query`, `ChatTextQuery` and the rest all hang up identically.
  The uplink vocabulary is closed, and a client that reaches outside it dies
  here the way it dies there.
* **History only lands at create, and only in even-length pairs.**
  `conversation.item.create` acks with `conversation.item.added` and
  `session.update` with a `dialog_context` acks with `session.updated`, and
  neither reaches `context`. Only `extension.dialog.dialog_context` on
  `session.create` does — and the real endpoint reads that array positionally
  in pairs without looking at `role`, so an odd length discards the WHOLE
  context while still answering `session.created`. This fake discards it the
  same way. It additionally records a violation for an array that is even but
  out of order: there the endpoint keeps the text and hangs the wrong speaker
  on it, which is harder to notice and worse to ship. The acks are what makes
  all this worth modelling — a client cannot tell success from silence by the
  reply, only by asking her afterwards.
* **Her first turn only hears the instructions she was CREATED with.** A
  `session.update` sent before any turn acks with `session.updated` and is
  not heard by that first turn: she answers as the vendor's default
  assistant (「我叫豆包，是字节跳动开发的AI助手」), three runs out of three,
  measured 2026-09-24; the same persona riding `session.create` is obeyed on
  the first turn every time. After a turn has happened, an update IS heard
  from the next one (test_duplex_contract.py's mid-session rename).
  `heard_instructions` is what her next turn runs on.
* **Transcription carries the speech edges.** There is no
  `input_audio_buffer.speech_started`; `...transcription.started` is the
  earliest one, its deltas are CUMULATIVE, and `...completed` comes back with
  an empty transcript.

Violations land in `violations` and fail the test from `__aexit__`, the same
machinery mock_volcano.py uses and for the same reason: websockets swallows
whatever a handler raises, so an assert in here would close the socket with
1011 and read as a stray reconnect in an otherwise green test.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

__all__ = ["MockDuplexServer", "Recorded"]

_OURS = "chat_tts_text"
_HERS = "default"
# How much of her turn streams before the audio frame announces it. Measured
# at 11 characters over 5 deltas; 2 is enough to keep a client honest.
_AHEAD = 2

# The closed uplink vocabulary. Anything else and the real endpoint hangs up.
_KNOWN = frozenset(
    {
        "session.create",
        "session.update",
        "session.close",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        # The keepalive pair. Documented, and confirmed present 2026-09-16 —
        # neither hangs up. Neither starts a turn either.
        "input_audio_mute.commit",
        "input_audio_unmute.commit",
        "conversation.item.create",
        "conversation.item.retrieve",
        "conversation.item.delete",
        "response.cancel",
        "speech_text_buffer.append",
        "speech_text_buffer.commit",
        "speech_text_buffer.replacement.append",
        "speech_text_buffer.replacement.commit",
    }
)


def _in_order(context: list[dict[str, str]]) -> bool:
    """user, assistant, user, assistant — what the positional read assumes."""
    return all(
        turn.get("role") == ("user" if i % 2 == 0 else "assistant")
        for i, turn in enumerate(context)
    )


@dataclass
class Recorded:
    """Every client frame, in order."""

    frames: list[dict[str, Any]] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.frames if f.get("type") == kind)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == kind]

    def last(self, kind: str) -> dict[str, Any]:
        found = self.of(kind)
        assert found, f"客户端从没发过 {kind}"
        return found[-1]


class MockDuplexServer:
    """One connection, one session, and the endpoint's own bad manners."""

    def __init__(self, *, refuse_session: bool = False, deaf_to_tts: bool = False) -> None:
        self.recorded = Recorded()
        self.violations: list[str] = []
        # What the model would answer from, as far as this fake models it:
        # only what arrived in dialog_context at create.
        self.context: list[dict[str, str]] = []
        # True when a dialog_context arrived and was thrown away for shape.
        self.context_refused = False
        # What came back as a function-call result, in arrival order.
        self.tool_results: list[str] = []
        self.tools: list[dict[str, Any]] = []
        self.instructions = ""
        # What session.create carried, and whether this session has had a
        # turn yet — together they decide heard_instructions.
        self._create_instructions = ""
        self._turned = False
        # Connections the client has opened; one session each.
        self.connections = 0
        self.session_id = "sess-1"
        self._refuse_session = refuse_session
        # Takes a speech_text_buffer.replacement and plays nothing: no error,
        # no refusal, no frames. Seen live on 2026-09-17, twice in a row, and
        # the slot it holds is the one her own turns need.
        self._deaf_to_tts = deaf_to_tts
        # Whether a greeting we send is played to completion. False leaves it
        # streaming with no done, for tests of the adapter's timed reply hold.
        self.finish_ours = True
        # Transcription items, as the real endpoint numbers them (measured
        # 2026-10-05: started / delta / completed all carry `item_id`). The
        # open one is a line whose `completed` has not been sent yet.
        self._item_seq = 0
        self._open_item: tuple[str, str] | None = None
        # What the real endpoint does to an unfinished line when a reply of
        # ours starts over it: the item is abandoned, never completed, and a
        # NEW item re-transcribes the line from its first word (measured
        # 2026-10-05, a commit 25 s into a 67 s monologue). On by default
        # because that is the endpoint's behaviour, not a fault.
        self.retranscribe_on_interrupt = True
        # How many response.cancel frames arrived, and whether one scrambles
        # the next transcription item the way the real endpoint's did.
        self.cancels = 0
        self.scramble_after_cancel = False
        self.items: list[str] = []
        self._server: Any = None
        self._ws: ServerConnection | None = None
        self._ready = asyncio.Event()
        self._created = False

    async def __aenter__(self) -> MockDuplexServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self.check()

    def check(self) -> None:
        assert not self.violations, "假服务端记下的违规：\n  " + "\n  ".join(self.violations)

    def _refuse(self, rule: str, *, ok: bool) -> bool:
        if not ok:
            self.violations.append(rule)
        return ok

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/api/v3/duplex/realtime/dialogue"

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=5.0)

    @property
    def heard_instructions(self) -> str:
        """The instructions her NEXT turn actually runs on.

        The create-time ones until this session has had a turn; after that,
        the latest. An update sent into a fresh session is acked and not heard
        by the first turn — see the module note.
        """
        return self.instructions if self._turned else self._create_instructions

    # ------------------------------------------------------------ the wire

    async def _handle(self, ws: ServerConnection) -> None:
        self._ws = ws
        # The rule is per CONNECTION: a client that reconnects or recreates
        # its session opens a new socket and creates again on it.
        self._created = False
        self.connections += 1
        try:
            async for raw in ws:
                if not self._refuse("上行必须是 JSON 文本帧，不是二进制", ok=isinstance(raw, str)):
                    continue
                try:
                    frame = json.loads(raw)
                except ValueError:
                    self.violations.append(f"上行不是合法 JSON：{raw[:80]!r}")
                    continue
                self.recorded.frames.append(frame)
                await self._on_frame(ws, frame)
        except websockets.ConnectionClosed:
            return

    async def _on_frame(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")

        if kind == "session.create":
            if self._refuse("一条连接只建一次会话", ok=not self._created):
                await self._on_create(ws, frame)
            return

        if not self._refuse(f"{kind} 发在 session.create 之前", ok=self._created):
            return

        if kind == "session.update":
            session = frame.get("session") or {}
            if "instructions" in session:
                # Instructions DO apply mid-session — the one thing that does —
                # but only from the turn after one has happened. Before that,
                # heard_instructions still answers with the create-time ones.
                self.instructions = str(session["instructions"])
            # dialog_context here is acked and dropped, which is the trap.
            await self._send(ws, {"type": "session.updated"})
        elif kind == "input_audio_buffer.append":
            audio = frame.get("audio")
            self._refuse("上行音频必须是 base64 字符串", ok=isinstance(audio, str))
        elif kind == "input_audio_buffer.commit":
            await self._send(ws, {"type": "input_audio_buffer.committed"})
        elif kind == "conversation.item.create":
            items = frame.get("items") or []
            first = items[0] if items else {}
            if "call_id" in first:
                # A function-call result. The real endpoint is unforgiving
                # about this shape — five near-misses were dropped whole, with
                # neither an ack nor an error — so anything else is a
                # violation here rather than a silent pass.
                self._refuse(
                    "FC 回传要用文档的形状：role=tool、content=[{type:input_text,text}]",
                    ok=first.get("role") == "tool"
                    and [c.get("type") for c in first.get("content") or []] == ["input_text"],
                )
                self.tool_results.append(str((first.get("content") or [{}])[0].get("text") or ""))
                return
            await self._send(ws, {"type": "conversation.item.added", "items": items})
        elif kind == "speech_text_buffer.commit":
            # 打招呼 — the vendor's turn-start primitive, mapped from the legacy
            # SayHello(300). It speaks on its own frame, no append needed, and
            # it speaks whatever the uplink is doing: measured on the real
            # endpoint 2026-09-21 in four conditions (no uplink, silent uplink,
            # and twice after a real exchange), audio every time with
            # tts_type chat_tts_text. Deliberately NOT gated on _deaf_to_tts:
            # that fault models the replacement channel, which is a different
            # event with a different failure.
            await self.say(str(frame.get("text") or ""), ours=True, finish=self.finish_ours)
            if self._open_item is not None and self.retranscribe_on_interrupt:
                # The line he was in the middle of is dropped and started over.
                _old, so_far = self._open_item
                new_id = self._next_item()
                await self._send(
                    ws,
                    {
                        "type": "conversation.item.input_audio_transcription.started",
                        "item_id": new_id,
                    },
                )
                await self._send(ws, self._delta(new_id, so_far))
                self._open_item = (new_id, so_far)
        elif kind == "speech_text_buffer.replacement.append":
            self._pending = str(frame.get("text") or "")
        elif kind == "speech_text_buffer.replacement.commit":
            if not self._deaf_to_tts:
                await self.say(getattr(self, "_pending", ""), ours=True)
        elif kind == "response.cancel":
            self.cancels += 1
            # Two terminal frames, canceled and then done: 23 cancels out of
            # 23 against the real endpoint on 2026-10-05. Neither carries a
            # response id.
            await self._send(ws, {"type": "response.canceled"})
            await self._send(ws, {"type": "response.done"})
            if self.scramble_after_cancel:
                # What the real endpoint did right after a cancel in the
                # 2026-10-05 probes (8 of 8 on a real streamer recording): a
                # new transcription item opens at the done and never gets a
                # word. Off by default; its other shape (lines cut after
                # 0.2-0.3 s) is not modelled.
                await self._send(
                    ws,
                    {
                        "type": "conversation.item.input_audio_transcription.started",
                        "item_id": self._next_item(),
                    },
                )
        elif kind == "session.close":
            await self._send(ws, {"type": "session.closed"})
        elif kind not in _KNOWN:
            # Outside the vocabulary: an error frame, and then the hang-up,
            # 0.0-0.1 s later (measured 2026-09-21, polling once a second for
            # 25 s with and without uplink keep-alive). Both halves are real
            # and the repo only ever recorded one each: duplex.py's header said
            # "Not an error frame: a 1006", volcano.py:32-35 said the names all
            # answered `unknown event name` and said nothing about the close.
            # The error frame is why a client can log WHICH name it got wrong;
            # the close is why it still costs the session.
            await self._send(
                ws,
                {
                    "type": "error",
                    "event_id": str(frame.get("event_id") or ""),
                    "error": {
                        "type": "Bad Request",
                        "code": "45000000",
                        "message": f"unknown event name:{kind}",
                    },
                },
            )
            await ws.close(code=1006)

    async def _on_create(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        if self._refuse_session:
            await self._send(
                ws, {"type": "error", "error": {"type": "invalid_request_error", "message": "拒绝"}}
            )
            return
        session = frame.get("session") or {}
        self.instructions = str(session.get("instructions") or "")
        self._create_instructions = self.instructions
        self._turned = False
        self.tools = list(session.get("tools") or [])
        dialog = list(
            ((frame.get("extension") or {}).get("dialog") or {}).get("dialog_context") or []
        )
        # Odd length and the endpoint keeps NONE of it, silently. The session
        # still comes up, so a client only finds out by asking her.
        self.context_refused = bool(dialog) and len(dialog) % 2 == 1
        self.context = [] if self.context_refused else dialog
        self._refuse(
            "dialog_context 顺序错了：服务端按位置配对，不看 role，会把话安到另一个人头上",
            ok=_in_order(self.context),
        )
        self._created = True
        await self._send(ws, {"type": "session.created", "session": {"id": self.session_id}})
        self._ready.set()

    async def _send(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(json.dumps(frame))

    # ------------------------------------------------------------ scripting

    async def say(
        self, text: str, *, ours: bool, audio: bytes = b"\x01\x02", finish: bool = True
    ) -> None:
        """One reply. `ours` decides the tts_type, which is the whole game.

        On her own turns the first characters arrive BEFORE
        `response.output_audio.started` — eleven of them in one measured run —
        so a client that books the turn on the audio frame throws away the
        start of everything she says. This fake used to send the audio frame
        first, which is precisely why it certified a client that did.
        """
        ws = self._ws
        assert ws is not None
        self._turned = True
        head, tail = (text[:_AHEAD], text[_AHEAD:]) if not ours else ("", "")
        for ch in head:
            await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
        await self._send(
            ws, {"type": "response.output_audio.started", "tts_type": _OURS if ours else _HERS}
        )
        if not ours:
            # Only her own turns stream text; the TTS path sends none, which
            # is why the adapter has to report the words it supplied.
            for ch in tail:
                await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
            await self._send(ws, {"type": "response.output_text.done", "text": text})
        await self._send(
            ws,
            {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        )
        if not finish:
            # Left open on purpose: the adapter's reply hold is timed, and a
            # reply that finishes inside its own hold flushes at once — so a
            # test of the TIMER needs a reply that has not finished yet.
            return
        await self._send(ws, {"type": "response.output_audio.done"})
        await self._send(ws, {"type": "response.done"})

    async def more(self, text: str, *, audio: bytes = b"\x03\x04") -> None:
        """Her turn goes on: more words and another audio frame on a reply
        left open by `say(finish=False)`."""
        ws = self._ws
        assert ws is not None
        for ch in text:
            await self._send(ws, {"type": "response.output_text.delta", "delta": ch})
        await self._send(
            ws,
            {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        )

    async def end_reply(self) -> None:
        """The endpoint finishes a reply left open by `say(finish=False)` on
        its own: the audio done, then one response.done and nothing else."""
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "response.output_audio.done"})
        await self._send(ws, {"type": "response.done"})

    async def wants_events(self, call_id: str = "call-1") -> None:
        """She calls the pull tool, the way she does after a streamer turn.

        Only meaningful when the client declared one: `tools` is recorded off
        session.create so a test can assert the declaration went out at all.
        """
        ws = self._ws
        assert ws is not None
        self._refuse("没声明 tools 就不该收到工具调用", ok=bool(self.tools))
        await self._send(
            ws,
            {
                "type": "response.function_call_arguments.done",
                "items": [{"call_id": call_id, "name": "get_live_events", "arguments": "{}"}],
            },
        )

    def _next_item(self) -> str:
        self._item_seq += 1
        item_id = f"item-{self._item_seq}"
        self.items.append(item_id)
        return item_id

    @staticmethod
    def _delta(item_id: str, so_far: str) -> dict[str, Any]:
        return {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": item_id,
            "content_index": 1,
            "delta": so_far,
        }

    @staticmethod
    def _completed(item_id: str, text: str) -> dict[str, Any]:
        # The final words ride in `text` (measured 2026-10-05). An adapter
        # reading `transcript` gets nothing and has to rebuild them from the
        # deltas — which is what this fake used to model, wrongly.
        return {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item_id,
            "content_index": 1,
            "text": text,
        }

    async def hear(self, transcript: str, *, flap: str = "", final: str | None = None) -> None:
        """The streamer speaks. Deltas are cumulative; `completed` carries the
        final words in `text`, which may differ from the last delta (`final`).

        `flap` replays the tail the real transcriber changes its mind about:
        measured as 「…游戏」「…游戏？」「…游戏」「…游戏？」, punctuation going
        back and forth after the words have settled. A client that measures
        the next increment against the LAST value sends that mark twice.
        """
        ws = self._ws
        assert ws is not None
        item_id = self._next_item()
        await self._send(
            ws, {"type": "conversation.item.input_audio_transcription.started", "item_id": item_id}
        )
        steps = [transcript[:i] for i in range(1, len(transcript) + 1)]
        if flap:
            steps += [transcript + flap, transcript, transcript + flap]
        for step in steps:
            await self._send(ws, self._delta(item_id, step))
        await self._send(ws, self._completed(item_id, steps[-1] if final is None else final))

    async def hear_without_ending(self, transcript: str) -> None:
        """The streamer starts a line and the completed frame has not come.

        Either he is still talking (follow with `still_hearing`, then
        `hear_tail`), or the frame is lost. A lost one was reported from live
        sessions on 2026-09-16/17 and never caught again in the probes of
        2026-10-05 (16 items, 15 completed, the 16th abandoned by our own
        commit) — so this is the backstop's case, kept whether or not the
        endpoint really does it. Without the backstop it would end her
        talking for the rest of the session.
        """
        ws = self._ws
        assert ws is not None
        item_id = self._next_item()
        await self._send(
            ws, {"type": "conversation.item.input_audio_transcription.started", "item_id": item_id}
        )
        for i in range(1, len(transcript) + 1):
            await self._send(ws, self._delta(item_id, transcript[:i]))
        self._open_item = (item_id, transcript)

    async def still_hearing(self, so_far: str) -> None:
        """One more cumulative delta on a line already open — the streamer
        carrying on. A client timing its backstop from the START of the line
        cuts him off here; one timing it from the last sign of him does not."""
        ws = self._ws
        assert ws is not None
        assert self._open_item is not None, "still_hearing 之前要先 hear_without_ending"
        item_id = self._open_item[0]
        await self._send(ws, self._delta(item_id, so_far))
        self._open_item = (item_id, so_far)

    async def hear_tail(self, transcript: str) -> None:
        """Finish an open line with its completed frame, `text` carrying the
        whole line. The endpoint has no idea whether the client's backstop
        already closed it, so the ending looks entirely ordinary."""
        ws = self._ws
        assert ws is not None
        assert self._open_item is not None, "hear_tail 之前要先 hear_without_ending"
        item_id = self._open_item[0]
        await self._send(ws, self._completed(item_id, transcript))
        self._open_item = None

    async def drop(self, code: int = 1011) -> None:
        """The socket dies under the client the way a network blip looks from
        our side: no session.close, no error frame, just an abnormal close."""
        ws = self._ws
        assert ws is not None
        await ws.close(code=code, reason="dropped")

    async def fail(self, code: str = "50000000", message: str = "模型推理出错") -> None:
        ws = self._ws
        assert ws is not None
        await self._send(ws, {"type": "error", "error": {"code": code, "message": message}})
