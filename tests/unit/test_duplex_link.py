"""DuplexLink against a fake that behaves like the endpoint measured on 2026-09-16.

The facts under test are the ones that made this provider need its own adapter
rather than a codec entry, and each is a thing the real endpoint does that the
other three do not. They are listed in providers/duplex.py's header; here they
are executable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bilisama.config.schema import VolcanoDuplexConfig
from bilisama.realtime import link
from bilisama.realtime.providers.duplex import DuplexLink, session_body
from tests.fakes.mock_duplex import _KNOWN as _VOCABULARY
from tests.fakes.mock_duplex import MockDuplexServer


async def _next[E: link.LinkEvent](
    events: AsyncIterator[link.LinkEvent], wanted: type[E], timeout: float = 2.0
) -> E:
    async def pump() -> E:
        seen: list[str] = []
        async for event in events:
            if isinstance(event, wanted):
                return event
            seen.append(type(event).__name__)
        raise AssertionError(f"事件流结束了也没等到 {wanted.__name__}，只看到 {seen}")

    return await asyncio.wait_for(pump(), timeout=timeout)


async def _settles(check: Any, what: str, timeout: float = 2.0) -> None:
    """The fake handles frames on its own task; a send returning does not mean
    it has been read."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


def _link(server: MockDuplexServer, **kw: object) -> DuplexLink:
    return DuplexLink(server.url, api_key="k", config=VolcanoDuplexConfig(), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------ the handshake


async def test_the_client_speaks_first() -> None:
    """RealtimeClient opens a socket and waits for session.created. Here the
    session does not exist until we ask for it, and a client that waits for a
    greeting waits forever — the first reason this provider is not a codec."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await asyncio.wait_for(duplex.connect(), timeout=3.0)
        try:
            assert server.recorded.count("session.create") == 1
            created = server.recorded.last("session.create")
            assert created["session"]["model"] == "1.2.6.1"
            assert created["session"]["audio"]["input"]["format"]["rate"] == 16000
            assert created["session"]["audio"]["output"]["format"]["rate"] == 24000
        finally:
            await duplex.aclose()


def test_the_audio_format_names_the_width_not_just_the_family() -> None:
    """`pcm` and `pcm_s16le` are both accepted and are not the same thing.

    `pcm` hands back float32 on the downlink. Everything downstream — the
    broker, the page's AudioContext, the local speaker — reads signed 16-bit,
    and float32 read that way is full-scale white noise. There is no error
    frame, no warning, and `session.created` looks identical, so the only
    thing standing between this and a stream of noise is the literal below.
    """
    body = session_body(VolcanoDuplexConfig(), instructions="p", model="1.2.6.1", speaker="v")
    audio = body["session"]["audio"]
    assert audio["output"]["format"] == {"type": "pcm_s16le", "rate": 24000}
    assert audio["input"]["format"] == {"type": "pcm_s16le", "rate": 16000}


async def test_a_refused_session_raises_rather_than_hangs() -> None:
    from bilisama.realtime.client import SessionRefused

    async with MockDuplexServer(refuse_session=True) as server:
        duplex = _link(server)
        with pytest.raises(SessionRefused):
            await asyncio.wait_for(duplex.connect(), timeout=3.0)


async def test_closing_says_goodbye_first() -> None:
    """Dropping the socket without session.close draws ContextCanceled
    (55000001) from the real endpoint — the vendor is explicit about it."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        await duplex.aclose()
        await _settles(
            lambda: server.recorded.count("session.close") == 1, "退出前没发 session.close"
        )


# ------------------------------------------------------------ whose words


async def test_a_reply_we_asked_for_is_not_mistaken_for_her_own() -> None:
    """The reason the shared client could not be reused.

    Nothing announces a reply here, and RealtimeClient books an unannounced
    one as the provider's own VAD turn — which would put the danmaku answer
    we commissioned in front of the voice gate, where it would be skipped for
    carrying no marker. tts_type is the only thing that tells them apart.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await duplex.request_reply(link.ReplySpec(text="谢谢小明送的火箭"))
            started = await _next(events, link.ReplyStarted)
            assert started.handle is handle
            assert not started.handle.implicit, "我们点的回复不是她自起的轮"

            # The TTS path streams no text, so the adapter reports the words
            # it supplied — otherwise the chat page and the memory layer see
            # a reply with no content at all.
            delta = await _next(events, link.ReplyTextDelta)
            assert delta.text == "谢谢小明送的火箭"
            done = await _next(events, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
        finally:
            await duplex.aclose()


async def test_a_turn_she_started_is_implicit() -> None:
    """The other half: her own VAD turn must reach the gate."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.say("我在呢", ours=False)
            started = await _next(events, link.ReplyStarted)
            assert started.handle.implicit, "她自起的轮必须过语音门"
            delta = await _next(events, link.ReplyTextDelta)
            assert delta.text == "我"
        finally:
            await duplex.aclose()


async def test_her_whole_sentence_survives_including_what_came_before_the_audio() -> None:
    """Her text starts streaming BEFORE the frame that announces the turn.

    Booking on the audio frame threw the opening away — eleven characters in
    one measured run. Nothing looked wrong: she was audible, and the panel
    just showed her starting mid-sentence.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.say("我主要是陪主播聊天", ours=False)
            started = await _next(events, link.ReplyStarted)
            assert started.handle.implicit, "她自起的轮必须过语音门"

            said = ""

            async def until_done() -> str:
                whole = ""
                async for event in events:
                    if isinstance(event, link.ReplyTextDelta):
                        whole += event.text
                    elif isinstance(event, link.ReplyDone):
                        assert event.text == "我主要是陪主播聊天", "结算时报的是整句"
                        return whole
                raise AssertionError("事件流结束了也没等到 ReplyDone")

            said = await asyncio.wait_for(until_done(), timeout=2.0)
            assert said == "我主要是陪主播聊天", "开头那两个字不能在音频帧之前掉了"
        finally:
            await duplex.aclose()


async def test_what_she_said_reaches_the_done_not_the_words_we_last_supplied() -> None:
    """`ReplyDone.text` is what memory, the chat page and the log all read
    (scheduler.py:917). Reporting the TTS text we happened to hold made her
    own turns arrive empty — audible, and invisible everywhere else."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            # A reply we drove first, so the supplied words are sitting there
            # to be wrongly reported by the turn that follows.
            await duplex.request_reply(link.ReplySpec(text="谢谢小明送的火箭"))
            ours = await _next(events, link.ReplyDone)
            assert ours.text == "谢谢小明送的火箭", "我们点的回复报的是我们给的词"

            await server.say("刚才那个火箭真好看", ours=False)
            hers = await _next(events, link.ReplyDone)
            assert hers.text == "刚才那个火箭真好看", "她自己的轮报的必须是她自己说的"
        finally:
            await duplex.aclose()


async def test_cancelling_a_reply_that_never_announced_frees_the_slot() -> None:
    """A barge-in can land before the reply has said a word.

    `_active` is only set when the audio frame announces the reply, so a
    cancel arriving first used to return having sent nothing and settled
    nothing — and the slot then sat taken until the 25-second watchdog, with
    nothing dispatchable in the meantime. Measured live on 2026-09-17: reply
    requested at :47, watchdog at :12.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await duplex.request_reply(link.ReplySpec(text="谢谢小明送的火箭"))
            await duplex.cancel(handle)  # before any response.output_audio.started
            done = await _next(events, link.ReplyDone)
            assert done.handle is handle
            assert done.status is link.ReplyStatus.CANCELLED
            # The slot is what matters: the next reply must not have to wait
            # out a watchdog to get it.
            await asyncio.wait_for(duplex.request_reply(link.ReplySpec(text="下一条")), timeout=1.0)
        finally:
            await duplex.aclose()


async def test_asking_for_a_reply_without_words_is_refused_not_guessed() -> None:
    """A spec with no text is a wiring bug in L3. Making something up would
    put words in her mouth that nobody wrote, so it raises instead."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            with pytest.raises(ValueError, match="只会照着念"):
                await duplex.request_reply(link.ReplySpec(instructions="回一句"))
            assert server.recorded.count("speech_text_buffer.replacement.commit") == 0
        finally:
            await duplex.aclose()


# ------------------------------------------------------------ what lands where


async def test_history_waits_for_the_next_session_because_mid_session_is_ignored() -> None:
    """Measured: conversation.item.create acks and never reaches her. So the
    item is stashed and rides dialog_context on the next create, which does."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.add_context_item("[弹幕] 观众A: 主播今天播到几点")
            await asyncio.sleep(0.05)  # anything it DID send would have landed
            assert (
                server.recorded.count("conversation.item.create") == 0
            ), "中途注入服务端照收不误，但她看不见——所以这里根本不发"
        finally:
            await duplex.aclose()

    async with MockDuplexServer() as second:
        duplex = _link(second)
        await duplex.add_context_item("[弹幕] 观众A: 主播今天播到几点")
        await duplex.add_context_item("那得看嗓子撑不撑得住。", role="assistant")
        await duplex.connect()
        try:
            assert second.context == [
                {"role": "user", "text": "[弹幕] 观众A: 主播今天播到几点"},
                {"role": "assistant", "text": "那得看嗓子撑不撑得住。"},
            ], "建会话时它必须走 dialog_context"
            assert not second.context_refused
        finally:
            await duplex.aclose()


async def test_a_danmaku_she_never_answered_does_not_void_the_whole_history() -> None:
    """The endpoint takes strictly alternating turns and discards EVERYTHING
    when it does not get them — measured 2026-09-16. A stash of danmaku is
    naturally a run of user turns, so without pair_up the reconnect would come
    back with no memory at all and nothing would say so."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.add_context_item("[弹幕] 观众A: 主播今天播到几点")
        await duplex.add_context_item("[弹幕] 观众B: 这把打完还播吗")
        await duplex.add_context_item("播到十点，这把打完再说。", role="assistant")
        await duplex.add_context_item("[弹幕] 观众C: 还没人回我")  # 她还没答的那条
        await duplex.connect()
        try:
            assert not server.context_refused, "发出去的形状必须是端点收得下的"
            assert server.context == [
                {
                    "role": "user",
                    "text": "[弹幕] 观众A: 主播今天播到几点\n[弹幕] 观众B: 这把打完还播吗",
                },
                {"role": "assistant", "text": "播到十点，这把打完再说。"},
            ], "连着的弹幕并成一轮，没人接的那条留到下次"
        finally:
            await duplex.aclose()


def test_pair_up_keeps_what_it_can_and_hands_back_what_it_cannot() -> None:
    from bilisama.realtime.providers.duplex import pair_up

    kept, dropped = pair_up(
        [
            {"role": "assistant", "text": "没头没尾的一句"},
            {"role": "user", "text": "问题一"},
            {"role": "user", "text": "问题二"},
            {"role": "assistant", "text": "一起答了"},
            {"role": "user", "text": "还没答的"},
        ]
    )
    assert kept == [
        {"role": "user", "text": "问题一\n问题二"},
        {"role": "assistant", "text": "一起答了"},
    ]
    assert [d["text"] for d in dropped] == ["没头没尾的一句", "还没答的"]
    assert pair_up([]) == ([], [])


# ------------------------------------------------------------ she pulls them herself


async def test_the_pull_tool_is_only_declared_when_that_route_is_chosen() -> None:
    """Declaring it changes what she does on every turn, so it is not a
    harmless default: under the ghostwriter route L3 answers the danmaku and a
    tool she could call would have her answering it twice."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            assert server.tools == []
            assert "get_live_events" not in server.instructions
        finally:
            await duplex.aclose()

    async with MockDuplexServer() as second:
        duplex = _link(second, pull_tool=True)
        await duplex.connect()
        try:
            assert [t["name"] for t in second.tools] == ["get_live_events"]
            # Without the rule she has a tool and no idea it exists.
            assert "get_live_events" in second.instructions
        finally:
            await duplex.aclose()


async def test_filed_events_go_back_when_she_asks_for_them() -> None:
    async with MockDuplexServer() as server:
        duplex = _link(server, pull_tool=True)
        await duplex.connect()
        try:
            await duplex.add_context_item("[弹幕] 观众A: 今天播到几点")
            await duplex.add_context_item("[礼物] 观众B 送了火箭")
            await server.wants_events(call_id="call-abc")
            await _settles(lambda: len(server.tool_results) == 1, "她要了，但没回传")
            assert server.tool_results[0] == "[弹幕] 观众A: 今天播到几点\n[礼物] 观众B 送了火箭"

            # Handed over once. A second pull must not read them again, or she
            # thanks the same rocket twice.
            await server.wants_events(call_id="call-def")
            await _settles(lambda: len(server.tool_results) == 2, "第二次没回传")
            assert "暂时没有" in server.tool_results[1]
        finally:
            await duplex.aclose()


async def test_her_own_lines_are_not_filed_as_viewer_events() -> None:
    """`add_context_item(role="assistant")` is the scheduler mirroring what she
    said (scheduler.py:287). Filing it would read it back to her as a viewer
    message on the next pull."""
    async with MockDuplexServer() as server:
        duplex = _link(server, pull_tool=True)
        await duplex.connect()
        try:
            await duplex.add_context_item("谢谢火箭！", role="assistant")
            await server.wants_events()
            await _settles(lambda: len(server.tool_results) == 1, "没回传")
            assert "暂时没有" in server.tool_results[0]
        finally:
            await duplex.aclose()


async def test_nothing_is_filed_when_l3_is_the_one_answering() -> None:
    """Under the ghostwriter route the queue would only ever grow."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.add_context_item("[弹幕] 观众A: 今天播到几点")
            assert not duplex._unpulled, "代笔路线下没人来拉，攒起来只会越攒越多"
        finally:
            await duplex.aclose()


async def test_an_event_outside_the_vocabulary_is_never_sent() -> None:
    """Any unknown uplink name hangs the socket up — fifteen guesses, fifteen
    1006s. So a typo in an event name is not a warning, it is a dropped call."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.set_context("你叫豆腐")
            await duplex.add_context_item("[弹幕] 观众A: 在吗")
            await duplex.request_reply(link.ReplySpec(text="在的"))
            await _settles(
                lambda: server.recorded.count("speech_text_buffer.replacement.commit") == 1,
                "回复没发出去",
            )
            unknown = [f for f in server.recorded.frames if f.get("type") not in _VOCABULARY]
            assert not unknown, f"发了协议里没有的事件：{[f.get('type') for f in unknown]}"
        finally:
            await duplex.aclose()


async def test_the_persona_goes_out_in_place() -> None:
    """instructions are the one thing this protocol applies mid-session —
    probed by name-swap on the real endpoint. No session churn for a persona."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.set_context("你叫豆腐")
            await _settles(lambda: server.instructions == "你叫豆腐", "人设没推到服务端")
            assert server.recorded.count("session.create") == 1, "改人设不该换会话"
        finally:
            await duplex.aclose()


async def test_what_the_streamer_said_lands_as_a_finished_line() -> None:
    """`UserTranscriptDone` is the streamer's side of the conversation.

    The chat page writes its line from it (ui/events.py:199), the console
    prints 你说： from it and `on_transcript` fires from it
    (dev_talk.py:1180). Sending only `SpeechStopped` left the panel showing
    her half of every exchange and none of his — which reads as a UI bug and
    is this adapter.

    The words have to be rebuilt here: the `completed` frame's own
    `transcript` is empty on this protocol, so the last cumulative delta is
    the only copy.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("今天打算打几把游戏", flap="？")
            done = await _next(events, link.UserTranscriptDone)
            assert done.text == "今天打算打几把游戏？", "报的是转写最后定下来的那一版"
        finally:
            await duplex.aclose()


async def test_the_transcript_deltas_go_up_as_increments_not_as_restatements() -> None:
    """Cumulative on this protocol（「你」「你叫」「你叫什么」）, incremental in
    the contract everywhere else. Nothing reads them today, which is exactly
    why forwarding them raw would sit there until something did."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        seen: list[str] = []

        async def collect() -> None:
            async for event in events:
                if isinstance(event, link.UserTranscriptDelta):
                    seen.append(event.text)
                elif isinstance(event, link.UserTranscriptDone):
                    return

        try:
            await server.hear("你叫什么", flap="？")
            await asyncio.wait_for(collect(), timeout=2.0)
            assert seen == [
                "你",
                "叫",
                "什",
                "么",
                "？",
            ], f"送上去的该是增量，而且标点反复也只送一次，实际是 {seen}"
        finally:
            await duplex.aclose()


async def test_a_transcript_that_never_closes_still_hands_the_floor_back() -> None:
    """A start with no end wedges the gate shut for the rest of the session.

    `SpeechStopped` is the only thing that clears `streamer_speaking`
    (floor.py:97), and on this protocol its only source is the transcriber's
    completed frame — every other provider has a VAD edge that does not depend
    on transcription. A live session on 2026-09-17 lost that frame and she
    answered nothing afterwards, every intent gated `gate.host_speaking`.
    """
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=20.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我说到一半")
            await _next(events, link.SpeechStarted)
            await clock.advance(21.0)
            done = await _next(events, link.UserTranscriptDone)
            assert done.text == "我说到一半", "超时收尾也要把听到的报上去"
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_a_real_ending_cancels_the_backstop() -> None:
    """Otherwise the timer fires into the next utterance and drops the floor
    while the streamer is still talking."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=20.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("说完了")
            await _next(events, link.SpeechStopped)
            await clock.advance(30.0)
            await server.hear_without_ending("下一句")
            await _next(events, link.SpeechStarted)
            # Only ONE stopped may be in flight: the first turn's backstop must
            # be gone, or this start is closed the instant it opens.
            await asyncio.sleep(0.05)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(
                isinstance(e, link.SpeechStopped) for e in pending
            ), f"上一句的兜底计时器还活着，把这一句当场收了：{pending}"
        finally:
            await duplex.aclose()


async def test_speech_edges_come_off_the_transcriber() -> None:
    """There is no input_audio_buffer.speech_started on this protocol; the
    floor would never close for a client that waited for one."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("在这里")
            await _next(events, link.SpeechStarted)
            first = await _next(events, link.UserTranscriptDelta)
            assert first.text == "在"
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_an_error_frame_reaches_l3() -> None:
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.fail(code="50000000", message="模型推理出错")
            err = await _next(events, link.LinkError)
            assert err.code == "50000000"
        finally:
            await duplex.aclose()


# ------------------------------------------------------------ the frame builder


def test_dialog_context_only_rides_when_it_can_work() -> None:
    """session_body never slips history into an update: it is accepted there
    and silently ignored, which is the worst of both — an ack and no effect."""
    cfg = VolcanoDuplexConfig()
    with_history = session_body(
        cfg,
        instructions="p",
        model="1.2.6.1",
        speaker="v",
        history=[{"role": "user", "text": "hi"}],
    )
    assert with_history["extension"]["dialog"]["dialog_context"] == [{"role": "user", "text": "hi"}]
    without = session_body(cfg, instructions="p", model="1.2.6.1", speaker="v")
    assert without["extension"]["dialog"] == {}
    assert "id" not in without["session"], "没有 session id 时不该发一个空的"
