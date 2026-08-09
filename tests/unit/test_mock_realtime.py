"""Fidelity tests for the mock server itself.

Tests that pass against an unfaithful mock prove nothing, so this file checks the
mock actually reproduces the provider behaviour it claims to.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets

from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from tests.fakes.mock_realtime import Fault, MockRealtimeServer, Script


async def _recv_until(ws: Any, wire_type: str, *, timeout: float = 1.0) -> dict[str, Any]:
    async def _loop() -> dict[str, Any]:
        while True:
            raw = await ws.recv()
            event: dict[str, Any] = json.loads(raw)
            if event.get("type") == wire_type:
                return event

    return await asyncio.wait_for(_loop(), timeout)


async def _drain(ws: Any, *, seconds: float = 0.15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(seconds):
            while True:
                out.append(json.loads(await ws.recv()))
    except (TimeoutError, websockets.ConnectionClosed):
        pass
    return out


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
            await ws.send(json.dumps({"type": "response.create", "response": {}}))
            names = [e["type"] for e in await _drain(ws)]
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
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        names = [e["type"] for e in await _drain(ws)]
        assert "response.audio.delta" in names
        assert "response.text.delta" not in names


# ------------------------------------------------------------ provider quirks


async def test_rule1_injection_during_speculative_window_wedges_the_connection() -> None:
    """The worst one: an in-band injection against an open speculative turn.

    The reply is swallowed, no done event arrives, and the connection stops
    accepting new responses. This is the entire reason every injection goes
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

        # In-band injection: swallowed whole, not even a done event.
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        assert await _drain(ws) == [], "a speculatively-invalidated reply emits nothing"

        # From here on every response.create is refused: the connection is dead.
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        events = await _drain(ws)
        assert any(
            e.get("error", {}).get("type") == "conversation_already_has_active_response"
            for e in events
        ), "a wedged connection should start refusing new responses"


async def test_rule1_out_of_band_is_immune_to_the_wedge() -> None:
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

        await ws.send(json.dumps({"type": "response.create", "response": {"conversation": "none"}}))
        names = [e["type"] for e in await _drain(ws)]
        assert "response.output_text.delta" in names
        assert "response.done" in names


async def test_rule3_cancel_is_a_noop_before_first_token() -> None:
    """Before its first token an implicit reply is only pending, and cancel is a no-op."""
    script = Script(faults={Fault.CANCEL_IS_NOOP})
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.cancel"}))
        assert await _drain(ws) == [], "cancelling a pending reply should produce nothing"


async def test_rule4_implicit_reply_never_sends_response_created() -> None:
    """In text mode an implicit reply only sends done.

    Any bookkeeping that pairs created with done will drift.
    """
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.emit_implicit_reply()
        names = [e["type"] for e in await _drain(ws)]
        assert "response.created" not in names
        assert "response.done" in names


async def test_rule5_single_slot_rejects_the_second_create() -> None:
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        events = await _drain(ws, seconds=0.4)
        assert any(
            e.get("error", {}).get("type") == "conversation_already_has_active_response"
            for e in events
        )


async def test_openai_ga_lets_out_of_band_run_concurrently() -> None:
    """On GA, out-of-band replies do not consume the single response slot."""
    script = Script(reply_text="一二三四五六", delta_chunks=6, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "response.create", "response": {"conversation": "none"}}))
        events = await _drain(ws, seconds=0.4)
        assert not any(
            "error" in e for e in events
        ), "GA should allow a concurrent out-of-band reply"


async def test_deferred_item_create_gets_a_late_ack() -> None:
    """item.create during a reply returns nothing and is acked once the reply ends.

    Treating the silence as failure and retrying would duplicate the item.
    """
    script = Script(
        faults={Fault.DEFER_ITEM_CREATE},
        reply_text="一二三四",
        delta_chunks=4,
        delta_interval_s=0.02,
    )
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"id": "i1"}}))
        events = await _drain(ws, seconds=0.4)
        names = [e["type"] for e in events]
        done_at = names.index("response.done")
        ack_at = names.index("conversation.item.created")
        assert ack_at > done_at, "the ack should arrive only after the reply finishes"


async def test_barge_in_sends_response_done_before_speech_started() -> None:
    """Counterintuitive but real: done(cancelled) arrives before speech_started."""
    script = Script(reply_text="一二三四五六七八", delta_chunks=8, delta_interval_s=0.02)
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        await asyncio.sleep(0.03)
        await server.barge_in()
        names = [e["type"] for e in await _drain(ws, seconds=0.3)]
        done_at = names.index("response.done")
        started_at = names.index("input_audio_buffer.speech_started")
        assert done_at < started_at


async def test_stalled_response_never_completes() -> None:
    """For the client watchdog. Without one, a stalled reply stalls forever, quietly."""
    async with (
        MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(faults={Fault.STALL_RESPONSE})
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        names = [e["type"] for e in await _drain(ws, seconds=0.2)]
        assert "response.done" not in names, "a stalled reply must not finish by itself"
        assert "response.output_text.delta" not in names


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
        names = [e["type"] for e in await _drain(ws)]
        assert "conversation.item.truncated" in names


async def test_tool_call_round_trip() -> None:
    async with (
        MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(faults={Fault.EMIT_TOOL_CALL})
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        events = await _drain(ws)
        call = next(e for e in events if e["type"] == "response.function_call_arguments.done")
        assert call["name"] == "get_stream_status"


async def test_session_update_ack_follows_capability() -> None:
    for caps, expect_ack in (
        (caps_mod.S2S, True),
        (caps_mod.Capabilities(acknowledges_session_update=False), False),
    ):
        async with MockRealtimeServer(caps=caps) as server, websockets.connect(server.url) as ws:
            await _recv_until(ws, "session.created")
            await ws.send(json.dumps({"type": "session.update", "session": {"instructions": "x"}}))
            names = [e["type"] for e in await _drain(ws)]
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
