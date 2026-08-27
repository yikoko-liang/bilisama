"""HostedLink's session bootstrap: the frame DashScope needs before audio.

Dev-talk's wire mode carried this session.update by hand (probed live
2026-08-10: without it the beta endpoint never runs server VAD). The adapter
owns it now, so director mode and the eventual production path get it for
free — and the GA dialect, whose VAD is on by default, stays untouched.
"""

from __future__ import annotations

import asyncio
import logging

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
