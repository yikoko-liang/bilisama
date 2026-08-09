"""Fidelity tests for the mock server itself.

Tests that pass against an unfaithful mock prove nothing, so this file checks the
mock actually reproduces the provider behaviour it claims to. Where the mock is
deliberately harsher than upstream, the test says so and says why the harsher
direction is the safe one.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
import websockets

from bilisama.config.schema import TurnConfig
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from tests.fakes.mock_realtime import _INPUT_BYTES_PER_MS, Fault, MockRealtimeServer, Script


async def _recv_until(ws: Any, wire_type: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """Read frames until one of the given wire type arrives.

    Args:
        ws: An open client connection.
        wire_type: Wire name to wait for.
        timeout: Deadlock guard, not a pacing knob. Generous on purpose, so a
            slow machine cannot turn it into a flake.

    Returns:
        The matching event.

    Raises:
        AssertionError: It never arrived, listing what did.
    """
    seen: list[str] = []

    async def _loop() -> dict[str, Any]:
        while True:
            raw = await ws.recv()
            event: dict[str, Any] = json.loads(raw)
            seen.append(str(event.get("type", "")))
            if event.get("type") == wire_type:
                return event

    try:
        return await asyncio.wait_for(_loop(), timeout)
    except TimeoutError:
        raise AssertionError(f"{wire_type} never arrived in {timeout}s; saw {seen}") from None


async def _collect_through(
    ws: Any, wire_type: str, *, count: int = 1, timeout: float = 2.0
) -> list[dict[str, Any]]:
    """Read frames up to and including the count-th one of the given type.

    Bounds a positive assertion by the event it is waiting for rather than by a
    clock, so a slow machine cannot cut the stream short mid-reply. Negative
    assertions still need _drain: "nothing came back" has no frame to wait on.

    Args:
        ws: An open client connection.
        wire_type: Wire name that ends the collection.
        count: How many of them to wait for.
        timeout: Deadlock guard, not a pacing knob.

    Returns:
        Every frame read, the last being the count-th match.

    Raises:
        AssertionError: They did not all arrive, listing what did.
    """
    out: list[dict[str, Any]] = []

    async def _loop() -> None:
        seen = 0
        while seen < count:
            event: dict[str, Any] = json.loads(await ws.recv())
            out.append(event)
            if event.get("type") == wire_type:
                seen += 1

    try:
        await asyncio.wait_for(_loop(), timeout)
    except TimeoutError:
        got = [str(e.get("type", "")) for e in out]
        raise AssertionError(f"expected {count}x {wire_type} in {timeout}s, got {got}") from None
    return out


async def _await_response_started(ws: Any) -> None:
    """Block until the server has accepted the reply and taken the slot.

    The mock registers the reply and sends response.created in the same step it
    handles the create, so receiving that frame is proof the slot is taken —
    upstream sets in_response inside handle_response_create too
    (handlers/response.py:220), before any generation exists. A sleep would only
    be a hope that the reply task has been scheduled.

    Args:
        ws: An open client connection with a response.create already sent.

    Raises:
        AssertionError: The reply was never accepted.
    """
    await _recv_until(ws, "response.created")


def _index_of(names: list[str], wire_type: str) -> int:
    """Position of an expected event, with a readable failure when it is absent."""
    assert wire_type in names, f"expected {wire_type} in the stream, got {names}"
    return names.index(wire_type)


async def _drain(ws: Any, *, seconds: float = 0.15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(seconds):
            while True:
                out.append(json.loads(await ws.recv()))
    except (TimeoutError, websockets.ConnectionClosed):
        pass
    return out


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("type", "")) for e in events]


def _append_audio(ms: int) -> str:
    """One append frame carrying `ms` of uplink audio.

    Silence, because only the duration counts and not the content: the
    speculative window closes on how many milliseconds the client sent, so one
    long frame closes it and a hundred tiny ones do not.
    """
    return json.dumps(
        {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(bytes(ms * _INPUT_BYTES_PER_MS)).decode(),
        }
    )


def _create(**response: Any) -> str:
    return json.dumps({"type": "response.create", "response": response})


async def test_session_created_on_connect() -> None:
    async with MockRealtimeServer() as server, websockets.connect(server.url) as ws:
        assert (await _recv_until(ws, "session.created"))["type"] == "session.created"


async def test_ga_and_beta_use_different_event_names() -> None:
    """One script, two dialects, different wire names.

    This is what lets one test class run against every provider shape.
    """
    for codec, expected in (
        (dia.GA, "response.output_text.delta"),
        (dia.BETA, "response.text.delta"),
    ):
        async with (
            MockRealtimeServer(
                caps=caps_mod.S2S, codec=codec, script=Script(reply_text="嗨", delta_chunks=1)
            ) as server,
            websockets.connect(server.url) as ws,
        ):
            await _recv_until(ws, "session.created")
            await ws.send(_create())
            names = _types(await _collect_through(ws, "response.done"))
            assert expected in names, f"{codec.dialect} should emit {expected}, got {names}"


async def test_owns_tts_decides_text_vs_audio() -> None:
    """owns_tts decides audio-plus-transcript versus plain text.

    This is the fork the hybrid TTS design hangs on.
    """
    async with (
        MockRealtimeServer(
            caps=caps_mod.DASHSCOPE, codec=dia.BETA, script=Script(delta_chunks=1)
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        names = _types(await _collect_through(ws, "response.done"))
        assert "response.audio.delta" in names
        assert "response.text.delta" not in names


# ------------------------------------------------------------ provider quirks


async def test_in_band_injection_during_a_speculative_turn_wedges_the_connection() -> None:
    """The worst one: an in-band injection against an open speculative turn.

    The create is acknowledged and then the reply vanishes — no deltas, no done —
    and the connection stops accepting new responses. The ack is the cruel part:
    a client that treats response.created as confirmation has already been told
    everything is fine. This is the entire reason every injection goes
    out-of-band.
    """
    script = Script(faults={Fault.WEDGE_ON_INJECTION})
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.speech_started()  # speculative window now open
        await _recv_until(ws, "input_audio_buffer.speech_started")

        await ws.send(_create())
        await _recv_until(ws, "response.created")  # accepted, as upstream accepts it
        assert await _drain(ws) == [], "and then nothing: not a delta, not a done"

        # From here on every response.create is refused: the connection is dead.
        await ws.send(_create())
        events = await _drain(ws)
        assert any(
            e.get("error", {}).get("type") == "conversation_already_has_active_response"
            for e in events
        ), "a wedged connection should start refusing new responses"


async def test_out_of_band_injection_is_immune_to_the_wedge() -> None:
    """The same setup out-of-band is fine: a null turn id short-circuits every
    staleness gate."""
    script = Script(faults={Fault.WEDGE_ON_INJECTION}, reply_text="谢谢老板", delta_chunks=1)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.speech_started()
        await _recv_until(ws, "input_audio_buffer.speech_started")

        await ws.send(_create(conversation="none"))
        names = _types(await _collect_through(ws, "response.done"))
        assert "response.output_text.delta" in names
        assert "response.done" in names


async def test_speculative_window_stays_open_while_the_append_stream_is_starved() -> None:
    """The window closes on the audio clock, not the wall clock.

    Eight frames carrying 80 ms between them, then a wait far longer than the
    whole reopen budget, then one more frame so a wall-clock server would have
    every chance to notice the time. The injection still hits the wedge, so
    neither frames nor elapsed time move the window — only appended milliseconds
    do (vad_handler.py:255-259, :268). That is why the rule is "send silence",
    not "stop sending".

    The sleep is safe in the only direction that matters: waiting longer can
    only help a wall-clock server close its window, never cause a flake.
    """
    script = Script(faults={Fault.WEDGE_ON_INJECTION}, unanswered_reopen_ms=200)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.speech_started()
        await _recv_until(ws, "input_audio_buffer.speech_started")
        await server.speech_stopped()
        await _recv_until(ws, "input_audio_buffer.speech_stopped")

        for _ in range(8):
            await ws.send(_append_audio(10))  # 80 ms in eight frames
        await asyncio.sleep(script.unanswered_reopen_ms / 1000 * 1.5)
        await ws.send(_append_audio(10))  # still streaming, still starved: 90 ms

        await ws.send(_create())
        await _recv_until(ws, "response.created")
        assert await _drain(ws) == [], "the window is still open, so the injection is swallowed"


async def test_speculative_window_closes_once_the_reopen_audio_has_flowed() -> None:
    """The control: one frame carrying the whole budget, and the identical
    injection completes.

    One frame against the starved test's eight, and no elapsed time at all, so
    the pair can only be explained by milliseconds of audio. Without this
    counterpart the wedge tests cannot tell a correctly modelled trap from a mock
    that is permanently broken.
    """
    script = Script(
        faults={Fault.WEDGE_ON_INJECTION},
        reply_text="谢谢老板",
        delta_chunks=1,
        unanswered_reopen_ms=200,
    )
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.speech_started()
        await _recv_until(ws, "input_audio_buffer.speech_started")
        await server.speech_stopped()
        await _recv_until(ws, "input_audio_buffer.speech_stopped")

        await ws.send(_append_audio(script.unanswered_reopen_ms))  # one frame, budget met
        await ws.send(_create())
        names = _types(await _collect_through(ws, "response.done"))
        assert "response.output_text.delta" in names
        assert "response.done" in names


def test_the_append_clock_matches_the_audio_we_actually_send() -> None:
    """The mock's uplink clock and the engine config we ship have to agree.

    Drift here is invisible — both sides keep passing on their own — while every
    speculative-window test silently measures a different amount of audio than
    the client will really append. The uplink is 16 kHz mono s16; the 24 kHz rate
    belongs to the server's output and must not leak into this constant.
    """
    turn = TurnConfig()
    assert turn.sample_rate * 2 // 1000 == _INPUT_BYTES_PER_MS, "mono s16 at the configured rate"
    assert turn.unanswered_reopen_ms == Script().unanswered_reopen_ms


async def test_cancel_with_no_reply_in_flight_is_silent() -> None:
    """An unmatched cancel draws neither a done nor an error.

    A client watchdog can fire after the reply already finished, so this has to
    be harmless rather than a protocol violation.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.cancel"}))
        assert await _drain(ws) == [], "cancelling nothing should produce nothing"


async def test_cancel_before_the_first_token_is_ignored_and_the_reply_still_speaks() -> None:
    """Pre-empting a reply that has not spoken yet does not work.

    Upstream lifts the generation only when in_response is already true
    (websocket_router.py:404-406) and builds the done events under the same
    condition (handlers/response.py:274); before that the reply is merely
    response_pending, so the cancel vanishes and the reply says its piece anyway.
    A client that assumes pre-emption worked and queues its own line alongside
    ends up with two voices.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply(hold=True)
        await ws.send(json.dumps({"type": "response.cancel"}))
        assert await _drain(ws) == [], "the cancel vanishes: no done, not even an error"

        await server.release_pending_reply()
        done = await _recv_until(ws, "response.done")
        assert done["response"]["status"] == "completed", "the held reply talks regardless"


async def test_cancel_after_the_first_token_is_honoured() -> None:
    """The control: the same cancel one token later does cancel.

    It exists so the test above reads as "cancel is state-dependent" rather than
    "this mock ignores cancel".
    """
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply()
        await _recv_until(ws, "response.output_text.delta")
        await ws.send(json.dumps({"type": "response.cancel"}))

        done = await _recv_until(ws, "response.done")
        assert done["response"]["status"] == "cancelled"
        assert done["response"]["status_details"]["reason"] == "client_cancelled"


async def test_implicit_reply_never_sends_response_created() -> None:
    """In text mode an implicit reply only sends done.

    Any bookkeeping that pairs created with done will drift.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply()
        names = _types(await _collect_through(ws, "response.done"))
        assert "response.created" not in names
        assert "response.done" in names


async def test_single_response_slot_rejects_a_second_in_band_create() -> None:
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        await _await_response_started(ws)
        await ws.send(_create())
        events = await _collect_through(ws, "error")
        assert any(
            e.get("error", {}).get("type") == "conversation_already_has_active_response"
            for e in events
        )


async def test_response_create_during_the_pending_window_is_admitted() -> None:
    """Plan section 3.3 rule 5, the failure case: the server does not protect us here.

    A turn the server's own VAD started is only response_pending until its first
    token, and upstream's single-response guard reads in_response alone
    (handlers/response.py:202-206). So a create arriving in that window is
    admitted, both generations run, and they share the one connection-scoped
    response id (handlers/response.py:42-50, :224). Then the first done to arrive
    clears that id and the survivor stamps a fresh one nobody ever announced.

    The lesson is that our own state machine has to serialise this: the server
    will not, and a mock that answered with an error would teach the opposite.
    """
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply(hold=True)  # response_pending, not generating

        await ws.send(_create())
        created = await _recv_until(ws, "response.created")  # admitted, not refused
        rid = created["response"]["id"]
        await server.release_pending_reply()

        # Seven is one more delta than a single reply can produce, so both
        # generations really are running — and every one of them carries the id
        # this client was handed for its own reply.
        frames = await _collect_through(ws, "response.output_text.delta", count=7)
        deltas = [e for e in frames if e["type"] == "response.output_text.delta"]
        assert [e["response_id"] for e in deltas] == [rid] * 7, "two generations, one id"
        assert not any(e["type"] == "error" for e in frames), "upstream never complains here"

        tail = await _collect_through(ws, "response.done", count=2)
        assert "response.created" not in _types(tail), "only one create was ever announced"
        done_ids = [e["response"]["id"] for e in tail if e["type"] == "response.done"]
        assert rid in done_ids, "one done closes the id the client knows"
        assert len(set(done_ids)) == 2, f"the other closes an id it never saw: {done_ids}"


async def test_the_slot_refuses_a_create_once_the_implicit_reply_has_spoken() -> None:
    """The control for rule 5: the same create, one token later, is refused.

    Same reply, same connection — only in_response has flipped
    (handlers/response.py:42-50). Without this pair the test above reads as "this
    mock has no slot check" instead of "the slot check has a hole in it".
    """
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply()
        await _recv_until(ws, "response.output_text.delta")  # in_response is true now

        await ws.send(_create())
        refusal = await _recv_until(ws, "error")
        assert refusal["error"]["type"] == "conversation_already_has_active_response"


async def test_ga_concurrent_replies_each_get_their_own_done() -> None:
    """Two replies in flight at once, each ending with a done carrying its own id.

    On GA an out-of-band reply does not consume the single response slot, so both
    run. One shared response id cannot express this: the second reply would
    inherit the first one's bookkeeping, so one of the two dones goes missing and
    the one that survives is labelled with the wrong reply.
    """
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        first = await _recv_until(ws, "response.created")
        await ws.send(_create(conversation="none"))
        events = [first, *await _collect_through(ws, "response.done", count=2)]

        created = [e["response"]["id"] for e in events if e["type"] == "response.created"]
        done = [e["response"]["id"] for e in events if e["type"] == "response.done"]
        assert len(created) == 2, f"both replies should announce themselves, got {created}"
        assert len(set(created)) == 2, f"two replies, two ids: {created}"
        assert sorted(done) == sorted(
            created
        ), f"every reply needs its own done: {created} vs {done}"
        assert not any("error" in e for e in events)


async def test_ga_out_of_band_reply_does_not_block_the_default_conversation() -> None:
    """The slot exemption has to hold in both orders, not just out-of-band-last."""
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create(conversation="none"))
        first = await _recv_until(ws, "response.created")
        await ws.send(_create())
        events = [first, *await _collect_through(ws, "response.done", count=2)]

        assert not any(
            "error" in e for e in events
        ), "the slot is free: an out-of-band reply does not hold it"
        created = [e["response"]["id"] for e in events if e["type"] == "response.created"]
        done = [e["response"]["id"] for e in events if e["type"] == "response.done"]
        assert len(set(created)) == 2
        assert sorted(done) == sorted(created)


async def test_bare_cancel_does_not_touch_an_out_of_band_reply() -> None:
    """A bare cancel targets the default conversation, which here has no reply.

    Cancelling the out-of-band reply instead would silence a background errand
    the streamer never interrupted.
    """
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create(conversation="none"))
        await _recv_until(ws, "response.created")
        await ws.send(json.dumps({"type": "response.cancel"}))
        done = (await _collect_through(ws, "response.done"))[-1]

        assert done["response"]["status"] == "completed", "the bare cancel was not for this reply"
        assert await _drain(ws) == [], "and the reply ends exactly once"


async def test_item_create_during_a_reply_is_deferred_until_it_ends() -> None:
    """item.create during a reply returns nothing and is acked once the reply ends.

    No fault flag switches this on, because upstream has no flag either
    (handlers/conversation.py:48-52). Treating the silence as failure and
    retrying would duplicate the item.
    """
    script = Script(reply_text="一二三四", delta_chunks=4, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        await _await_response_started(ws)
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"id": "i1"}}))
        names = _types(await _collect_through(ws, "conversation.item.created"))
        done_at = _index_of(names, "response.done")
        ack_at = _index_of(names, "conversation.item.created")
        assert ack_at > done_at, "the ack should arrive only after the reply finishes"


async def test_item_create_is_acked_at_once_when_nothing_is_generating() -> None:
    """The control, and the distinction the deferral turns on.

    Upstream defers on in_response, not on response_pending
    (handlers/conversation.py:49), so an item.create that arrives while a VAD
    turn is merely queued is acked immediately. Without this pair the test above
    reads as "this mock always defers".
    """
    script = Script(reply_text="一二三四", delta_chunks=4, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"id": "i1"}}))
        assert (await _recv_until(ws, "conversation.item.created"))["item"]["id"] == "i1"

        await server.emit_implicit_reply(hold=True)  # queued, not yet generating
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"id": "i2"}}))
        ack = await _recv_until(ws, "conversation.item.created")
        assert ack["item"]["id"] == "i2", "response_pending is not in_response"


async def test_two_held_implicit_replies_both_wake_up() -> None:
    """A second VAD turn while the first is still pending must not strand it.

    Nothing upstream stops two turns being queued back to back
    (service.py:474, :506 queue without consulting in_response), so the wakeup
    has to be per reply. A connection-scoped one would be replaced by the second
    turn and the first would wait on an object nobody signals again — a fake that
    hangs, which is the one failure mode a fake must never invent.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply(hold=True)
        await server.emit_implicit_reply(hold=True)
        await server.release_pending_reply()

        frames = await _collect_through(ws, "response.done", count=2)
        dones = [e for e in frames if e["type"] == "response.done"]
        assert len(dones) == 2, f"both replies have to finish, got {_types(frames)}"
        assert all(d["response"]["status"] == "completed" for d in dones)


async def test_barge_in_sends_response_done_before_speech_started() -> None:
    """Counterintuitive but real: done(cancelled) arrives before speech_started."""
    script = Script(reply_text="一二三四五六七八", delta_chunks=8, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        await _await_response_started(ws)
        await _recv_until(ws, "response.output_text.delta")  # interrupt it mid-reply
        await server.barge_in()
        names = _types(await _collect_through(ws, "input_audio_buffer.speech_started"))
        done_at = _index_of(names, "response.done")
        started_at = _index_of(names, "input_audio_buffer.speech_started")
        assert done_at < started_at


async def test_barge_in_kills_a_pending_reply_without_a_word() -> None:
    """A reply interrupted before its first token dies silently and stays dead.

    The router cancels the generation when in_response *or* response_pending was
    set (websocket_router.py:773-777), while the done events are built only under
    in_response (handlers/response.py:274). So there is nothing on the wire for
    it — and a client that waits for a done before starting its own line waits
    forever. Note this is the opposite of a bare response.cancel in the same
    state, which the reply survives.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply(hold=True)
        await server.barge_in()

        frames = await _collect_through(ws, "input_audio_buffer.speech_started")
        assert _types(frames) == [
            "input_audio_buffer.speech_started"
        ], "no done for a pending reply"

        await server.release_pending_reply()
        assert await _drain(ws) == [], "and it never speaks"


async def test_forbidden_audio_buffer_events_draw_their_own_upstream_errors() -> None:
    """The three events rule 8 forbids, each with the error a client will really see.

    They fail for three different reasons and a client branching on the code has
    to meet the one production sends:

    - input_audio_buffer.clear is not in the client-event table at all
      (service.py:73-81), so parsing fails and the router answers
      unknown_or_invalid_event (websocket_router.py:343-346).
    - output_audio_buffer.clear parses but is WebRTC-only
      (websocket_router.py:370-380).
    - commit parses and is routed (websocket_router.py:365-368); on an empty
      buffer it is the one commit case that answers at all
      (handlers/audio.py:94-101).
    """
    expected = {
        "input_audio_buffer.clear": "unknown_or_invalid_event",
        "output_audio_buffer.clear": "invalid_event_for_transport",
        "input_audio_buffer.commit": "input_audio_buffer_commit_empty",
    }
    async with (
        MockRealtimeServer(caps=caps_mod.S2S) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        for kind, code in expected.items():
            await ws.send(json.dumps({"type": kind}))
            error = await _recv_until(ws, "error")
            assert error["error"]["type"] == code, f"{kind} should draw {code}, got {error}"

        # Every frame is recorded, which is how the static half of rule 8 gets
        # checked once L3 exists: no client of ours may send these at all.
        for kind in expected:
            assert server.recorded.count(kind) == 1


async def test_commit_with_audio_buffered_draws_nothing_at_all() -> None:
    """The commit case that matters, and the reason rule 8 forbids the event.

    With audio in the buffer handle_audio_commit returns no error
    (handlers/audio.py:94-101), and under server VAD nothing else happens either.
    A client that sends commit to force a turn gets silence rather than a
    complaint, so the mistake ships. Modelling only the empty-buffer error would
    promise a warning that never comes.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_append_audio(40))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        assert await _drain(ws) == [], "silence, not an error, is what upstream sends here"


async def test_audio_buffer_append_is_accepted() -> None:
    """The control: the one buffer event we may send draws no error.

    Without it the test above only proves the mock can emit errors at all.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_append_audio(40))
        assert await _drain(ws) == [], "append is the frame the client must never stop sending"


async def test_cancelled_text_reply_sends_no_output_text_done() -> None:
    """A cancelled text reply never sends output_text.done.

    Whatever it managed to say has to be reassembled from the deltas already
    received, so a client that waits for the done event waits forever. The tail
    is drained after response.done for exactly that reason: a late one would
    arrive behind the collection boundary, where an assertion bounded by
    response.done cannot see it.
    """
    script = Script(reply_text="一二三四五六七八", delta_chunks=8, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        first = await _recv_until(ws, "response.output_text.delta")
        await server.barge_in()
        events = [first, *await _collect_through(ws, "response.done")]

        assert "response.output_text.done" not in _types(events)
        tail = await _drain(ws)
        assert _types(tail) == [
            "input_audio_buffer.speech_started"
        ], f"nothing follows the done but the barge-in itself, got {_types(tail)}"

        spoken = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
        assert spoken, "the deltas are the only record of what it said"
        assert len(spoken) < len(script.reply_text), f"it was cut off, yet said all of {spoken!r}"


async def test_completed_text_reply_does_send_output_text_done() -> None:
    """The control: a reply that runs to the end does send it, with the full text."""
    script = Script(reply_text="一二三四五六七八", delta_chunks=8)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        events = await _collect_through(ws, "response.done")

        finals = [e for e in events if e["type"] == "response.output_text.done"]
        assert len(finals) == 1, f"exactly one final text, got {_types(events)}"
        assert finals[0]["text"] == script.reply_text


async def test_stalled_response_never_completes() -> None:
    """For the client watchdog. Without one, a stalled reply stalls forever, quietly."""
    async with (
        MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(faults={Fault.STALL_RESPONSE})
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        names = _types(await _drain(ws, seconds=0.2))
        assert "response.done" not in names, "a stalled reply must not finish by itself"
        assert "response.output_text.delta" not in names


async def test_session_limit_is_refused_at_the_handshake() -> None:
    """A full server rejects on connect, not on response.create.

    The pipeline slot is claimed before any session exists, so a client that
    loses the race gets one error frame and a 1008 close, with no session.created
    at all (websocket_router.py:465-475). A client that only handles errors
    arriving mid-session will read this as a connection that came up fine.
    """
    async with (
        MockRealtimeServer(script=Script(faults={Fault.SESSION_LIMIT})) as server,
        websockets.connect(server.url) as ws,
    ):
        first = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
        assert first["type"] == "error", "the error arrives instead of session.created"
        assert first["error"]["type"] == "session_limit_reached"
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 2.0)


async def test_item_truncate_rejected_when_capability_absent() -> None:
    """speech-to-speech has no item.truncate. Clients must handle the rejection
    rather than assume it worked."""
    async with (
        MockRealtimeServer(caps=caps_mod.S2S) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(
            json.dumps({"type": "conversation.item.truncate", "item_id": "i1", "audio_end_ms": 100})
        )
        events = await _drain(ws)
        assert any(e.get("error", {}).get("type") == "unknown_or_invalid_event" for e in events)

    async with (
        MockRealtimeServer(caps=caps_mod.OPENAI_GA) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(
            json.dumps({"type": "conversation.item.truncate", "item_id": "i1", "audio_end_ms": 100})
        )
        assert "conversation.item.truncated" in _types(await _drain(ws))


async def test_tool_call_round_trip() -> None:
    async with (
        MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(faults={Fault.EMIT_TOOL_CALL})
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        events = await _collect_through(ws, "response.function_call_arguments.done")
        call = events[-1]
        assert call["name"] == "get_stream_status"


async def test_session_update_ack_follows_capability() -> None:
    for caps, expect_ack in (
        (caps_mod.S2S, True),
        (caps_mod.Capabilities(acknowledges_session_update=False), False),
    ):
        async with MockRealtimeServer(caps=caps) as server, websockets.connect(server.url) as ws:
            await _recv_until(ws, "session.created")
            await ws.send(json.dumps({"type": "session.update", "session": {"instructions": "x"}}))
            names = _types(await _drain(ws))
            assert ("session.updated" in names) is expect_ack


def test_expr_tags_safe_is_derived_not_stored() -> None:
    """Storing a value you can derive gives you two places to keep in sync."""
    assert caps_mod.S2S.expr_tags_safe is True
    assert caps_mod.DASHSCOPE.expr_tags_safe is False
    assert caps_mod.OPENAI_GA.expr_tags_safe is False


@pytest.mark.parametrize("caps", [caps_mod.S2S, caps_mod.DASHSCOPE, caps_mod.OPENAI_GA])
def test_every_profile_declares_a_turn_detection_type(caps: caps_mod.Capabilities) -> None:
    assert caps.turn_detection_types, "a provider must declare which turn detection it supports"


def test_s2s_does_not_claim_semantic_vad() -> None:
    """It accepts semantic_vad and then ignores it, so claiming support would be a lie."""
    assert "semantic_vad" not in caps_mod.S2S.turn_detection_types
    assert "semantic_vad" in caps_mod.DASHSCOPE.turn_detection_types


# ------------------------------------------------------------ test-harness guards


async def test_the_slot_is_taken_when_the_create_is_accepted_not_at_the_first_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ordering test in this file rests on _await_response_started being real.

    Hold the reply task off its first slice for longer than any sleep a test
    would plausibly have picked. response.created must still come back, a second
    create must already be refused, and not one delta may have arrived in
    between — upstream sets in_response inside handle_response_create
    (handlers/response.py:220), long before a generation exists.
    """
    original = MockRealtimeServer._run_response

    async def held_off(self: MockRealtimeServer, reply: Any, *, implicit: bool) -> None:
        await asyncio.sleep(0.05)
        await original(self, reply, implicit=implicit)

    monkeypatch.setattr(MockRealtimeServer, "_run_response", held_off)

    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(_create())
        await _await_response_started(ws)

        await ws.send(_create())
        frames = await _collect_through(ws, "error")
        assert _types(frames) == ["error"], f"the slot was taken before any output, got {frames}"
        assert (
            frames[-1]["error"]["type"] == "conversation_already_has_active_response"
        ), "the wait returned before the slot was taken"


def test_index_of_names_the_event_that_never_arrived() -> None:
    """A missing event should read as an assertion, not as a ValueError."""
    assert _index_of(["a", "b"], "b") == 1
    with pytest.raises(AssertionError, match=r"expected response\.done in the stream"):
        _index_of(["response.output_text.delta"], "response.done")
