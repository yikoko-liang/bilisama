"""HostedLink's session bootstrap: the frame DashScope needs before audio.

Dev-talk's wire mode carried this session.update by hand (probed live
2026-08-10: without it the beta endpoint never runs server VAD). The adapter
owns it now, so director mode and the eventual production path get it for
free — and the GA dialect, whose VAD is on by default, stays untouched.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName
from bilisama.config.schema import HostedTurnConfig
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.providers.hosted import HostedLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script
from tests.unit.test_realtime_client import _next_event


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


@pytest.mark.parametrize("hosted_provider", [True, False])
async def test_fresh_conversation_drops_old_items_and_replays_only_new_instructions(
    hosted_provider: bool,
) -> None:
    from bilisama.dev_talk import _Fanout
    from bilisama.realtime.providers.s2s import S2SLink

    caps = caps_mod.DASHSCOPE if hosted_provider else caps_mod.S2S
    async with MockRealtimeServer(caps=caps, script=Script()) as server:
        hosted = (
            HostedLink(server.url, ProviderName.DASHSCOPE)
            if hosted_provider
            else S2SLink(server.url)
        )
        fanout = _Fanout(hosted)
        fanout.events()
        fanout.gated_events()
        await hosted.connect()
        fanout.start()
        try:
            await fanout.reset_conversation("上一例背景")
            await hosted.add_context_item("上一例的回答", role="assistant")
            await hosted.add_context_item("上一例的观众弹幕")
            old_socket = hosted._client._ws
            for queue in (*fanout._sinks, *fanout._gated_sinks):
                queue.put_nowait(link.LinkDown("旧连接错误"))
            await fanout.reset_conversation("本例背景")
            assert hosted._client._ws is not old_socket
            assert all(queue.empty() for queue in (*fanout._sinks, *fanout._gated_sinks))
            for _ in range(50):
                frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
                if frames and frames[-1]["session"].get("instructions") == "本例背景":
                    break
                await asyncio.sleep(0.01)
            assert frames[-1]["session"]["instructions"] == "本例背景"
            assert server.recorded.count("conversation.item.create") == 2
            assert not hosted._client._awaiting_created
            assert hosted._client._slot_free.is_set()
        finally:
            await fanout.aclose()


async def test_headers_reach_the_transport() -> None:
    hosted = HostedLink(
        "ws://127.0.0.1:1/unused",
        ProviderName.DASHSCOPE,
        headers={"Authorization": "Bearer x"},
    )
    # Pinned at the client attribute: the mock server ignores headers, and a
    # live assert belongs to the integration tier.
    assert hosted._client._headers == {"Authorization": "Bearer x"}


async def test_reply_instructions_carry_the_session_persona_s2s() -> None:
    """Per the Realtime protocol, response.instructions REPLACE the session's
    for that response — upstream picks either/or (base_openai_compatible_
    language_model.py:709-711), and a live probe (2026-08-14) showed a
    session-level pirate persona vanishing from an instruction-carrying
    reply. The scheduler sends only the per-turn ask and assumes the persona
    stays, so the adapter must recompose: persona first, then the ask."""
    from bilisama.realtime import link
    from bilisama.realtime.providers.s2s import S2SLink

    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server:
        s2s = S2SLink(server.url, text_replies=False)
        await s2s.connect()
        try:
            await s2s.set_context("你是豆腐，主播的AI搭子。")
            await s2s.request_reply(link.ReplySpec(instructions="谢谢阿强的SC，一句话。"))
            for _ in range(50):
                if server.recorded.count("response.create"):
                    break
                await asyncio.sleep(0.01)
            creates = [e for e in server.recorded.events if e.get("type") == "response.create"]
            assert creates, "no response.create reached the server"
            sent = creates[0]["response"]["instructions"]
            assert "你是豆腐" in sent, "the persona must survive a per-turn instruction"
            assert "谢谢阿强的SC" in sent
            assert sent.index("你是豆腐") < sent.index("谢谢阿强的SC"), "persona comes first"
        finally:
            await s2s.aclose()


async def test_reply_without_instructions_stays_bare_s2s() -> None:
    """No per-turn ask -> no instructions key; the server falls back to the
    session's own instructions, which is exactly the persona already."""
    from bilisama.realtime import link
    from bilisama.realtime.providers.s2s import S2SLink

    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server:
        s2s = S2SLink(server.url, text_replies=False)
        await s2s.connect()
        try:
            await s2s.set_context("你是豆腐，主播的AI搭子。")
            await s2s.request_reply(link.ReplySpec())
            for _ in range(50):
                if server.recorded.count("response.create"):
                    break
                await asyncio.sleep(0.01)
            creates = [e for e in server.recorded.events if e.get("type") == "response.create"]
            assert creates
            assert "instructions" not in creates[0]["response"]
        finally:
            await s2s.aclose()


async def test_reply_instructions_carry_the_session_persona_hosted() -> None:
    """Same protocol semantics on the hosted path (DashScope beta dialect)."""
    from bilisama.realtime import link

    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, script=Script()) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE)
        await hosted.connect()
        try:
            await hosted.set_context("你是豆腐，主播的AI搭子。")
            await hosted.request_reply(link.ReplySpec(instructions="谢谢阿强的SC，一句话。"))
            for _ in range(50):
                if server.recorded.count("response.create"):
                    break
                await asyncio.sleep(0.01)
            creates = [e for e in server.recorded.events if e.get("type") == "response.create"]
            assert creates
            sent = creates[0]["response"]["instructions"]
            assert "你是豆腐" in sent
            assert "谢谢阿强的SC" in sent
        finally:
            await hosted.aclose()


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
            text_deltas: list[str] = []
            done_text = ""
            async for event in s2s.events():
                if isinstance(event, link.ReplyAudioDelta):
                    audio += len(event.pcm)
                elif isinstance(event, link.ReplyTextDelta):
                    text_deltas.append(event.text)
                elif isinstance(event, link.ReplyDone):
                    done_text = event.text
                    break
            assert audio > 0, "audio mode must yield PCM from a TTS-owning server"
            # The GA server sends no transcript deltas — the text arrives as one
            # output_audio_transcript.done, and the client must fold it in so a
            # spoken reply never reports empty text (the starved-exemplar bug).
            assert done_text == server.script.reply_text
            assert "".join(text_deltas) == server.script.reply_text
            updates = [
                e["session"] for e in server.recorded.events if e.get("type") == "session.update"
            ]
            assert updates and all(
                "output_modalities" not in s for s in updates
            ), "the session must not be pinned to text in audio mode"
        finally:
            await s2s.aclose()


async def test_the_voice_rides_the_bootstrap() -> None:
    """Asking for a voice is the only way to avoid the server's own pick.

    DashScope defaults to longanqian, measured at 343 Hz against the 180-260 Hz
    of an ordinary adult female voice — high enough that the first person to
    hear it called it shrill. Nothing above this layer can override it, so the
    name has to survive the trip to the wire.
    """
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, script=Script()) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            turn=HostedTurnConfig(),
            voice="longanlingxin",
        )
        await hosted.connect()
        try:
            for _ in range(50):
                if server.recorded.count("session.update"):
                    break
                await asyncio.sleep(0.01)
            frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
            assert frames, "no bootstrap reached the server"
            assert frames[0]["session"]["voice"] == "longanlingxin"
        finally:
            await hosted.aclose()


async def test_rotation_is_armed_from_the_providers_own_cap_by_default() -> None:
    """Debt #19's open half: the rotation code shipped 2026-08-20 and nothing
    could turn it on. dev-talk builds the link without session_cap_min, the
    default was 0, and 0 returns immediately — so DashScope's 120-minute cap
    was still met the passive way, by being cut off mid-sentence.

    The cap is a published property of the provider, so the adapter knows it
    without being told (plan section 3.1: DashScope 120, OpenAI 60).
    """
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE, clock=clock, auto_reconnect=True)
        await hosted.connect()
        try:
            events = hosted.events()
            await clock.advance(116 * 60)
            await asyncio.sleep(0)
            assert hosted._rotation is not None and not hosted._rotation.done()

            await clock.advance(2 * 60)  # crosses 120 minus the 3-minute margin
            down = await _next_event(events, link.LinkDown)
            assert isinstance(down, link.LinkDown)
            assert down.reason == "rotate:session_cap", down.reason
        finally:
            await hosted.aclose()


async def test_rotation_can_still_be_switched_off() -> None:
    """0 keeps meaning "never rotate" — the escape hatch for an endpoint whose
    cap we guessed wrong, and what every test that does not care passes."""
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        hosted = HostedLink(
            server.url, ProviderName.DASHSCOPE, clock=clock, session_cap_min=0, auto_reconnect=True
        )
        await hosted.connect()
        try:
            assert hosted._rotation is None
            await clock.advance(10 * 60 * 60)
            await asyncio.sleep(0)
            assert hosted._rotation is None
        finally:
            await hosted.aclose()


async def test_a_provider_with_no_known_cap_does_not_rotate_on_a_guess() -> None:
    """s2s never reaches this adapter, but a hosted provider we have not
    measured must not be rotated on an invented number."""
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=Script()) as server:
        hosted = HostedLink(server.url, ProviderName.OPENAI_GA, clock=clock)
        await hosted.connect()
        try:
            # OpenAI's cap IS known (60 minutes), so this one does arm — the
            # assertion that matters is that the number came from the table
            # rather than from DashScope's.
            assert hosted._session_cap_s == (60 - 3) * 60
        finally:
            await hosted.aclose()


async def test_a_protected_reply_says_so_when_the_provider_cannot_protect() -> None:
    """Ledger #45: hosted request_reply ignores spec.protected and
    end_protection is a no-op, so a paid SC answer is interruptible on
    DashScope while the scheduler's books say it is protected. The gap is not
    closable from here — disarming the server's own barge-in needs a
    turn_detection field no live probe has confirmed — but it must not be
    invisible, which is what made it survive three months.
    """
    from bilisama.realtime import link as link_mod

    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(f"{record.getMessage()} {getattr(record, 'fields', {})}")

    logger = logging.getLogger("bilisama.realtime.providers.hosted")
    sink = _Sink()
    logger.addHandler(sink)
    try:
        async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
            hosted = HostedLink(server.url, ProviderName.DASHSCOPE, session_cap_min=0)
            await hosted.connect()
            try:
                await hosted.request_reply(link_mod.ReplySpec(protected=True))
                await hosted.request_reply(link_mod.ReplySpec(protected=True))
                await hosted.request_reply(link_mod.ReplySpec())
            finally:
                await hosted.aclose()
    finally:
        logger.removeHandler(sink)

    warned = [line for line in lines if "hosted.protection_unsupported" in line]
    # Once per link, not once per SC: an answer-every-gift stream would drown
    # its own log.
    assert len(warned) == 1, lines
    assert "dashscope" in warned[0]


async def test_ending_protection_on_a_hosted_link_sends_nothing() -> None:
    """The scheduler calls end_protection on every protected reply's done and
    again on the hard cap. With nothing to re-arm, the pair must stay a no-op
    rather than invent a session.update no endpoint was probed for."""
    from bilisama.realtime import link as link_mod

    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE, session_cap_min=0)
        await hosted.connect()
        try:
            await hosted.request_reply(link_mod.ReplySpec(protected=True))
            before = server.recorded.count("session.update")
            await hosted.end_protection()
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert server.recorded.count("session.update") == before
        finally:
            await hosted.aclose()


async def test_the_s2s_protection_window_has_two_visible_edges() -> None:
    """Barge-in is disarmed for a paid reply and re-armed after it.

    Both edges at info, because a session stuck at interrupt_response=false is
    the failure where the streamer opens their mouth and nothing happens, for
    hours, over one lost frame (ledger #46). Without the pair in the log there
    is no way to tell that state from a quiet room.
    """
    from bilisama.realtime import link as link_mod
    from bilisama.realtime.providers.s2s import S2SLink
    from tests.unit.test_realtime_client import _capturing, _named

    with _capturing("bilisama.realtime.providers.s2s") as records:
        async with MockRealtimeServer(caps=caps_mod.S2S, script=Script(delta_chunks=1)) as server:
            s2s = S2SLink(server.url)
            await s2s.connect()
            try:
                await s2s.request_reply(link_mod.ReplySpec(protected=True, protect_ms=6000))
                await _next_event(s2s.events(), link_mod.ReplyDone)
                await s2s.end_protection()
                # The scheduler's belt-and-braces second call, on the hard cap.
                await s2s.end_protection()
            finally:
                await s2s.aclose()

    armed = _named(records, "s2s.protection_armed")
    ended = _named(records, "s2s.protection_ended")
    assert len(armed) == 1, armed
    assert armed[0]["protect_ms"] == 6000
    assert len(ended) == 2, ended
    assert ended[0]["was_armed"] is True, "the first call is the one that closed the window"
    assert ended[1]["was_armed"] is False, "the cap's repeat must not read as a second window"


async def test_the_s2s_replay_line_survives_the_scrubber() -> None:
    """Field NAMES decide what the scrubber folds, and it fails closed.

    `text_replies=True` reached the log as `<bool>`, because `text` is the word
    that means danmaku (obs/logging.py:51) — a line that answers nothing while
    looking like it does. Every field on this line is checked, not just that
    one: the next person adding `reply_text=` here deserves to find out from a
    test rather than from an empty panel.
    """
    from bilisama.obs.logging import _scrub
    from bilisama.realtime.providers.s2s import S2SLink
    from tests.unit.test_realtime_client import _capturing, _named

    with _capturing("bilisama.realtime.providers.s2s") as records:
        async with MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server:
            s2s = S2SLink(server.url, text_replies=True)
            await s2s.connect()
            try:
                await s2s.set_context("你是豆腐。")
            finally:
                await s2s.aclose()

    replays = _named(records, "s2s.session_replayed")
    assert len(replays) == 1, replays
    # The scrub check comes first, so a rename back to `text_replies=` fails
    # here — with the reason — rather than on a missing key further down.
    for key, value in replays[0].items():
        assert (
            _scrub(key, value, log_viewer_content=False) == value
        ), f"{key} is folded by the scrubber, so this line loses it"
    assert replays[0]["modality"] == "text"
    assert replays[0]["rearmed_barge_in"] is False


async def test_a_voice_alone_is_worth_a_bootstrap() -> None:
    """GA sends no turn config, but a named voice still has to get through.

    The frame used to exist only to carry turn detection, so it returned early
    when there was none — which would have dropped the voice on exactly the
    dialect that needs no other setup.
    """
    async with MockRealtimeServer(caps=caps_mod.OPENAI_GA, script=Script()) as server:
        hosted = HostedLink(server.url, ProviderName.OPENAI_GA, voice="marin")
        await hosted.connect()
        try:
            for _ in range(50):
                if server.recorded.count("session.update"):
                    break
                await asyncio.sleep(0.01)
            frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
            assert frames, "a voice-only bootstrap never left"
            session = frames[0]["session"]
            assert session["voice"] == "marin"
            assert session["type"] == "realtime", "GA rejects a session without it"
            assert "turn_detection" not in session, "we never configured one"
        finally:
            await hosted.aclose()


async def test_an_assistant_history_item_uses_the_output_content_type() -> None:
    """Realtime items carry role-matched content types: user text is
    `input_text`, assistant text is `output_text`. Writing a reply back into
    history with the user's type is a protocol error a strict endpoint
    rejects — and a lenient one records the assistant's own words as a
    viewer's, which is worse. Both adapters share the shape, so both are
    pinned here."""
    from bilisama.realtime.providers.s2s import S2SLink

    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE, session_cap_min=0)
        await hosted.connect()
        try:
            await hosted.add_context_item("[弹幕] 阿强: 你好")
            await hosted.add_context_item("欢迎阿强！", role="assistant")
            for _ in range(50):
                if server.recorded.count("conversation.item.create") >= 2:
                    break
                await asyncio.sleep(0.01)
            items = [
                e["item"]
                for e in server.recorded.events
                if e.get("type") == "conversation.item.create"
            ]
            assert [i["role"] for i in items] == ["user", "assistant"]
            assert items[0]["content"][0]["type"] == "input_text"
            assert items[1]["content"][0]["type"] == "output_text"
        finally:
            await hosted.aclose()

    async with MockRealtimeServer(caps=caps_mod.S2S, script=Script()) as server:
        s2s = S2SLink(server.url)
        await s2s.connect()
        try:
            await s2s.add_context_item("说得不错，下一题。", role="assistant")
            for _ in range(50):
                if server.recorded.count("conversation.item.create"):
                    break
                await asyncio.sleep(0.01)
            items = [
                e["item"]
                for e in server.recorded.events
                if e.get("type") == "conversation.item.create"
            ]
            assert items and items[0]["content"][0]["type"] == "output_text"
        finally:
            await s2s.aclose()


async def test_a_chunk_too_short_to_convert_is_not_sent_as_an_empty_frame() -> None:
    """`Resampler.feed` returns b"" when a chunk cannot produce one output
    sample at this ratio. Forwarding that base64s an empty buffer into an
    append with `"audio": ""`, which the GA endpoint rejects — a rate
    conversion that turns a valid frame into a protocol error is backwards."""
    sent: list[bytes] = []
    hosted = HostedLink("wss://example.invalid/x", ProviderName.OPENAI_GA)
    hosted._client.push_audio = lambda pcm: _record(sent, pcm)  # type: ignore[method-assign]

    await hosted.push_audio(b"\x01\x02")  # one sample: 16k -> 24k emits nothing yet
    assert sent == []

    await hosted.push_audio(b"\x01\x02" * 320)
    assert sent and sent[0]


async def _record(sink: list[bytes], pcm: bytes) -> None:
    sink.append(pcm)


async def test_a_reply_scoped_base_replaces_the_session_persona_for_that_turn() -> None:
    """Event turns carry event rules without touching the session: the base
    rides the response.create, composed under the per-turn ask, and the next
    reply without one falls back to the session's own context."""
    from bilisama.realtime.providers.s2s import S2SLink

    for make in (
        lambda url: HostedLink(url, ProviderName.DASHSCOPE, session_cap_min=0),
        lambda url: S2SLink(url, text_replies=False),
    ):
        async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
            adapter = make(server.url)
            await adapter.connect()
            try:
                await adapter.set_context("公共上下文＋主播语音规则")
                await adapter.request_reply(
                    link.ReplySpec(base_instructions="公共上下文＋事件规则", instructions="谢谢SC")
                )
                await adapter.request_reply(link.ReplySpec(instructions="随口一句"))
                for _ in range(50):
                    if server.recorded.count("response.create") >= 2:
                        break
                    await asyncio.sleep(0.01)
                creates = [
                    e["response"]
                    for e in server.recorded.events
                    if e.get("type") == "response.create"
                ]
                assert len(creates) == 2
                assert creates[0]["instructions"].startswith("公共上下文＋事件规则")
                assert "谢谢SC" in creates[0]["instructions"]
                assert "主播语音规则" not in creates[0]["instructions"]
                assert creates[1]["instructions"].startswith("公共上下文＋主播语音规则")
            finally:
                await adapter.aclose()


async def test_reconfigure_session_rides_the_new_voice_on_the_next_bootstrap() -> None:
    """The panel's voice change, end to end at this layer: reconfigure rotates
    the socket, and the replayed bootstrap carries the new name — this method
    shipped with zero tests, which is how a silent no-op would have looked
    exactly like success."""
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, script=Script()) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            turn=HostedTurnConfig(),
            voice="longanlingxin",
            session_cap_min=0,
        )
        await hosted.connect()
        try:
            for _ in range(50):
                if server.recorded.count("session.update"):
                    break
                await asyncio.sleep(0.01)
            await hosted.reconfigure_session(voice="longanlufeng")
            for _ in range(100):
                if server.recorded.count("session.update") >= 2:
                    break
                await asyncio.sleep(0.01)
            frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
            assert len(frames) >= 2, "the rotate never produced a second bootstrap"
            assert frames[-1]["session"]["voice"] == "longanlufeng"
        finally:
            await hosted.aclose()


async def test_reconfigure_while_suspended_stores_the_voice_without_reconnecting() -> None:
    """The pause gate closed the socket on purpose; a settings change must not
    wake it through rotate()'s reconnect ladder. The value waits, and resume's
    own bootstrap carries it."""
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, script=Script()) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            turn=HostedTurnConfig(),
            voice="longanlingxin",
            session_cap_min=0,
        )
        await hosted.connect()
        try:
            for _ in range(50):
                if server.recorded.count("session.update"):
                    break
                await asyncio.sleep(0.01)
            await hosted.suspend()
            before = server.recorded.count("session.update")
            await hosted.reconfigure_session(voice="longanlufeng")
            await asyncio.sleep(0.1)
            assert server.recorded.count("session.update") == before, "suspended 中不该重连"
            assert hosted._voice == "longanlufeng", "值要先落下，resume 时生效"
            await hosted.resume()
            for _ in range(100):
                if server.recorded.count("session.update") > before:
                    break
                await asyncio.sleep(0.01)
            frames = [e for e in server.recorded.events if e.get("type") == "session.update"]
            assert frames[-1]["session"]["voice"] == "longanlufeng"
        finally:
            await hosted.aclose()
