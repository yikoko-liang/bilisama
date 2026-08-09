"""Mock 服务端自身的保真度测试。

Mock 不可信，拿它跑绿的测试就没有意义。所以先证明它真的复现了 §3.3 那八条。
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
    """同一份脚本在两种方言下发出不同的 wire 名。这是三套 Capabilities 跑同一测试的前提。"""
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
            assert expected in names, f"{codec.dialect} 应该发 {expected}，实际 {names}"


async def test_owns_tts_decides_text_vs_audio() -> None:
    """owns_tts=True 出音频和转写，False 出纯文本。这是混合 TTS 的分水岭。"""
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


# ------------------------------------------------------------ §3.3 八条规则


async def test_rule1_injection_during_speculative_window_wedges_the_connection() -> None:
    """最要命的一条：in-band 注入撞上还开着的投机轮次 → 回复被吞，连接卡死。

    这是「所有注入一律走 out-of-band」这个架构决策的全部理由。
    """
    script = Script(faults={Fault.WEDGE_ON_INJECTION})
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await server.speech_started()  # 投机窗口开着
        await _recv_until(ws, "input_audio_buffer.speech_started")

        # in-band 注入：被吞，一个字都不出来，也没有 response.done
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        assert await _drain(ws) == [], "被投机作废的回复不该有任何输出"

        # 之后所有 response.create 都被拒 —— 连接实际已经死了
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        events = await _drain(ws)
        assert any(
            e.get("error", {}).get("type") == "conversation_already_has_active_response"
            for e in events
        ), "卡死之后应该开始拒绝新回复"


async def test_rule1_out_of_band_is_immune_to_the_wedge() -> None:
    """同样的情况走 out-of-band 就没事 —— turn_id=None 让所有投机门禁短路。"""
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
    """隐式回复在首个 token 之前只是 pending，cancel 什么也不做。"""
    script = Script(faults={Fault.CANCEL_IS_NOOP})
    async with (
        MockRealtimeServer(caps=caps_mod.S2S, script=script) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.cancel"}))
        assert await _drain(ws) == [], "pending 状态下的 cancel 不该有任何回应"


async def test_rule4_implicit_reply_never_sends_response_created() -> None:
    """纯文本模式下隐式回复只发 response.done。按 created/done 配对记账一定失衡。"""
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
    """OpenAI GA 上旁路回复不占名额。这个差异由 out_of_band_exempt_from_slot 承载。"""
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
        assert not any("error" in e for e in events), "GA 上旁路回复应该可以并发"


async def test_deferred_item_create_gets_a_late_ack() -> None:
    """回复期间的 item.create 返回空，回复结束后才补发 ack。别当失败重发。"""
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
        assert ack_at > done_at, "ack 应该在回复结束之后才到"


async def test_barge_in_sends_response_done_before_speech_started() -> None:
    """反直觉但确实如此：先 response.done(cancelled)，后 speech_started。"""
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
    """给客户端看门狗准备的。没有它，卡住的连接会静默地永远卡住。"""
    async with (
        MockRealtimeServer(
            caps=caps_mod.S2S, script=Script(faults={Fault.STALL_RESPONSE})
        ) as server,
        websockets.connect(server.url) as ws,
    ):
        await _recv_until(ws, "session.created")
        await ws.send(json.dumps({"type": "response.create", "response": {}}))
        names = [e["type"] for e in await _drain(ws, seconds=0.2)]
        assert "response.done" not in names, "卡住的回复不该自己结束"
        assert "response.output_text.delta" not in names


async def test_item_truncate_rejected_when_capability_absent() -> None:
    """s2s 没实现 item.truncate。客户端要能接住这个错，而不是假设它成功了。"""
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
    """存一个可以推导的值就会有两处真相。"""
    assert caps_mod.S2S.expr_tags_safe is True
    assert caps_mod.DASHSCOPE.expr_tags_safe is False
    assert caps_mod.OPENAI_GA.expr_tags_safe is False


@pytest.mark.parametrize("caps", [caps_mod.S2S, caps_mod.DASHSCOPE, caps_mod.OPENAI_GA])
def test_every_profile_declares_a_turn_detection_type(caps: caps_mod.Capabilities) -> None:
    assert caps.turn_detection_types, "provider 必须声明它支持哪些判停类型"


def test_s2s_does_not_claim_semantic_vad() -> None:
    """它会收下 semantic_vad 然后忽略。声明支持等于骗自己。"""
    assert "semantic_vad" not in caps_mod.S2S.turn_detection_types
    assert "semantic_vad" in caps_mod.DASHSCOPE.turn_detection_types
