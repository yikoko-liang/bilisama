"""VolcanoLink against a fake that refuses things.

The four semantic gaps in the module docstring are what these tests are mostly
about — no response.create, no cancel, no per-turn instructions slot, and a
reply that ends when the AUDIENCE stops hearing rather than when the model
stops generating. Each one is a place where doing the obvious thing produces a
link that looks fine and behaves wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

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


async def _linked(
    server: MockVolcanoServer, **kw: Any
) -> tuple[VolcanoLink, AsyncIterator[link.LinkEvent]]:
    volcano = VolcanoLink(server.url, app_id="app", access_key="key", config=_cfg(), **kw)
    await volcano.connect()
    await server.wait_ready()
    events = volcano.events()
    await _collect(events, 1)  # LinkUp
    return volcano, events


# ------------------------------------------------------------------ handshake


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
        await volcano.set_context("你是米娅，主播的AI搭子。")
        await volcano.connect()
        await server.wait_ready()
        try:
            dialog = server.recorded.body_for(wire.ClientEvent.START_SESSION)["dialog"]
            assert dialog[key] == "你是米娅，主播的AI搭子。"
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
        await volcano.set_context("你是米娅，说话短促爱接梗。")
        await volcano.connect()
        await server.wait_ready()
        try:
            await volcano.request_reply(link.ReplySpec(instructions="说句话"))
            await server.wait_for(wire.ClientEvent.CHAT_TEXT_QUERY)
            content = server.recorded.body_for(wire.ClientEvent.CHAT_TEXT_QUERY)["content"]
            assert "米娅" not in content, "人设跟着每一轮又发了一遍"
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

            got = await _collect(events, 5)
            assert isinstance(got[0], link.SpeechStarted)
            done = [e for e in got if isinstance(e, link.ReplyDone)]
            assert done and done[0].status is link.ReplyStatus.CANCELLED
            assert isinstance(got[-1], link.SpeechStopped)
        finally:
            await volcano.aclose()


async def test_a_barged_in_reply_goes_stale_so_late_audio_is_dropped() -> None:
    """Nothing on the wire stops generation, so the frames keep coming. What
    makes the barge-in sound clean is the handle going stale — both audio
    consumers drop anything carrying it."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            await server.barge_in()
            await _collect(events, 5)
            assert handle.stale
        finally:
            await volcano.aclose()


async def test_cancel_is_local_and_says_so() -> None:
    """No wire event stops the model. cancel() settles our side and nothing
    else — the audience hears the right thing, the tokens are spent anyway."""
    async with MockVolcanoServer() as server:
        volcano, events = await _linked(server)
        try:
            handle = await volcano.request_reply(link.ReplySpec())
            before = list(server.recorded.events)
            await volcano.cancel(handle)

            done = await _collect(events, 1)
            assert isinstance(done[0], link.ReplyDone)
            assert done[0].status is link.ReplyStatus.CANCELLED
            assert handle.stale
            assert server.recorded.events == before, "cancel 往线上发了东西"
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
    """The persona says 「我叫米娅」 in its first line and that is not enough.

    dialog.bot_name defaults to 豆包, and against our real 557-character
    persona it wins: probed 2026-08-28, three answers out of three were
    「豆包」 without this field and 「米娅」 with it. A 31-character persona
    saying the same thing DID win on its own, which is exactly why the gap
    survived early testing.
    """
    async with MockVolcanoServer() as server:
        volcano = VolcanoLink(
            server.url, api_key="k", config=_cfg(model="1.2.1.1"), bot_name="米娅"
        )
        await volcano.set_context("我叫米娅，是这个直播间的 AI 伴播。")
        await volcano.connect()
        await server.wait_ready()
        try:
            dialog = server.recorded.body_for(wire.ClientEvent.START_SESSION)["dialog"]
            assert dialog["bot_name"] == "米娅"
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
            server.url, api_key="k", config=_cfg(model="2.2.0.0"), bot_name="米娅"
        )
        await volcano.set_context("你叫米娅，直播间的 AI 伴播。")
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
