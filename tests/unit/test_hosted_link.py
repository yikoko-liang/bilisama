"""HostedLink's session bootstrap: the frame DashScope needs before audio.

Dev-talk's wire mode carried this session.update by hand (probed live
2026-08-10: without it the beta endpoint never runs server VAD). The adapter
owns it now, so director mode and the eventual production path get it for
free — and the GA dialect, whose VAD is on by default, stays untouched.
"""

from __future__ import annotations

import asyncio

from bilisama.config.enums import ProviderName
from bilisama.config.schema import HostedTurnConfig
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime.providers.hosted import HostedLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script


async def test_dashscope_connect_sends_the_beta_bootstrap() -> None:
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, script=Script()) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            turn=HostedTurnConfig(type="server_vad", threshold=0.4, silence_duration_ms=300),
        )
        await hosted.connect()
        try:
            # The send returns once the frame is on the socket; give the server
            # a few turns to actually read it.
            for _ in range(50):
                if server.recorded.count("session.update"):
                    break
                await asyncio.sleep(0.01)
            frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
            assert frames, "no bootstrap reached the server"
            session = frames[0]["session"]
            assert session["modalities"] == ["text", "audio"], "beta key, not output_modalities"
            assert session["input_audio_format"] == "pcm16", "flat beta format keys"
            assert session["output_audio_format"] == "pcm16"
            assert session["turn_detection"] == {
                "type": "server_vad",
                "threshold": 0.4,
                "silence_duration_ms": 300,
            }
            assert "type" not in session, "beta sessions carry no session.type"
        finally:
            await hosted.aclose()


async def test_a_link_without_turn_config_sends_no_bootstrap() -> None:
    """OpenAI GA runs server_vad by default; an unconfigured link stays quiet."""
    async with MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=Script()) as server:
        hosted = HostedLink(server.url, ProviderName.OPENAI_GA)
        await hosted.connect()
        try:
            assert server.recorded.count("session.update") == 0
        finally:
            await hosted.aclose()


async def test_headers_reach_the_transport() -> None:
    hosted = HostedLink(
        "ws://127.0.0.1:1/unused",
        ProviderName.DASHSCOPE,
        headers={"Authorization": "Bearer x"},
    )
    # Pinned at the client attribute: the mock server ignores headers, and a
    # live assert belongs to the integration tier.
    assert hosted._client._headers == {"Authorization": "Bearer x"}


async def test_s2s_audio_mode_leaves_the_session_unpinned_and_replies_carry_pcm() -> None:
    """Director mode against the official pipeline: text_replies=False must not
    pin the session to text, and explicit replies come back as audio — a
    text-pinned session was the bug that muted an entire s2s run."""
    from bilisama.realtime import link
    from bilisama.realtime.providers.s2s import S2SLink

    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server:
        s2s = S2SLink(server.url, text_replies=False)
        await s2s.connect()
        try:
            await s2s.set_context("人设")
            await s2s.add_context_item("[弹幕] 阿强: 你好")
            await s2s.request_reply(link.ReplySpec(instructions="回一句"))
            audio = 0
            async for event in s2s.events():
                if isinstance(event, link.ReplyAudioDelta):
                    audio += len(event.pcm)
                elif isinstance(event, link.ReplyDone):
                    break
            assert audio > 0, "audio mode must yield PCM from a TTS-owning server"
            updates = [
                e["session"] for e in server.recorded.events if e.get("type") == "session.update"
            ]
            assert updates and all(
                "output_modalities" not in s for s in updates
            ), "the session must not be pinned to text in audio mode"
        finally:
            await s2s.aclose()
