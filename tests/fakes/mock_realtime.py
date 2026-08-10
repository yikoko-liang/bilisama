"""In-process fake Realtime server.

The point is not that it accepts a connection. The point is that each provider
quirk we have to code around becomes a reproducible failure mode here, so the
rules that avoid them are covered by tests rather than by good intentions in a
document. The corollary decides every judgement call below: wherever this fake is
safer or tidier than the real server, it certifies a broken client as correct.
When in doubt, model the ugly thing.

Constructed from a Capabilities plus a Codec, so one test class can run against
all three provider shapes.

Loads no models, opens no sockets to the outside, and emits canned PCM.

What of plan section 3.3 is modelled. The bracketed names are greppable in
tests/unit/test_mock_realtime.py:

- The headline rule — an in-band injection against an open speculative turn is
  swallowed and wedges the connection — has a fault, Fault.WEDGE_ON_INJECTION,
  and a control [test_out_of_band_injection_is_immune_to_the_wedge]. Both routes
  to an open turn are covered: the streamer starting to talk, and the streamer
  talking over the assistant [test_barge_in_reopens_the_speculative_window].
- Rule 3, cancel cannot pre-empt a reply before its first token: failure case
  and control [test_cancel_before_the_first_token_is_ignored_and_the_reply_still_speaks,
  test_cancel_after_the_first_token_is_honoured].
- Rule 5, a response.create during the pending window is admitted and the two
  generations share one response id: failure case and control
  [test_response_create_during_the_pending_window_is_admitted,
  test_the_slot_refuses_a_create_once_the_implicit_reply_has_spoken].
- Rule 7, never stop appending: failure case and control
  [test_speculative_window_stays_open_while_the_append_stream_is_starved,
  test_speculative_window_closes_once_the_reopen_audio_has_flowed]. The budget
  the window closes on is per turn, not per connection
  [test_a_new_turn_starts_the_reopen_budget_over].
- Rule 8, commit and the two clears: one case per event, each with the error
  upstream really sends, plus the three commits that decide between silence and
  a complaint — audio buffered, buffer already committed, nothing appended that
  carried audio — plus append as the control
  [test_forbidden_audio_buffer_events_draw_their_own_upstream_errors,
  test_commit_with_audio_buffered_draws_nothing_at_all,
  test_a_second_commit_with_nothing_appended_in_between_is_refused,
  test_an_append_carrying_no_audio_does_not_arm_the_commit_buffer,
  test_audio_buffer_append_is_accepted].
- Rule 2, the watchdog: Fault.STALL_RESPONSE is the failure case
  [test_stalled_response_never_completes]. It has no dedicated control; every
  other reply in the file finishes.
- Rule 4, do not pair response.created with response.done: unconditional
  behaviour rather than a fault, since implicit replies never send created
  [test_implicit_reply_never_sends_response_created].

Rules 1 and 6 are not modelled. Rule 1 (`speculative_quiet`) is a condition on
our own scheduler with no server side to fake. Rule 6 turns on `interrupt_response`:
upstream reads it (handlers/audio.py:113-114) and this fake does not, so barge_in()
always interrupts.

Three more quirks are modelled although they are not among the eight, because
each costs a debugging session every time it is met cold. conversation.item.create
is deferred while a reply generates, and the acks come back in arrival order
[test_item_create_during_a_reply_is_deferred_until_it_ends,
test_item_create_is_acked_at_once_when_nothing_is_generating,
test_deferred_items_are_acked_in_arrival_order]. Barge-in sends response.done
before speech_started [test_barge_in_sends_response_done_before_speech_started].
A cancelled text reply never sends output_text.done, whether the cancel lands
mid-stream or after the last delta
[test_cancelled_text_reply_sends_no_output_text_done,
test_a_reply_cancelled_after_its_last_delta_still_sends_no_text_done,
test_completed_text_reply_does_send_output_text_done].

The static half of rule 8 — that L3 never sends those events in the first place —
needs an L3 to check. This server rejects them on arrival, which is the half a
server can be held to, and records every frame it receives, so the other half is
one Recorded.count away once L3 exists.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.capabilities import Capabilities


class Fault(StrEnum):
    """Scriptable server behaviours a test can switch on.

    Most are provider quirks we have to code around; EMIT_TOOL_CALL and
    SESSION_LIMIT are just states worth reaching. Behaviour upstream produces
    unconditionally does not belong here: putting it behind a flag would make the
    default path kinder than the real server. See _on_item_create, which defers
    without a flag because upstream defers without one.
    """

    # An in-band injection lands while a speculative turn is still open. The
    # create is acked and then every frame the generation produces is dropped,
    # so no response.done ever arrives and the slot stays occupied forever.
    #
    # Verified live on v0.2.12-40-g68f0604 (tests/integration/test_real_server.py):
    # the real server loses the reply but SELF-HEALS — the injected create sets
    # in_response, so the streamer's resumed speech takes the barge-in path,
    # cancels the injected reply and frees the slot. The fake keeps the trap
    # permanent on purpose: harsher than the real server is the one safe
    # direction, since a client that survives the permanent wedge also survives
    # the transient one.
    WEDGE_ON_INJECTION = "wedge_on_injection"
    # Reply hangs forever, to exercise the client-side watchdog.
    STALL_RESPONSE = "stall_response"
    # Every pipeline slot is busy, so the connection is refused at the handshake.
    SESSION_LIMIT = "session_limit"
    # Emit a tool call.
    EMIT_TOOL_CALL = "emit_tool_call"


@dataclass(slots=True)
class Script:
    """What one test wants to reproduce."""

    faults: set[Fault] = field(default_factory=set)
    reply_text: str = "好的，我看到了。"
    delta_chunks: int = 3
    # Gap between deltas, so a test can slip an interruption in mid-reply.
    delta_interval_s: float = 0.0
    audio_ms_per_delta: int = 40
    # How much audio the client must append after speech_stopped before the
    # speculative window closes. An audio clock, not a wall clock: upstream
    # measures elapsed as `audio_start_ms - _last_final_audio_ms`
    # (vad_handler.py:255-259) and keeps the turn reopenable while that stays
    # within unanswered_reopen_ms (vad_handler.py:268; default 7000 at :75,
    # raised at :117-121 to the largest of speculative_reopen_ms, the requested
    # value and smart_turn_max_wait_ms — 800, 7000 and 2000 by default, so the
    # effective default is the 7000 mirrored here). Stop sending frames and the
    # window freezes open.
    #
    # Not upstream's speculative_reopen_ms (800). That one is a wall-clock grace
    # that only delays *committing* the turn (vad_handler.py:766-773 into
    # speculative_turns.py:159-177), which is a different mechanism on a
    # different clock. Neither it nor the commit that closes the window early
    # once the assistant has spoken (vad_handler.py:249-254) is modelled here.
    # Both omissions leave the trap open longer than upstream would, so they can
    # make a client's life harder but cannot certify a broken one.
    unanswered_reopen_ms: int = 7000

    def has(self, fault: Fault) -> bool:
        return fault in self.faults


@dataclass(slots=True)
class _Reply:
    """One reply the server has accepted. It may not be generating yet: see `started`.

    Per-response rather than per-connection: on GA an out-of-band reply runs
    alongside the default conversation (Capabilities.out_of_band_exempt_from_slot),
    so a single flag cannot say which reply a done event belongs to.
    """

    holds_slot: bool
    # Decided at creation and never re-read from caps: session defaults drive
    # only the implicit VAD turn, while an explicit create is exactly what the
    # client sent — absent output_modalities means AUDIO (utils/utils.py:20-23,
    # the default-is-audio rule), no matter what the session was put into.
    # Getting this wrong the kind way is how the fake certified clients that
    # forget to ask for text (the §15.8 finding).
    text_reply: bool = False
    # Upstream's st.in_response. Only the server's own VAD turn is ever accepted
    # without it: that one is merely response_pending from the moment its request
    # is queued (service.py:474, :506) until its first token flips in_response
    # (handlers/response.py:42-50). Every guard upstream reads in_response alone,
    # which is why the pending window is where the protection is missing.
    started: bool = True
    # Resolved on first use, never at construction — see _ensure_response_id.
    rid: str | None = None
    is_open: bool = True
    # Held closed while the reply is merely pending. Per reply rather than per
    # connection, so a second implicit reply cannot steal the first one's wakeup.
    first_token: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class Recorded:
    """What the server received. Used to assert on client behaviour."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def count(self, wire_type: str) -> int:
        return sum(1 for e in self.events if e.get("type") == wire_type)


# The client's append stream is 16 kHz mono s16 on the s2s path — that is the
# rate we hand the engine (src/bilisama/config/schema.py:30, TurnConfig.sample_rate,
# rendered into config/s2s/bilisama-s2s.json) and the rate its VAD ticks on
# (vad_handler.py:64). So 32 bytes per millisecond. Server *output* is 24 kHz,
# 48 bytes/ms — see _pcm. Measuring the uplink at the downlink rate would demand
# 1.5x too much audio before the speculative window closes, which is the timing
# this file exists to pin down.
_INPUT_BYTES_PER_MS = 32


def _pcm(ms: int, *, rate: int = 24000, freq: float = 220.0) -> bytes:
    """Canned server output: 24 kHz mono s16, unlike the 16 kHz uplink above."""
    n = int(rate * ms / 1000)
    return struct.pack(
        f"<{n}h", *(int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
    )


class MockRealtimeServer:
    """A fake Realtime server on an ephemeral loopback port.

    Usage::

        async with MockRealtimeServer(caps=capabilities.S2S) as server:
            ...  # connect to server.url
            assert server.recorded.count("response.create") == 1
    """

    def __init__(
        self,
        *,
        caps: Capabilities | None = None,
        codec: dia.Codec | None = None,
        script: Script | None = None,
    ) -> None:
        self.caps = caps or caps_mod.S2S
        self.codec = codec or dia.GA
        self.script = script or Script()
        self.recorded = Recorded()
        self._server: Any = None
        self._conn: ServerConnection | None = None
        self._port = 0
        # Server state, deliberately named after upstream so the two read together.
        self._replies: list[_Reply] = []
        self._current_response_id: str | None = None
        self._next_response_id = 0
        self._deferred: list[dict[str, Any]] = []
        self._speculative_open = False
        self._appended_since_stop_ms: int | None = None
        self._audio_buffer_has_data = False
        # Total appended milliseconds: the audio clock §2.8 nominates as the
        # ground-truth timebase (s2s computes audio_end_ms from sample counts).
        self._audio_ms_total = 0
        self._user_item_seq = 0
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> MockRealtimeServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self._port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/v1/realtime"

    # ------------------------------------------------------------ response bookkeeping

    def _start_reply(
        self, *, holds_slot: bool, started: bool = True, text_reply: bool = False
    ) -> _Reply:
        reply = _Reply(holds_slot=holds_slot, started=started, text_reply=text_reply)
        self._replies.append(reply)
        return reply

    def _ensure_response_id(self, reply: _Reply) -> str:
        """Resolve this reply's response id the way upstream's _ensure_response does.

        Upstream keeps exactly one response id per connection, st.current_response_id:
        response.create mints it outright (handlers/response.py:224) and a VAD
        turn's first token mints one only when the slot is empty
        (handlers/response.py:42-50). So when a create is admitted during the
        pending window, both generations resolve to the same id — the shared id
        plan section 3.3 rule 5 warns about — and whichever one outlives the
        other mints a fresh, never-announced id once the first response.done has
        cleared the slot (handlers/response.py:71-75).

        A reply exempt from the slot has no connection-level id to collide with,
        so it keeps one of its own.

        Args:
            reply: The reply about to emit a frame.

        Returns:
            The response id that frame must carry.
        """
        if not reply.holds_slot:
            if reply.rid is None:
                reply.rid = self._mint_response_id()
            return reply.rid
        if self._current_response_id is None:
            self._current_response_id = self._mint_response_id()
        reply.rid = self._current_response_id
        return reply.rid

    def _mint_response_id(self) -> str:
        self._next_response_id += 1
        return f"resp_{self._next_response_id}"

    def _slot_holder(self) -> _Reply | None:
        """The reply occupying the single response slot, if any.

        Only a *started* reply counts. Upstream's single-response guard reads
        st.in_response and nothing else (handlers/response.py:202-206), and
        in_response is still false while a VAD turn is merely response_pending,
        so a create arriving in that window is admitted rather than refused.
        Filtering on holds_slot alone would invent a protection upstream does not
        have, which is exactly what rule 5 tells the client not to rely on.
        """
        return next((r for r in self._replies if r.holds_slot and r.started), None)

    def _pending_slot_reply(self) -> _Reply | None:
        """A slot-holding reply that has been accepted but has not spoken yet."""
        return next((r for r in self._replies if r.holds_slot and not r.started), None)

    def _generating(self) -> bool:
        """Whether upstream's st.in_response would be true for this connection."""
        return any(r.started for r in self._replies)

    # ------------------------------------------------------------ server-side pushes

    async def send(self, event: dia.ServerEvent, **payload: Any) -> None:
        """Send an event, translated into the current dialect's wire name."""
        if self._conn is None:
            raise RuntimeError("no client has connected yet")
        body = {"type": self.codec.wire_name(event), **payload}
        await self._conn.send(json.dumps(body))

    async def speech_started(self) -> None:
        """Streamer starts talking, which opens a fresh speculative turn.

        The reopen budget starts over rather than carrying on from the last
        turn. Upstream measures it from _last_final_audio_ms, which only a
        finalised stretch of speech sets (vad_handler.py:766) and a new turn
        clears (:212, inside _start_new_turn at :204-214, reached from :350 when
        speech starts and the old turn is not reopened). With no reference point
        there is no elapsed time to compare against the cap (:268), so a fresh
        turn cannot inherit what the last one spent. A count that carried over
        would shut the window while the streamer is still talking, and shut it
        for good — the next speech_stopped finds _speculative_open already false
        and never re-arms it.

        Modelled as a new turn every time. Upstream can instead reopen the
        previous one and keep counting (:313-330), which shuts the trap sooner
        than this does, so the omission leaves the trap open longer than
        upstream would rather than shorter.
        """
        self._speculative_open = True
        self._appended_since_stop_ms = None
        self._user_item_seq += 1
        await self.send(
            dia.ServerEvent.SPEECH_STARTED,
            audio_start_ms=self._audio_ms_total,
            item_id=f"item_user_{self._user_item_seq}",
        )

    async def speech_stopped(self) -> None:
        """Streamer stops talking. The speculative window stays open a moment longer.

        What closes it is appended audio, not elapsed time (vad_handler.py:255-259,
        :268). A client that stops sending frames freezes the window open, which
        is why the rule is to keep appending silence rather than to stop.
        """
        # Guard, not behaviour: it keeps the counter meaningful only while there
        # is a window to close. Dropping it changes nothing a client can observe,
        # because _speculative_open is already false.
        if self._speculative_open:
            self._appended_since_stop_ms = 0
        await self.send(
            dia.ServerEvent.SPEECH_STOPPED,
            audio_end_ms=self._audio_ms_total,
            item_id=f"item_user_{self._user_item_seq}",
        )

    async def barge_in(self) -> None:
        """Simulate the streamer talking over the assistant.

        Note the order: response.done(cancelled) arrives *before* speech_started.
        That reads backwards, but upstream builds both into one list and sends it
        in that order — on_speech_started extends with finish_response(cancelled,
        turn_detected) first (handlers/audio.py:113-114) and appends the
        speech_started event last (handlers/audio.py:140-147). Upstream's own
        README:185-186 and tests/openai_realtime/test_websocket_router.py:429-431
        describe the reverse order and are stale.

        A merely pending reply dies here too, and silently: the router cancels the
        generation when either in_response or response_pending was set
        (websocket_router.py:773-777), while finish_response builds its done
        events only under in_response (handlers/response.py:274). So the client
        sees nothing for it and it never speaks.
        """
        target = self._slot_holder()
        if target is not None:
            await self._finish_response(target, status="cancelled", reason="turn_detected")
        pending = self._pending_slot_reply()
        if pending is not None:
            self._close_reply(pending)
            pending.first_token.set()  # let its task observe the kill and exit
        # The streamer is talking again, so the trap is armed again: the same
        # event that cancels the reply starts a fresh input item and turn id
        # (handlers/audio.py:108-138), and an in-band injection sent now — the
        # obvious moment, since our scheduler just saw a reply end — lands on a
        # turn that is still speculative. Same bookkeeping as speech_started.
        self._speculative_open = True
        self._appended_since_stop_ms = None
        self._user_item_seq += 1
        await self.send(
            dia.ServerEvent.SPEECH_STARTED,
            audio_start_ms=self._audio_ms_total,
            item_id=f"item_user_{self._user_item_seq}",
        )

    async def emit_implicit_reply(self, *, hold: bool = False) -> None:
        """The turn the server's own VAD starts. Sends no response.created.

        Args:
            hold: Stop before the first token, leaving the reply merely pending.
                Release it with release_pending_reply(). This is the only way to
                observe the pending state, where response.cancel does nothing and
                the single-response guard is missing.
        """
        # The implicit turn follows the session default: the S2S profile's
        # owns_tts=False stands in for patch A pinning the session to text.
        reply = self._start_reply(holds_slot=True, started=False, text_reply=not self.caps.owns_tts)
        # The s2s text pipeline announces nothing (rule 4's "no created" is a
        # TEXT-MODE fact); hosted endpoints DO send response.created for their
        # VAD replies — modelling them as silent hid the unbooked-slot bug for
        # a whole stage (C8).
        if self._is_hosted():
            await self.send(
                dia.ServerEvent.RESPONSE_CREATED,
                response={"id": self._ensure_response_id(reply)},
            )
        if not hold:
            reply.first_token.set()
        task = asyncio.create_task(self._run_response(reply, implicit=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _is_hosted(self) -> bool:
        """DashScope (beta dialect) or OpenAI (the GA profile with the
        out-of-band exemption) — the two announced-implicit shapes."""
        return self.codec.dialect is dia.Dialect.BETA or self.caps.out_of_band_exempt_from_slot

    async def release_pending_reply(self) -> None:
        """Let every held implicit reply emit its first token."""
        for reply in self._replies:
            reply.first_token.set()

    # ------------------------------------------------------------ internals

    async def _handle(self, conn: ServerConnection) -> None:
        self._conn = conn
        try:
            if self.script.has(Fault.SESSION_LIMIT):
                # Upstream refuses at the handshake, not at response.create: the
                # pipeline slot is claimed before any session exists, and a
                # client that loses the race gets one error frame and a 1008
                # close (websocket_router.py:465-475).
                await self._error(
                    "session_limit_reached",
                    "All 1 session slots are in use. Disconnect an existing client first.",
                )
                await conn.close(code=1008, reason="All session slots are in use")
                return
            await self.send(dia.ServerEvent.SESSION_CREATED, session={"id": "mock"})
            async for raw in conn:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.recorded.events.append(event)
                await self._dispatch(event)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conn = None

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == dia.ClientEvent.SESSION_UPDATE.value:
            if self.caps.acknowledges_session_update:
                await self.send(dia.ServerEvent.SESSION_UPDATED, session=event.get("session", {}))
        elif kind == dia.ClientEvent.AUDIO_APPEND.value:
            self._on_audio_append(event)  # recorded, never answered
        elif kind == "input_audio_buffer.commit":
            await self._on_audio_commit()
        elif kind == "output_audio_buffer.clear":
            # A known event that this transport refuses: over WebSocket the
            # unplayed audio sits client-side (websocket_router.py:370-380).
            await self._error(
                "invalid_event_for_transport",
                "output_audio_buffer.clear is only supported on the WebRTC transport.",
            )
        elif kind == dia.ClientEvent.ITEM_CREATE.value:
            await self._on_item_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CREATE.value:
            await self._on_response_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CANCEL.value:
            await self._on_cancel()
        elif kind == dia.ClientEvent.ITEM_TRUNCATE.value and self.caps.item_truncate:
            await self.send(
                dia.ServerEvent.ITEM_TRUNCATED,
                item_id=event.get("item_id"),
                content_index=event.get("content_index", 0),
                audio_end_ms=event.get("audio_end_ms", 0),
            )
        else:
            # Everything upstream cannot parse lands here, input_audio_buffer.clear
            # included: it is absent from the client-event table
            # (service.py:73-81), so parse_client_event returns None and the
            # router answers with this one error (websocket_router.py:343-346).
            await self._error("unknown_or_invalid_event", f"Unknown or invalid event: {kind}")

    def _on_audio_append(self, event: dict[str, Any]) -> None:
        """Bank the audio, then close the speculative window if enough has flowed.

        Counts the milliseconds of audio the client actually appended, so a
        starved stream stops the countdown instead of merely slowing it. Frame
        count is irrelevant: one long append closes the window and a hundred tiny
        ones do not.
        """
        try:
            pcm = base64.b64decode(str(event.get("audio") or ""), validate=True)
        except binascii.Error:
            return  # a frame we cannot decode carries no audio
        if pcm:
            # Upstream only flips this once a full 1024-byte chunk is assembled
            # (handlers/audio.py:80-92, CHUNK_SIZE_BYTES at service.py:66-68).
            # Flipping on any audio errs toward commit being silently accepted,
            # which is the harder case for a client, never the easier one.
            self._audio_buffer_has_data = True
        self._audio_ms_total += len(pcm) // _INPUT_BYTES_PER_MS
        if self._appended_since_stop_ms is None:
            return
        self._appended_since_stop_ms += len(pcm) // _INPUT_BYTES_PER_MS
        if self._appended_since_stop_ms >= self.script.unanswered_reopen_ms:
            self._speculative_open = False
            self._appended_since_stop_ms = None

    async def _on_audio_commit(self) -> None:
        """Commit is recognised upstream, and that is the trap.

        It is a parseable client event (service.py:75) routed to handle_audio_commit
        (websocket_router.py:365-368), which errors only on an empty buffer
        (handlers/audio.py:94-101). Under server VAD a commit with audio in the
        buffer therefore draws *nothing at all* while doing nothing at all — the
        silence is why plan section 3.3 rule 8 forbids sending it, since a client
        waiting for a turn it thinks it forced waits forever.
        """
        if not self._audio_buffer_has_data:
            await self._error(
                "input_audio_buffer_commit_empty", "Input audio buffer is empty, nothing to commit."
            )
            return
        # A successful commit empties the buffer (handlers/audio.py:102), which
        # is what makes the *next* commit the one that answers. Leaving it set
        # would make every commit after the first equally silent.
        self._audio_buffer_has_data = False

    async def _on_item_create(self, event: dict[str, Any]) -> None:
        """Ack the item, or defer it until the reply in flight finishes.

        Deferral is unconditional upstream — `if st.in_response:` with no flag,
        capability or config behind it (handlers/conversation.py:48-52) — so it
        is unconditional here. Behind a Fault the default path would ack promptly
        during a live reply, which upstream never does, and a client could grow a
        dependency on that ack without a single test going red.

        The gate is in_response, not response_pending: an item.create arriving
        while a VAD turn is merely queued is acked immediately.

        Only the s2s server has been read for this. The mock applies it to every
        profile because a fake that acks promptly is the one that certifies a
        broken client.
        """
        if self._generating():
            self._deferred.append(event)
            return
        await self.send(dia.ServerEvent.ITEM_CREATED, item=event.get("item", {}))

    async def _on_response_create(self, event: dict[str, Any]) -> None:
        out_of_band = (event.get("response") or {}).get("conversation") == "none"
        holds_slot = not (out_of_band and self.caps.out_of_band_exempt_from_slot)

        if self.caps.single_response_slot and holds_slot and self._slot_holder() is not None:
            await self._error(
                "conversation_already_has_active_response",
                "Cannot create response while another response is in progress.",
            )
            return

        # Past the guard the create always succeeds and is always announced:
        # handle_response_create returns a ResponseCreatedEvent (handlers/response.py:243-247)
        # and the router sends it before anything downstream can go wrong
        # (websocket_router.py:395-401).
        # Explicit creates carry their own parameters verbatim
        # (handlers/response.py:223): output_modalities==["text"] means text,
        # anything else — including absent — means audio. Patch A only fills
        # the default for the implicit turn; it does not touch this path.
        modalities = (event.get("response") or {}).get("output_modalities")
        reply = self._start_reply(holds_slot=holds_slot, text_reply=modalities == ["text"])
        rid = self._ensure_response_id(reply)
        await self.send(dia.ServerEvent.RESPONSE_CREATED, response={"id": rid})

        if self.script.has(Fault.WEDGE_ON_INJECTION) and not out_of_band and self._speculative_open:
            # The reply was accepted and stamped with the streamer's still-open
            # speculative turn, so every frame it produces is dropped as stale —
            # including the EndOfResponse that would have closed the response
            # (LLM/lm_output_processor.py:81-91). Nothing more arrives, the slot
            # is never released, and every later response.create is refused.
            return

        task = asyncio.create_task(self._run_response(reply, implicit=False))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_cancel(self) -> None:
        # A bare cancel targets the default conversation, i.e. the reply holding
        # the slot. A reply that is only response_pending cannot be cancelled:
        # upstream lifts the generation only when in_response is already true
        # (websocket_router.py:404-406), and finish_response builds the
        # audio.done/response.done pair under the same condition
        # (handlers/response.py:274). The reply then says its piece anyway, so a
        # client that assumes pre-emption worked ends up talking over itself.
        #
        # Upstream also flushes the deferred buffer on the way out of this path,
        # outside the in_response guard (handlers/response.py:311-314). Not
        # modelled: nothing can be deferred here without a reply left to finish
        # it, and _finish_response flushes, so no item can be stranded.
        target = self._slot_holder()
        if target is None:
            return
        await self._finish_response(target, status="cancelled", reason="client_cancelled")

    async def _run_response(self, reply: _Reply, *, implicit: bool) -> None:
        if implicit:
            await reply.first_token.wait()  # held here it is only response_pending
            if not reply.is_open:
                return  # barge_in killed it while it was still pending
            reply.started = True

        if self.script.has(Fault.STALL_RESPONSE):
            await asyncio.sleep(3600)  # let the client watchdog deal with it
            return

        if self.script.has(Fault.EMIT_TOOL_CALL):
            await self.send(
                dia.ServerEvent.FUNCTION_ARGS_DONE,
                response_id=self._ensure_response_id(reply),
                call_id="call_1",
                name="get_stream_status",
                arguments='{"key": "uptime"}',
            )
            await self._finish_response(reply, status="completed")
            return

        text = self.script.reply_text
        step = max(1, len(text) // max(1, self.script.delta_chunks))
        pieces = [text[i : i + step] for i in range(0, len(text), step)]

        for piece in pieces:
            if not reply.is_open:
                return  # cancelled mid-reply
            # Re-resolved per frame, not cached: upstream calls _ensure_response
            # on every assistant event, so a reply that outlives the id it was
            # sharing starts stamping a new one mid-stream.
            rid = self._ensure_response_id(reply)
            if not reply.text_reply:
                # Dialects carry an audio reply's text differently, and the fake
                # must not be kinder than either real server (that hid the
                # muted-voice-box bug once). Beta (DashScope, probed live)
                # streams response.audio_transcript.delta per piece plus a final
                # all-text done. GA (s2s) streams NOTHING per piece — its text
                # arrives as one output_audio_transcript.done PER LLM CHUNK
                # (handlers/response.py:362), sent right here.
                if self._is_hosted():
                    await self.send(dia.ServerEvent.TRANSCRIPT_DELTA, response_id=rid, delta=piece)
                else:
                    await self.send(
                        dia.ServerEvent.TRANSCRIPT_DONE, response_id=rid, transcript=piece
                    )
                await self.send(
                    dia.ServerEvent.AUDIO_DELTA,
                    response_id=rid,
                    delta=base64.b64encode(_pcm(self.script.audio_ms_per_delta)).decode(),
                )
            else:
                await self.send(dia.ServerEvent.TEXT_DELTA, response_id=rid, delta=piece)
            if self.script.delta_interval_s:
                await asyncio.sleep(self.script.delta_interval_s)

        # The loop guard again, for a cancel that lands after the last delta.
        # Upstream builds output_text.done only under `elif status ==
        # "completed"` (handlers/response.py:291), so a cancelled reply has none
        # to send — and one sent from here would arrive *behind* its own
        # response.done, stamped with a freshly minted id, telling a client that
        # treats it as the terminator that the reply ended cleanly.
        if not reply.is_open:
            return
        if reply.text_reply:
            await self.send(
                dia.ServerEvent.TEXT_DONE, response_id=self._ensure_response_id(reply), text=text
            )
        else:
            # Audio streams have terminators too; without them a client cannot
            # tell "the reply finished" from "the network went quiet". Only the
            # beta dialect closes with an all-text transcript done (after its
            # deltas); GA already delivered the text chunk by chunk above.
            rid = self._ensure_response_id(reply)
            if self._is_hosted():
                await self.send(dia.ServerEvent.TRANSCRIPT_DONE, response_id=rid, transcript=text)
            await self.send(dia.ServerEvent.AUDIO_DONE, response_id=rid)
        await self._finish_response(reply, status="completed")

    def _close_reply(self, reply: _Reply) -> None:
        """Drop a reply from the connection's books without sending anything."""
        reply.is_open = False
        if reply in self._replies:
            self._replies.remove(reply)
        if reply.rid is not None and self._current_response_id == reply.rid:
            self._current_response_id = None

    async def _finish_response(
        self, reply: _Reply, *, status: str, reason: str | None = None
    ) -> None:
        if not reply.is_open:
            return
        rid = self._ensure_response_id(reply)  # finish_response mints one too, response.py:275
        self._close_reply(reply)

        payload: dict[str, Any] = {"response": {"id": rid, "status": status}}
        if reason:
            payload["response"]["status_details"] = {"reason": reason}
        await self.send(dia.ServerEvent.RESPONSE_DONE, **payload)
        await self._flush_deferred()

    async def _flush_deferred(self) -> None:
        """Acknowledge items that arrived mid-reply, in arrival order.

        Upstream flushes from finish_response, outside its in_response guard
        (handlers/response.py:311-314), so the ack rides out directly behind the
        response.done in the same batch. The client has to cope with an ack
        arriving long after the request that earned it.

        The order is upstream's too, not a convenience: items are appended as
        they arrive (handlers/conversation.py:48-52) and replayed front to back
        (:74-89), so a client may reconcile a batch of acks to its requests
        positionally. Draining from the other end would certify one that cannot.
        """
        while self._deferred:
            pending = self._deferred.pop(0)
            await self.send(dia.ServerEvent.ITEM_CREATED, item=pending.get("item", {}))

    async def _error(self, code: str, message: str) -> None:
        await self.send(dia.ServerEvent.ERROR, error={"type": code, "message": message})
