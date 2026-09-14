"""VolcanoLink against a fake that refuses things.

The semantic gaps in the adapter's module docstring are what these tests are
mostly about — no response.create, no per-turn instructions slot, and a reply
that ends when the AUDIENCE stops hearing rather than when the model stops
generating. Each one is a place where doing the obvious thing produces a link
that looks fine and behaves wrong.

「No cancel」 used to be on that list and is not: ClientInterrupt exists and
works, probed live 2026-08-27. A test asserting the old reading survived the
correction for a while, comparing two snapshots both taken before the frame
had landed — an assertion that would have passed whatever cancel sent.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets

from bilisama.clock import FakeClock
from bilisama.config.schema import VolcanoConfig
from bilisama.realtime import link
from bilisama.realtime.client import SessionRefused
from bilisama.realtime.providers import volcano_wire as wire
from bilisama.realtime.providers.volcano import VolcanoLink
from tests.fakes.mock_volcano import MockVolcanoServer


def _cfg(**over: Any) -> VolcanoConfig:
    return VolcanoConfig(**{"speaker": "zh_female_test", **over})


async def _collect(source: AsyncIterator[link.LinkEvent], count: int) -> list[link.LinkEvent]:
    """Take `count` events, or fail loudly rather than hanging the suite."""
    got: list[link.LinkEvent] = []
    for _ in range(count):
        got.append(await asyncio.wait_for(anext(source), timeout=2.0))
    return got


async def _until(cond: Any, *, timeout: float = 2.0) -> None:
    """Poll until `cond()` holds, or fail saying it never did.

    For states the adapter owns. Waiting on a frame the FAKE has recorded is a
    different clock, and tests that conflate the two report races that are not
    there — or miss ones that are.
    """
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("等不到那个状态成立")


async def _linked(
    server: MockVolcanoServer, **kw: Any
) -> tuple[VolcanoLink, AsyncIterator[link.LinkEvent]]:
    config = kw.pop("config", None) or _cfg()
    volcano = VolcanoLink(server.url, app_id="app", access_key="key", config=config, **kw)
    await volcano.connect()
    await server.wait_ready()
    # No LinkUp to drain: it means 「the link came BACK」 and a first connect is
    # not a recovery. This helper used to swallow one, which is how the extra
    # startup event went unnoticed while dev-talk printed 「已恢复」 every run.
    return volcano, volcano.events()


# ------------------------------------------------------------------ handshake


async def _raw_frame(
    kind: wire.MessageKind, event: int, *, session_id: str, json_bit: bool
) -> bytes:
    """Build a client frame by hand, so a planted violation does not have to go
    through the encoder that is supposed to prevent it."""
    header = bytes((0x11, (kind << 4) | 0b0100, 0b0001_0000 if json_bit else 0, 0))
    parts = [header, struct.pack(">i", event)]
    if event >= 100:
        # The length field is always there for a session-scoped event; a client
        # that "forgot the field exists" writes zero into it, which is the shape
        # the fake's header describes. Omitting it entirely is a different bug
        # and one the decoder catches on its own.
        parts.append(struct.pack(">I", len(session_id)))
        parts.append(session_id.encode())
    body = b"{}"
    parts.append(struct.pack(">I", len(body)))
    parts.append(body)
    return b"".join(parts)


@pytest.mark.parametrize(
    ("rule", "frames"),
    [
        pytest.param(
            "没带 session id",
            [(wire.MessageKind.FULL_CLIENT, int(wire.ClientEvent.START_SESSION), "", False)],
            id="会话级事件漏了 id",
        ),
        pytest.param(
            "还没建连接就开会话",
            [(wire.MessageKind.FULL_CLIENT, int(wire.ClientEvent.START_SESSION), "s1", False)],
            id="跳过握手第一级",
        ),
        pytest.param(
            "上行音频不是 Raw",
            [(wire.MessageKind.AUDIO_CLIENT, int(wire.ClientEvent.TASK_REQUEST), "s1", True)],
            id="音频按 JSON 发",
        ),
    ],
)
async def test_the_fake_refuses_what_its_header_says_it_refuses(
    rule: str, frames: list[tuple[wire.MessageKind, int, str, bool]]
) -> None:
    """These rules are asserts inside a websockets handler, and websockets
    catches whatever a handler raises — it logs and closes with 1011. So for a
    while all but the first were decoration: a violation showed up as a stray
    reconnect (VolcanoLink retries by default) and the test went green. This
    plants one and checks the fake now names it.

    Same reasoning as tests/unit/test_ui_meta.py's planted violations: a gate
    whose teeth are never exercised is one nobody can trust.
    """
    async with MockVolcanoServer() as server:
        async with websockets.connect(server.url) as ws:
            for kind, event, session_id, json_bit in frames:
                await ws.send(
                    await _raw_frame(kind, event, session_id=session_id, json_bit=json_bit)
                )
            # Give the handler a turn; the violation is recorded, not answered.
            for _ in range(100):
                if server.violations:
                    break
                await asyncio.sleep(0.005)

        assert any(rule in complaint for complaint in server.violations), server.violations
        # Cleared so __aexit__ does not re-raise what we just asserted.
        server.violations.clear()


async def test_the_fake_leaves_a_legal_exchange_alone() -> None:
    """A gate that fires on correct traffic is one someone switches off."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            await volcano.push_audio(b"\x00\x01" * 320)
            await server.wait_for(wire.ClientEvent.TASK_REQUEST)
        finally:
            await volcano.aclose()
        assert server.violations == []


async def test_the_handshake_climbs_both_levels_in_order() -> None:
    """Connection first, then session. The fake asserts the order because a
    client that fires both without waiting passes on loopback and fails on a
    real network."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            assert server.recorded.events[:2] == [
                int(wire.ClientEvent.START_CONNECTION),
                int(wire.ClientEvent.START_SESSION),
            ]
        finally:
            await volcano.aclose()


async def test_the_credentials_travel_as_headers_not_in_the_url() -> None:
    """A key in a query string ends up in every proxy log there is."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            headers = server.recorded.headers
            assert headers["x-api-app-id"] == "app"
            assert headers["x-api-access-key"] == "key"
            assert headers["x-api-resource-id"] == "volc.speech.dialog"
            assert "key" not in server.url
        finally:
            await volcano.aclose()


async def test_the_downlink_is_asked_for_as_pcm() -> None:
    """The vendor's default is Ogg Opus, and every consumer above us wants raw
    PCM. Not asking is how you get audio nothing can play."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body["tts"]["audio_config"] == {
                "channel": 1,
                "format": "pcm_s16le",
                "sample_rate": 24000,
            }
        finally:
            await volcano.aclose()


@pytest.mark.parametrize(
    ("model", "key"),
    [("1.2.1.1", "system_role"), ("2.2.0.0", "character_manifest")],
)
async def test_the_persona_goes_under_the_key_its_model_generation_reads(
    model: str, key: str
) -> None:
    """The two are not aliases. Sending one under the other's key is accepted
    and ignored — she connects, sounds fine, and has no persona at all."""
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(server.url, app_id="a", access_key="k", config=_cfg(model=model))
        await volcano.set_context("你是豆腐，主播的AI搭子。")
        await volcano.connect()
        await server.wait_ready()
        try:
            dialog = server.recorded.body_for(wire.ClientEvent.START_SESSION)["dialog"]
            assert dialog[key] == "你是豆腐，主播的AI搭子。"
            other = {"system_role", "character_manifest"} - {key}
            assert not (other & set(dialog)), f"人设同时写进了两个键：{sorted(dialog)}"
            # The version rides along, because the key alone does not say which
            # generation is meant to read it — and the vendor lists it required.
            assert dialog["extra"]["model"] == model
        finally:
            await volcano.aclose()


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [({"refuse_connection": True}, "连接"), ({"refuse_session": True}, "会话")],
)
async def test_a_refused_handshake_says_which_step_failed(
    kwargs: dict[str, bool], why: str
) -> None:
    """Two steps, two failure modes. Reporting either as the other sends
    people to check credentials when the session config was the problem."""
    async with MockVolcanoServer(**kwargs) as server:
        volcano = VolcanoLink(server.url, app_id="a", access_key="k", config=_cfg())
        with pytest.raises(SessionRefused) as caught:
            await volcano.connect()
        assert why in str(caught.value)
        assert "session.created" not in str(caught.value), "报的是别家协议的事件名"


# ---------------------------------------------------------------- a full turn


async def test_a_reply_arrives_as_text_then_audio_then_done() -> None:
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await volcano.add_context_item("[SC ¥30] 阿强: 主播今天玩什么")
            await volcano.request_reply(link.ReplySpec(instructions="谢谢阿强，一句话。"))
            await server.say("玩这个", audio=b"\x01\x02" * 240)

            got = await _collect(events, 5)
            assert isinstance(got[0], link.ReplyTextDelta)
            assert not got[0].handle.implicit, "we asked for this one"
            audio = [e for e in got if isinstance(e, link.ReplyAudioDelta)]
            assert audio and audio[0].pcm == b"\x01\x02" * 240
            done = got[-1]
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
            assert done.text == "玩这个"
        finally:
            await volcano.aclose()


async def test_the_item_and_the_ask_collapse_into_one_query() -> None:
    """No response.create here: the two-step inject has to become one
    ChatTextQuery, carrying both halves. Dropping either one sends her either
    the data with no instruction or the instruction with no data."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            await volcano.add_context_item("[SC ¥30] 阿强: 主播今天玩什么")
            await volcano.request_reply(link.ReplySpec(instructions="谢谢阿强，一句话。"))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            body = server.recorded.body_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            assert "阿强" in body["content"]
            assert "一句话" in body["content"]
            assert server.recorded.count(wire.ClientEvent.CHAT_TEXT_QUERY) == 1
        finally:
            await volcano.aclose()


async def test_the_persona_is_not_resent_with_every_query() -> None:
    """compose_instructions exists for a quirk this protocol does not have:
    there is no per-response instructions field to replace the session's, so
    the persona sits in the session config undisturbed. Re-sending it every
    turn would be waste dressed up as safety."""
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(server.url, app_id="a", access_key="k", config=_cfg())
        await volcano.set_context("你是豆腐，说话短促爱接梗。")
        await volcano.connect()
        await server.wait_ready()
        try:
            await volcano.request_reply(link.ReplySpec(instructions="说句话"))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            content = server.recorded.body_for(wire.ClientEvent.CHAT_TEXT_QUERY)["content"]
            assert "豆腐" not in content, "人设跟着每一轮又发了一遍"
            assert content == "说句话"
        finally:
            await volcano.aclose()


async def test_a_stashed_item_is_spent_once() -> None:
    """It travels with the next query and not the one after. A second reply
    inheriting the first one's SC would thank 阿强 twice."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await volcano.add_context_item("[SC ¥30] 阿强: 主播今天玩什么")
            await volcano.request_reply(link.ReplySpec(instructions="第一句"))
            await server.say("好的")
            await _collect(events, 3)

            await volcano.request_reply(link.ReplySpec(instructions="第二句"))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY, count=2)
            content = server.recorded.body_for(wire.ClientEvent.CHAT_TEXT_QUERY)["content"]
            assert "阿强" not in content
            assert content == "第二句"
        finally:
            await volcano.aclose()


async def test_uplink_audio_goes_out_as_raw_frames() -> None:
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            frame = b"\x00\x01" * 320  # 20 ms at 16 kHz mono s16le
            await volcano.push_audio(frame)
            await server.wait_for(wire.ClientEvent.TASK_REQUEST)
            assert server.recorded.audio == [frame]
        finally:
            await volcano.aclose()


# --------------------------------------------------------------- interruption


async def test_asr_info_is_the_barge_in_signal() -> None:
    """There is no speech_started on this protocol. A client waiting for one
    never reports the streamer talking, and the floor gate never closes."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await volcano.request_reply(link.ReplySpec(instructions="讲个长故事"))
            await server.barge_in("等一下")

            got = await _collect(events, 4)
            assert isinstance(got[0], link.SpeechStarted)
            assert isinstance(got[-1], link.SpeechStopped)
        finally:
            await volcano.aclose()


async def test_a_barge_in_reports_it_and_leaves_the_verdict_to_l3() -> None:
    """ASRInfo says the streamer opened their mouth. Whether that ends the
    reply is a product decision, and L3 is the only layer holding the facts:
    director/scheduler.py's _barge_in deliberately does NOT cancel while a paid
    thank-you is inside its protected window.

    Settling here took that decision away. The log then read 「扛住了打断」 for
    a reply this adapter had killed two lines earlier — and worse, _settle had
    already cleared _active, so the cancel L3 does send hit cancel()'s own
    guard and never reached the wire. The model kept generating and the tokens
    kept being spent, which is the exact thing ClientInterrupt was wired up for.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec(protected=True))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            await server.confirm_query()
            await server.barge_in()
            got = await _collect(events, 4)

            assert isinstance(got[0], link.SpeechStarted)
            assert not any(isinstance(e, link.ReplyDone) for e in got), "adapter 替调度器拍了板"
            assert not handle.stale

            # And the decision L3 does make still reaches the far end.
            await volcano.cancel(handle)
            await server.wait_for(wire.ClientEvent.CLIENT_INTERRUPT)
            assert server.interrupted == 1
            assert handle.stale
        finally:
            await volcano.aclose()


async def test_a_cancelled_reply_goes_stale_so_late_audio_is_dropped() -> None:
    """What makes a barge-in sound clean is the handle going stale — both audio
    consumers drop anything carrying it. The interrupt stops the far end; this
    is what covers the frames already in flight."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.barge_in()
            await _collect(events, 4)
            await volcano.cancel(handle)
            await _collect(events, 1)
            assert handle.stale
        finally:
            await volcano.aclose()


async def test_the_transcript_separates_interim_from_final() -> None:
    """The memory layer keys on the difference — a partial written as final is
    a half sentence she thinks the streamer said."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await server.barge_in("今天天气怎么样")
            got = await _collect(events, 4)
            assert isinstance(got[1], link.UserTranscriptDelta)
            assert isinstance(got[2], link.UserTranscriptDone)
            assert got[2].text == "今天天气怎么样"
        finally:
            await volcano.aclose()


# ------------------------------------------------------------- the slot, kept


async def test_the_reply_ends_when_the_audience_stops_hearing() -> None:
    """ChatEnded means the model stopped generating; TTSEnded means she
    stopped talking. Settling on the first frees the slot mid-sentence, and
    the next reply starts over the top of this one."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await volcano.request_reply(link.ReplySpec())
            await server._emit(wire.ServerEvent.CHAT_RESPONSE, {"content": "在的"})
            await server._emit(wire.ServerEvent.CHAT_ENDED, {})
            await _collect(events, 1)  # the text delta

            async def next_event() -> link.LinkEvent:
                return await anext(events)

            settled = asyncio.create_task(next_event())
            await asyncio.sleep(0.05)
            assert not settled.done(), "ChatEnded 就把名额放了，她还在说话"

            await server._emit(wire.ServerEvent.TTS_ENDED, {})
            done = await asyncio.wait_for(settled, timeout=2.0)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
        finally:
            await volcano.aclose()


async def test_a_reply_nobody_ever_ends_is_taken_back_by_the_watchdog() -> None:
    """A lost TTSEnded holds the single slot forever, and every later
    request_reply waits on a slot that is not coming back."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, watchdog_s=25.0)
        try:
            await volcano.request_reply(link.ReplySpec())
            await clock.advance(26.0)

            done = await _collect(events, 1)
            assert isinstance(done[0], link.ReplyDone)
            assert done[0].status is link.ReplyStatus.TIMED_OUT

            # And the slot really came back: this would hang otherwise.
            await asyncio.wait_for(volcano.request_reply(link.ReplySpec()), timeout=2.0)
        finally:
            await volcano.aclose()


async def test_a_reply_the_model_started_on_its_own_still_gets_a_handle() -> None:
    """After VAD the model answers unasked. Without a handle that reply cannot
    be cancelled, and the barge-in that follows has nothing to mark stale."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await server.say("我自己开的口")
            got = await _collect(events, 8)
            assert isinstance(got[0], link.ReplyStarted)
            assert got[0].handle.implicit, "minted for a turn nobody here requested"
            assert isinstance(got[-1], link.ReplyDone)
            assert got[-1].text == "我自己开的口"
        finally:
            await volcano.aclose()


# --------------------------------------------------------------- rough edges


async def test_one_unreadable_frame_does_not_end_the_session() -> None:
    """A dropped video frame is survivable; so is this. Tearing the session
    down would turn one bad packet into a dead stream."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await server.send_garbage()
            await server.say("还在呢")
            got = await _collect(events, 4)
            assert any(isinstance(e, link.ReplyTextDelta) for e in got)
        finally:
            await volcano.aclose()


async def test_an_unknown_event_is_ignored_rather_than_fatal() -> None:
    """The fake sends UsageResponse unasked, exactly as the real one does.
    Treating an unnamed event as an error takes the session down on arrival."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            assert server.recorded.events  # the handshake got through it
            await server.say("没事")
            got = await _collect(events, 4)
            assert any(isinstance(e, link.ReplyTextDelta) for e in got)
        finally:
            await volcano.aclose()


async def test_a_server_error_becomes_a_link_error_and_frees_the_slot() -> None:
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await volcano.request_reply(link.ReplySpec())
            await server.fail(code=45000001, message="并发超限")

            got = await _collect(events, 2)
            errors = [e for e in got if isinstance(e, link.LinkError)]
            assert errors and errors[0].code == "45000001"
            assert any(
                isinstance(e, link.ReplyDone) and e.status is link.ReplyStatus.FAILED for e in got
            )
        finally:
            await volcano.aclose()


async def test_context_pushed_after_the_session_starts_goes_as_update_config() -> None:
    """Before the session it rides inside StartSession. Afterwards it needs its
    own event, and dropping it there means a persona change that silently never
    happens."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            await volcano.set_context("你现在是海盗。")
            await server.wait_for(wire.ClientEvent.UPDATE_CONFIG)
            body = server.recorded.body_for(wire.ClientEvent.UPDATE_CONFIG)
            assert body["dialog"]["system_role"] == "你现在是海盗。"
        finally:
            await volcano.aclose()


async def test_closing_says_goodbye_at_both_levels() -> None:
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        await volcano.aclose()
        await server.wait_for(wire.ClientEvent.FINISH_CONNECTION)
        assert server.recorded.count(wire.ClientEvent.FINISH_SESSION) == 1
        assert server.recorded.count(wire.ClientEvent.FINISH_CONNECTION) == 1


# ------------------------------------------- what the docs had that we missed


async def test_cancel_reaches_the_wire_not_just_our_side() -> None:
    """ClientInterrupt exists, and cancel used to be local-only because the
    first read of the docs missed it. The difference is not cosmetic: without
    the frame the model keeps generating and the tokens are still spent, so a
    barge-in that only sounds clean still costs a full reply."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            talking = asyncio.create_task(server.say_slowly())
            await asyncio.sleep(0.05)
            await volcano.cancel(handle)
            await server.wait_for(wire.ClientEvent.CLIENT_INTERRUPT)

            assert server.interrupted == 1
            assert handle.stale, "线上停了，本地也得停：迟到的帧归消费者丢"
            await asyncio.wait_for(talking, timeout=2.0)
        finally:
            await volcano.aclose()


async def test_a_dropped_socket_comes_back_resuming_the_same_conversation() -> None:
    """Reconnect used to be absent here while the hosted adapter had it, and
    the fix is the protocol's own: StartSession takes the dialog_id the server
    handed out, and the server keeps the last twenty rounds against it. So she
    comes back knowing what was said rather than with amnesia mid-stream."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            await server.drop()
            down = await _collect(events, 1)
            assert isinstance(down[0], link.LinkDown)
            assert down[0].retrying, "掉线了却说不再试，上层就不会等它回来"

            await clock.advance(2.0)
            up = await _collect(events, 1)
            assert isinstance(up[0], link.LinkUp)
            assert server.resumed_with == [
                server.dialog_id
            ], f"重连没带 dialog_id，她会忘掉这一场：{server.resumed_with}"
        finally:
            await volcano.aclose()


async def test_a_reply_in_flight_when_the_socket_dies_does_not_hold_the_slot() -> None:
    """The single slot is held by whoever is speaking. Losing the socket while
    she speaks must free it, or every request_reply after the reconnect waits
    on a reply that died with the old connection."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            await volcano.request_reply(link.ReplySpec())
            await server.drop()
            got = await _collect(events, 2)
            done = [e for e in got if isinstance(e, link.ReplyDone)]
            assert done and done[0].status is link.ReplyStatus.FAILED

            await clock.advance(2.0)
            await _collect(events, 1)  # LinkUp
            await asyncio.wait_for(volcano.request_reply(link.ReplySpec()), timeout=2.0)
        finally:
            await volcano.aclose()


async def test_reconnect_gives_up_out_loud_rather_than_retrying_forever() -> None:
    """A revoked key would otherwise become an infinite quiet retry, and the
    panel would say 「connecting」 for the rest of the stream."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            server.refuse_connection = True
            await server.drop()
            await _collect(events, 1)  # the first LinkDown, retrying=True
            for _ in range(8):
                await clock.advance(40.0)
            final = await _collect(events, 1)
            assert isinstance(final[0], link.LinkDown)
            assert not final[0].retrying, "试满了还说在重试"
        finally:
            await volcano.aclose()


async def test_the_tail_of_a_finished_reply_does_not_become_a_ghost() -> None:
    """A watchdog timeout does not reach the far end, so she keeps talking.

    Every content frame walked into _ensure_active, which minted a fresh
    handle for anything arriving with no reply active — a phantom reply that
    emitted ReplyStarted, took the single slot back, and could only be ended
    by another 25-second timeout. The question_id is what tells the tail of an
    old reply from the start of a new one.
    """
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, watchdog_s=25.0)
        try:
            await volcano.request_reply(link.ReplySpec())
            await server.confirm_query("q1")
            # Collect a delta from the same question before touching the clock.
            # It arrives after the ack on the same socket, so seeing it proves
            # the ack was processed — waiting on a sleep instead would make
            # this test pass or fail on scheduling luck.
            await server._emit(
                wire.ServerEvent.CHAT_RESPONSE, {"content": "开", "question_id": "q1"}
            )
            await _collect(events, 1)

            await clock.advance(26.0)
            timed_out = await _collect(events, 1)
            assert isinstance(timed_out[0], link.ReplyDone)
            assert timed_out[0].status is link.ReplyStatus.TIMED_OUT

            await server.late_tail("q1")
            # Whatever arrives next must not be a reply we never asked for.
            leaked = asyncio.create_task(_collect(events, 1))
            await asyncio.sleep(0.1)
            assert not leaked.done(), f"迟到的尾巴变成了新回复：{leaked.result()}"
            leaked.cancel()

            # And the slot really is free, not held by a phantom.
            await asyncio.wait_for(volcano.request_reply(link.ReplySpec()), timeout=2.0)
        finally:
            await volcano.aclose()


async def test_a_genuinely_new_reply_after_a_timeout_still_gets_through() -> None:
    """The other half: tombstoning must not swallow the next real reply. A
    filter that drops everything is not a filter."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, watchdog_s=25.0)
        try:
            await volcano.request_reply(link.ReplySpec())
            await server.confirm_query("q1")
            await server._emit(
                wire.ServerEvent.CHAT_RESPONSE, {"content": "开", "question_id": "q1"}
            )
            await _collect(events, 1)
            await clock.advance(26.0)
            await _collect(events, 1)  # the timeout

            server.question_id = "q2"
            await server.say("这是新的一轮")
            got = await _collect(events, 3)
            assert any(isinstance(e, link.ReplyTextDelta) for e in got)
        finally:
            await volcano.aclose()


# ---------------------------------------------- what the name actually needs


async def test_the_name_travels_as_its_own_field_on_the_o_generation() -> None:
    """The persona says 「我叫豆腐」 in its first line and that is not enough.

    dialog.bot_name defaults to 豆包, and against our real 557-character
    persona it wins: probed 2026-08-28, three answers out of three were
    「豆包」 without this field and 「豆腐」 with it. A 31-character persona
    saying the same thing DID win on its own, which is exactly why the gap
    survived early testing.
    """
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(
            server.url, api_key="k", config=_cfg(model="1.2.1.1"), bot_name="豆腐"
        )
        await volcano.set_context("我叫豆腐，是这个直播间的 AI 伴播。")
        await volcano.connect()
        await server.wait_ready()
        try:
            dialog = server.recorded.body_for(wire.ClientEvent.START_SESSION)["dialog"]
            assert dialog["bot_name"] == "豆腐"
            assert dialog["system_role"], "名字有了，人设别丢了"
        finally:
            await volcano.aclose()


async def test_the_sc_generation_takes_its_name_from_the_manifest_instead() -> None:
    """bot_name is documented as 「只针对O版本生效」 and SC reads the name out
    of its character manifest. Sending it there would put a field in the frame
    that does nothing — and a field that does nothing is one the next reader
    assumes does something."""
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(
            server.url, api_key="k", config=_cfg(model="2.2.0.0"), bot_name="豆腐"
        )
        await volcano.set_context("你叫豆腐，直播间的 AI 伴播。")
        await volcano.connect()
        await server.wait_ready()
        try:
            dialog = server.recorded.body_for(wire.ClientEvent.START_SESSION)["dialog"]
            assert "bot_name" not in dialog
            assert dialog["character_manifest"]
        finally:
            await volcano.aclose()


async def test_an_error_frame_with_no_event_number_is_not_swallowed() -> None:
    """How a wrong voice presented before this: session started, query acked,
    then nothing at all — while the server had been saying
    「ClientError:InvalidSpeaker」 the whole time. Connection-level errors
    carry no event number, and a dispatch keyed purely off `event` dropped
    them into the ignore branch."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.fail_hard(55000001, "sami error: ClientError:InvalidSpeaker")

            got = await _collect(events, 2)
            errors = [e for e in got if isinstance(e, link.LinkError)]
            assert errors, f"错误帧被吞了，收到的是 {[type(e).__name__ for e in got]}"
            assert errors[0].code == "55000001"
            assert "InvalidSpeaker" in errors[0].detail
            # And it must not leave the slot held by a reply that will never end.
            assert any(
                isinstance(e, link.ReplyDone) and e.status is link.ReplyStatus.FAILED for e in got
            )
            assert handle.stale
        finally:
            await volcano.aclose()


# ------------------------------- the two generations push context differently


async def test_the_o_generation_updates_its_persona_in_place() -> None:
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, config=_cfg(model="1.2.1.1"))
        try:
            await volcano.set_context("你现在是海盗。")
            await server.wait_for(wire.ClientEvent.UPDATE_CONFIG)
            body = server.recorded.body_for(wire.ClientEvent.UPDATE_CONFIG)
            assert body["dialog"]["system_role"] == "你现在是海盗。"
            assert server.recorded.count(wire.ClientEvent.START_SESSION) == 1
        finally:
            await volcano.aclose()


async def test_the_sc_generation_takes_a_new_session_instead() -> None:
    """SC ignores a manifest sent in an UpdateConfig and answers ConfigUpdated
    anyway, so a push that looked delivered was not. Probed 2026-08-28: the
    manifest applies at StartSession and nowhere else, which makes a new
    session the only channel a mid-stream context change has. It costs 114 ms
    on the same socket and keeps the conversation, because the dialog_id goes
    with it."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, config=_cfg(model="2.2.0.0"))
        try:
            await volcano.set_context("你现在是海盗。")
            await server.wait_for(wire.ClientEvent.START_SESSION, count=2)

            assert server.recorded.count(wire.ClientEvent.FINISH_SESSION) == 1
            assert (
                server.recorded.count(wire.ClientEvent.UPDATE_CONFIG) == 0
            ), "SC 上 UpdateConfig 会被静默忽略，发它等于什么都没做"
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body["dialog"]["character_manifest"] == "你现在是海盗。"
            assert body["dialog"]["dialog_id"] == server.dialog_id, "换会话丢了对话"
        finally:
            await volcano.aclose()


async def test_a_context_change_waits_until_she_stops_talking() -> None:
    """Swapping mid-reply would cut her off for a change nobody was waiting on.
    The slot frees between replies, so the swap rides on that."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, config=_cfg(model="2.2.0.0"))
        try:
            await volcano.request_reply(link.ReplySpec())
            await volcano.set_context("你现在是海盗。")
            await asyncio.sleep(0.05)
            assert server.recorded.count(wire.ClientEvent.FINISH_SESSION) == 0, "说到一半就换会话了"

            await server.say("说完啦")
            await _collect(events, 4)
            await server.wait_for(wire.ClientEvent.START_SESSION, count=2)
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body["dialog"]["character_manifest"] == "你现在是海盗。"
        finally:
            await volcano.aclose()


# ------------------------------------------------- audio attribution and slots


async def test_a_stale_sentence_start_does_not_silence_the_live_reply() -> None:
    """The mute decision belongs to the sentence it was made on, not to the link.

    TTSResponse carries no id, so it inherits the judgement made on the
    TTSSentenceStart before it. That judgement used to be stored in one
    link-wide flag that stayed set until the next settle — so a single late
    frame from a reply we had already closed muted whatever was speaking NOW,
    for its whole duration. The audience saw her subtitles move and heard
    nothing at all.

    What is still lost, honestly: the live reply's audio between the stale
    sentence-start and its own next one. That is one sentence, bounded, and
    there is no id on the audio to do better with.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            await server.confirm_query("q1")
            await volcano.cancel(handle)
            await _collect(events, 1)  # ReplyDone for q1, which is now tombstoned

            # A genuinely new reply, the model's own.
            await server.chat_delta("新的", question="q2")
            await _collect(events, 2)  # ReplyStarted, ReplyTextDelta

            # q1's tail crosses it, and must take only its own audio down.
            await server.sentence_start("q1")
            await server.audio_only(b"\x07\x08")
            await server.sentence_start("q2")
            await server.audio_only(b"\x09\x0a")

            got = await _collect(events, 1)
            assert isinstance(got[0], link.ReplyAudioDelta)
            assert got[0].pcm == b"\x09\x0a", "放出去的是 q1 的尾巴"
        finally:
            await volcano.aclose()


async def test_audio_on_its_own_never_mints_a_reply() -> None:
    """Raw audio has no id, so it cannot be told apart from a tail — and it
    used to walk into _ensure_active and mint a fresh handle. That is how a
    cancelled reply came back as a ghost: ReplyStarted for something nobody
    asked for, the interrupted audio played to the audience after all, and the
    one reply slot held until the 25-second watchdog let go.

    Every reply this endpoint starts announces itself with a frame that DOES
    carry a question_id, so audio has no business minting one.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            await volcano.cancel(handle)
            await _collect(events, 1)  # ReplyDone

            await server.audio_only(b"\x0b\x0c")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.2)

            # The slot is genuinely free, not merely quiet.
            again = await asyncio.wait_for(volcano.request_reply(link.ReplySpec()), timeout=0.5)
            assert again is not handle
        finally:
            await volcano.aclose()


async def test_the_slot_is_taken_by_one_waiter_at_a_time() -> None:
    """`Event.set()` wakes every waiter, and each then clears the event on its
    own — so two request_reply calls both walked out with the single slot. The
    loser's watchdog was cancelled by the winner's _take_slot, so it never
    settled at all and whoever awaited it hung. client.py holds a lock across
    the wait AND the take for exactly this reason.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            first = asyncio.create_task(volcano.request_reply(link.ReplySpec(instructions="甲")))
            second = asyncio.create_task(volcano.request_reply(link.ReplySpec(instructions="乙")))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            await asyncio.sleep(0.05)

            assert server.recorded.count(wire.ClientEvent.CHAT_TEXT_QUERY) == 1, "两个都拿到了名额"
            handle = await asyncio.wait_for(first, timeout=1.0)

            # Finish the first, and the second gets its turn rather than hanging.
            # Three events: the text delta, the audio delta, and the done — no
            # ReplyStarted, because this reply is one we asked for.
            await server.say("好", audio=b"\x01\x02")
            done = [e for e in await _collect(events, 3) if isinstance(e, link.ReplyDone)]
            assert done and done[0].handle is handle
            await asyncio.wait_for(second, timeout=1.0)
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY, count=2)
        finally:
            await volcano.aclose()


async def test_a_send_that_never_left_does_not_hold_the_slot() -> None:
    """Taking the slot before the send means a ConnectionError leaves it taken
    for the full watchdog — 25 seconds during which the retry the scheduler is
    about to make just waits. client.py:260-272 records the same lesson."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, auto_reconnect=False)
        try:
            await volcano._drop_socket()
            with pytest.raises(ConnectionError):
                await volcano.request_reply(link.ReplySpec())

            # Free again, so the next attempt is not stuck behind a ghost.
            volcano._ws = object()
            assert volcano._slot_free.is_set()
        finally:
            volcano._ws = None
            await volcano.aclose()


async def test_a_body_that_is_not_json_does_not_take_the_session_down() -> None:
    """decode() says yes and Frame.json() then says no, which is by design. But
    the dispatch that calls json() sat OUTSIDE the receive loop's guard, so one
    such frame killed the receive task with an exception nobody retrieved: the
    socket stayed open, no LinkDown was emitted, the reconnect ladder was never
    armed, and every later request_reply waited out its full watchdog against a
    link that had gone deaf.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await server.send_unparseable_body()
            await server.say("还在", audio=b"\x01\x02")

            got = await _collect(events, 4)
            assert any(isinstance(e, link.ReplyTextDelta) for e in got), "会话被一帧坏 body 带走了"
            assert not any(isinstance(e, link.LinkDown) for e in got)
        finally:
            await volcano.aclose()


# ---------------------------------------------------------- the session swap


async def test_two_context_pushes_do_not_race_into_two_sessions() -> None:
    """A swap has no serialisation of its own, and it does not take two callers
    to overlap two of them: a push deferred by _settle and the ticker's next
    refresh are enough. Overlapping, each cleared the event the other was
    waiting on and each generated its own session id — and the real endpoint,
    which is concurrent unlike this fake, answers the second StartSession with
    「session number limit exceeded: 1」. The link is then addressing an id the
    server never confirmed, which is a link that has quietly gone mute.
    """
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, config=_cfg(model="2.2.0.0", speaker="saturn_x"))
        try:
            await asyncio.gather(volcano.set_context("甲"), volcano.set_context("乙"))
            server.check()

            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body["dialog"]["character_manifest"] == "乙", "后来的那次人设没赢"
            # Coalesced: the first session plus one swap, not one swap each.
            assert server.recorded.count(wire.ClientEvent.START_SESSION) == 2
        finally:
            await volcano.aclose()


@pytest.mark.parametrize(
    ("answer_finish", "step"),
    [(False, "收掉上一个会话"), (True, "换会话之后")],
    ids=["旧会话收不掉", "新会话建不起来"],
)
async def test_a_swap_that_cannot_finish_takes_the_link_down(
    answer_finish: bool, step: str
) -> None:
    """Both failure paths used to just `return`, and that is the worst state
    this adapter can be in: the server has retired the session, `_started` is
    still True, no LinkDown was emitted, and every later frame goes to a
    session that no longer exists. On the panel it reads as a healthy link that
    simply stopped talking.

    Both branches also used wall-clock `asyncio.wait_for`, which is why neither
    had a test — the injected clock is what makes them reachable.
    """
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        server.answer_finish_session = answer_finish
        volcano, events = await _linked(
            server,
            config=_cfg(model="2.2.0.0", speaker="saturn_x"),
            clock=clock,
            swap_timeout_s=5.0,
            auto_reconnect=False,
        )
        try:
            if answer_finish:
                # Let FinishSession through, then refuse the StartSession that
                # follows, so the wait that times out is the second one.
                server.refuse_session = True
            swapping = asyncio.create_task(volcano.set_context("新人设"))
            await server.wait_for(wire.ClientEvent.FINISH_SESSION)
            await clock.advance(5.0)
            await asyncio.wait_for(swapping, timeout=2.0)

            # A refused StartSession also reports itself as a LinkError first,
            # which is worth having: it names the reason while the timeout is
            # still counting.
            got = await _collect(events, 2 if answer_finish else 1)
            down = [e for e in got if isinstance(e, link.LinkDown)]
            assert down, got
            assert step in down[0].reason
            assert down[0].retrying is False
        finally:
            await volcano.aclose()


# ------------------------------------------------- lifecycle and error classes


async def test_a_first_connect_is_not_a_recovery() -> None:
    """`LinkUp` means 「the link came BACK」 — client.py emits it from one place,
    the reconnect loop. Sharing `_open` between connect and the ladder made
    every startup announce a recovery: dev_talk printed 「已恢复（第 1 次尝试
    成功）」, the panel feed got 「语音连接已恢复」, and link_health.mark_up ran
    twice for one connection.
    """
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            await server.say("在的", audio=b"\x01\x02")
            got = await _collect(events, 3)
            assert not any(isinstance(e, link.LinkUp) for e in got), got
        finally:
            await volcano.aclose()


async def test_a_reconnect_reports_the_try_that_worked() -> None:
    """`link.py` and `obs/health.py` both define this number as 「第几次成功的，
    1 = 第一次」. `_reconnect` assigned `self._attempts` and then `_open` sent
    `self._attempts + 1`, so succeeding on the first retry reported 第 2 次 —
    into the streamer's banner and into the health probe."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            await server.drop()
            down = await _collect(events, 1)
            assert isinstance(down[0], link.LinkDown)
            await clock.advance(1.0)
            up = await _collect(events, 1)
            assert isinstance(up[0], link.LinkUp)
            assert up[0].attempts == 1, "第一次重试就成功，不该报第 2 次"
        finally:
            await volcano.aclose()


class _Unauthorized(OSError):
    """A handshake rejection, in the shape `errors.classify_error` reads."""

    status_code = 401


async def test_a_revoked_key_stops_the_ladder_instead_of_climbing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`realtime/errors.py` exists to answer 「retry or stop」 and this adapter
    never asked it. A dead credential answers the same way six times, so the
    ladder spent about a minute on a panel reading 「连接中」 before saying
    anything — and then said the retries failed, not that the key is bad.
    """
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            monkeypatch.setattr(
                volcano, "_open", lambda: (_ for _ in ()).throw(_Unauthorized("401"))
            )
            await server.drop()
            await _collect(events, 1)  # LinkDown(retrying=True)
            await clock.advance(1.0)

            got = await _collect(events, 1)
            assert isinstance(got[0], link.LinkDown)
            assert got[0].retrying is False, "吊销的 key 还在说自己会重试"
        finally:
            await volcano.aclose()


async def test_a_fatal_close_code_does_not_promise_a_retry() -> None:
    """1008 is 「你不受欢迎」, not 「我们被切断了」 — `errors.py` calls it fatal
    and says so in its own docstring. Reporting `retrying=True` for it makes
    the panel wait for a reconnection nobody is attempting."""
    # auto_reconnect ON. With it off, `retrying = self._auto_reconnect and not
    # fatal` is False whatever classify_error says, so the assertion held for
    # the wrong reason — replacing the classification with `fatal = False` left
    # the whole suite green.
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            await server.drop(code=1008)
            got = await _collect(events, 1)
            assert isinstance(got[0], link.LinkDown)
            assert got[0].retrying is False
        finally:
            await volcano.aclose()


async def test_the_watchdog_stops_the_far_end_too() -> None:
    """Giving up locally leaves the server generating. That is the same wasted
    spend `cancel` was wired to ClientInterrupt to stop, reached by the other
    road — and the road nobody is watching, because a timeout is by definition
    a turn that went quiet."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server, clock=clock, watchdog_s=25.0)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            await clock.advance(25.0)

            done = await _collect(events, 1)
            assert isinstance(done[0], link.ReplyDone)
            assert done[0].status is link.ReplyStatus.TIMED_OUT
            assert done[0].handle is handle
            await server.wait_for(wire.ClientEvent.CLIENT_INTERRUPT)
        finally:
            await volcano.aclose()


async def test_uplink_stops_while_the_session_is_being_replaced() -> None:
    """For the ~114 ms of a swap the old session is retired and the new one is
    unconfirmed. A frame addressed to either draws a server error, which lands
    in the ERROR branch and settles the active reply FAILED — so dropping a
    fifth of a second of microphone is the cheaper of the two.

    Not the s2s invariant (plan 3.3 rule 7): that one exists because the
    speculative reopen window runs on the audio clock, and this protocol has no
    such window.
    """
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        server.answer_finish_session = False
        volcano, _ = await _linked(
            server,
            config=_cfg(model="2.2.0.0", speaker="saturn_x"),
            clock=clock,
            swap_timeout_s=5.0,
            auto_reconnect=False,
        )
        try:
            swapping = asyncio.create_task(volcano.set_context("新人设"))
            # Wait for the state the gate actually keys on, not for a frame that
            # merely implies it. `wait_for(FINISH_SESSION)` returns when the
            # FAKE has recorded the frame, and that is a different clock from
            # the adapter's — the first version of this test raced on exactly
            # that difference and reported a leak that was not there.
            await _until(lambda: not volcano._session_ready.is_set())

            for _ in range(5):
                await volcano.push_audio(b"\x00\x01" * 320)
            # And give the fake's handler a turn before counting. Reading
            # `recorded` straight after a send is the race mock_volcano's own
            # docstring warns about, and it made this assertion vacuous: with
            # the gate deleted the test still passed, because nothing had
            # landed yet.
            await asyncio.sleep(0.1)
            assert (
                server.recorded.count(wire.ClientEvent.TASK_REQUEST) == 0
            ), f"有帧发给了正在被收掉的会话：{server.recorded.events}"
            assert volcano._dropped_uplink == 5

            await clock.advance(5.0)
            await asyncio.wait_for(swapping, timeout=2.0)
        finally:
            await volcano.aclose()


async def test_closing_ends_a_reply_that_was_still_in_flight() -> None:
    """aclose cancels the watchdog, which is the only other thing that could
    have ended this reply — so without settling here the scheduler awaiting
    that handle never wakes. The disconnect path has always done it."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
        finally:
            await volcano.aclose()

        done = await _collect(events, 1)
        assert isinstance(done[0], link.ReplyDone)
        assert done[0].handle is handle
        assert handle.stale


async def test_closing_leaves_nothing_still_running() -> None:
    """client.py:206-214 records the lesson: cancelling without awaiting left a
    ladder still climbing after aclose returned. The window is real here — a
    reconnect sitting just past `websockets.connect` would assign the new
    socket over the one we just dropped, set `_started` and start a receive
    task, all after we said we were done."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, clock=clock, reconnect_backoff_s=5.0)
        await volcano.request_reply(link.ReplySpec())
        await server.drop()
        await _until(lambda: volcano._reconnect_task is not None)

        await volcano.aclose()

        still_running = [
            name
            for name, task in (
                ("watchdog", volcano._watchdog),
                ("reconnect", volcano._reconnect_task),
                ("recv", volcano._recv),
            )
            if task is not None and not task.done()
        ]
        assert not still_running, f"aclose 返回了，这些还在跑：{still_running}"


async def test_a_socket_dropped_while_a_send_waits_for_the_lock_says_so() -> None:
    """The `_ws is None` check has to be inside the lock and read through a
    local name. Outside it, a caller passes the check, blocks on the lock, and
    acquires it after the receive loop has nulled the socket — then runs
    `None.send` and raises AttributeError, which `cancel()` does not catch and
    which escapes a spawned barge-in task instead of taking its intended
    「socket 都死了，她自然已经停了」 path."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(server, auto_reconnect=False)
        try:
            await volcano._send_lock.acquire()
            blocked = asyncio.create_task(volcano.push_audio(b"\x00\x01" * 320))
            await asyncio.sleep(0.02)
            volcano._ws = None
            volcano._send_lock.release()

            with pytest.raises(ConnectionError):
                await asyncio.wait_for(blocked, timeout=1.0)
        finally:
            await volcano.aclose()


async def test_the_command_line_model_reaches_the_wire() -> None:
    """`--model` lands on the resolved endpoint and used to stop there: the
    banner and `volcano.session_started` printed the generation the caller
    asked for while `_session_body` sent `cfg.model`. Two silent failures in
    one, because the generation decides which key the persona travels under
    and which voice family is legal."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(
            server,
            config=_cfg(model="1.2.1.1"),
            model="2.2.0.0",
            speaker="saturn_x",
        )
        try:
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body["dialog"]["extra"]["model"] == "2.2.0.0"
        finally:
            await volcano.aclose()


async def test_the_name_survives_the_channel_production_actually_uses() -> None:
    """The assembly connects first and pushes the persona afterwards, so on
    O2.0 the persona arrives in an UpdateConfig — which the vendor documents as
    a FULL replacement. If bot_name did not ride along with it, she would be
    named for one frame and 「豆包」 for the rest of the stream. Nothing pinned
    that; the tests that touched bot_name all used StartSession."""
    async with MockVolcanoServer() as server:
        volcano, _ = await _linked(
            server, config=_cfg(model="1.2.1.1", speaker="zh_female_x"), bot_name="豆腐"
        )
        try:
            await volcano.set_context("我叫豆腐，是主播的AI搭子。" * 20)
            await server.wait_for(wire.ClientEvent.UPDATE_CONFIG)

            dialog = server.recorded.body_for(wire.ClientEvent.UPDATE_CONFIG)["dialog"]
            assert dialog["bot_name"] == "豆腐"
            assert dialog["system_role"].startswith("我叫豆腐")
        finally:
            await volcano.aclose()


# ------------------------------------------------------------ pause (suspend/resume)


async def test_suspend_parks_the_link_and_resume_carries_the_dialogue_back() -> None:
    """The pause gate on this adapter cannot use aclose (its _closing is
    permanent), so suspend is the same teardown minus the finality — and
    resume's StartSession carries the surviving dialog_id, the same
    continuation the reconnect ladder performs."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server, clock=clock, reconnect_backoff_s=1.0)
        try:
            await volcano.suspend()
            # Locals, not attribute asserts: mypy narrows the attribute to a
            # literal and calls the rest of the test unreachable.
            paused = volcano._suspended
            assert paused
            # A dead socket while suspended must not be reported or retried.
            await clock.advance(30.0)
            assert volcano._reconnect_task is None

            await volcano.resume()
            await server.wait_ready()
            resumed = volcano._suspended
            assert not resumed
            assert server.resumed_with == [
                server.dialog_id
            ], f"恢复没带 dialog_id，暂停一次她就失忆：{server.resumed_with}"
        finally:
            await volcano.aclose()


async def test_fresh_conversation_does_not_resume_old_dialogue() -> None:
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            volcano._done.append("上一例问题编号")
            volcano._pending_item = "上一例未发出的弹幕"
            await volcano.reset_conversation("本例背景")
            await server.wait_ready()
            assert server.resumed_with == []
            assert volcano._context == "本例背景"
            assert not volcano._done
            assert not volcano._pending_item
        finally:
            await volcano.aclose()


async def test_uplink_during_suspend_is_dropped_at_the_counter_not_raised() -> None:
    """The microphone pump keeps pushing while paused; those frames must die
    quietly at the session gate instead of raising into the pump's retry."""
    clock = FakeClock()
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server, clock=clock)
        try:
            await volcano.suspend()
            before = volcano._dropped_uplink
            await volcano.push_audio(b"\x00\x00" * 320)
            assert volcano._dropped_uplink == before + 1
        finally:
            await volcano.aclose()


async def test_assistant_write_backs_never_overwrite_the_stashed_danmaku() -> None:
    """add_context_item is a stash for the NEXT query's body on this protocol.
    An assistant-role write-back landing between a danmaku's stash and its
    request_reply would replace the viewer's words with her own."""
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            await volcano.add_context_item("[弹幕] 阿强: 显存怎么算")
            await volcano.add_context_item("我刚才说过了……", role="assistant")
            assert volcano._pending_item == "[弹幕] 阿强: 显存怎么算"
        finally:
            await volcano.aclose()


async def test_a_scoped_base_instruction_is_ignored_with_one_warning() -> None:
    """No per-reply instruction channel here: the capability bit says so, the
    Assembly folds event rules into the session context, and a spec that
    still carries one is a wiring bug worth exactly one log line."""
    import logging

    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("bilisama.realtime.providers.volcano")
    sink = _Sink()
    logger.addHandler(sink)
    try:
        async with MockVolcanoServer() as server:
            volcano, _events = await _linked(server)
            try:
                first = await volcano.request_reply(link.ReplySpec(base_instructions="事件规则"))
                await volcano.cancel(first)
                await volcano.request_reply(link.ReplySpec(base_instructions="事件规则"))
            finally:
                await volcano.aclose()
    finally:
        logger.removeHandler(sink)
    assert lines.count("volcano.base_instructions_ignored") == 1, lines


async def test_set_bot_name_swaps_the_session_on_the_o_generation() -> None:
    """Probed live 2026-08-28: an UpdateConfig carrying dialog.bot_name is
    accepted and IGNORED — asked her name right after, she answered the old
    one. The previous version of this test pinned exactly that mercy of the
    fake server («a rename is not a new session»); the real carrier is the
    next StartSession, so a rename swaps sessions on the same dialog."""
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            starts_before = server.recorded.count(wire.ClientEvent.START_SESSION)
            await volcano.set_context("你是豆腐。")
            await volcano.set_bot_name("奶豆")
            await server.wait_for(wire.ClientEvent.FINISH_SESSION)
            await server.wait_for(wire.ClientEvent.START_SESSION, count=starts_before + 1)
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body.get("dialog", {}).get("bot_name") == "奶豆"
            assert volcano._bot_name == "奶豆"
            # A rename with UNCHANGED persona text must still swap: the swap
            # dedupe compares context, and the stale marker defeats it.
            await volcano.set_bot_name("雪豆")
            await server.wait_for(wire.ClientEvent.START_SESSION, count=starts_before + 2)
        finally:
            await volcano.aclose()


async def test_a_rename_queued_behind_a_running_swap_still_swaps() -> None:
    """The stale marker is written under _swap_lock: set outside it, an
    in-flight swap's completion (`_swapped_context = sent`) erased the marker
    and the queued rename lost to the very dedupe it exists to defeat."""
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            await volcano.set_context("你是豆腐。")
            starts_before = server.recorded.count(wire.ClientEvent.START_SESSION)
            # Two renames in quick succession: the second queues behind the
            # first swap's lock and must still produce its own StartSession.
            await asyncio.gather(volcano.set_bot_name("奶豆"), volcano.set_bot_name("雪豆"))
            await server.wait_for(wire.ClientEvent.START_SESSION, count=starts_before + 2)
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body.get("dialog", {}).get("bot_name") == "雪豆"
        finally:
            await volcano.aclose()


async def test_set_speaker_swaps_the_session_with_the_new_voice() -> None:
    """The panel's voice change on this provider: tts.speaker rides the
    StartSession body on BOTH generations, so the carrier is a session swap —
    same socket, same dialog_id, next sentence in the new voice."""
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            await volcano.set_context("你是豆腐。")
            starts_before = server.recorded.count(wire.ClientEvent.START_SESSION)
            await volcano.set_speaker("zh_female_vv_jupiter_bigtts")
            await server.wait_for(wire.ClientEvent.FINISH_SESSION)
            await server.wait_for(wire.ClientEvent.START_SESSION, count=starts_before + 1)
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body.get("tts", {}).get("speaker") == "zh_female_vv_jupiter_bigtts"
            # Unchanged persona text must not dedupe the swap away — the
            # stale marker exists for exactly this (the rename learned it).
            await volcano.set_speaker("zh_male_yunzhou_jupiter_bigtts")
            await server.wait_for(wire.ClientEvent.START_SESSION, count=starts_before + 2)
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body.get("tts", {}).get("speaker") == "zh_male_yunzhou_jupiter_bigtts"
        finally:
            await volcano.aclose()


async def test_a_wrong_generation_speaker_is_refused_without_a_swap() -> None:
    """The pairing is silent on the wire (she becomes someone else, or goes
    mute), so the setter refuses it loudly instead — and must not have half
    torn the session down first."""
    async with MockVolcanoServer() as server:
        volcano, _events = await _linked(server)
        try:
            finishes_before = server.recorded.count(wire.ClientEvent.FINISH_SESSION)
            with pytest.raises(ValueError, match="克隆音色"):
                await volcano.set_speaker("saturn_someone_else")
            assert volcano._speaker == "zh_female_test", "拒绝后旧值必须还在"
            await asyncio.sleep(0.05)
            assert server.recorded.count(wire.ClientEvent.FINISH_SESSION) == finishes_before
        finally:
            await volcano.aclose()


async def test_a_speaker_set_before_connect_rides_the_first_start_session() -> None:
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(server.url, app_id="app", access_key="key", config=_cfg())
        await volcano.set_speaker("zh_female_vv_jupiter_bigtts")
        await volcano.connect()
        try:
            await server.wait_ready()
            body = server.recorded.body_for(wire.ClientEvent.START_SESSION)
            assert body.get("tts", {}).get("speaker") == "zh_female_vv_jupiter_bigtts"
        finally:
            await volcano.aclose()
