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


async def test_a_reply_the_endpoint_never_speaks_lets_go_of_the_slot_early() -> None:
    """The endpoint takes a replacement and sometimes plays nothing — no
    error, no refusal, no frames. The slot it holds is the same one her own
    microphone turns need, so the old 25-second watchdog meant 25 seconds of
    her ignoring the streamer. Measured twice in a row in a live session on
    2026-09-17."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer(deaf_to_tts=True) as server:
        duplex = _link(server, clock=clock, unspoken_s=6.0, watchdog_s=25.0)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await duplex.request_reply(link.ReplySpec(text="她永远不会念这句"))
            await clock.advance(7.0)
            done = await _next(events, link.ReplyDone)
            assert done.handle is handle
            assert done.status is link.ReplyStatus.CANCELLED
            await asyncio.wait_for(duplex.request_reply(link.ReplySpec(text="下一条")), timeout=1.0)
        finally:
            await duplex.aclose()


async def test_her_own_turn_takes_the_slot_from_a_reply_that_never_started() -> None:
    """She is audibly talking; ours is hypothetical. Holding the slot for the
    hypothetical one is what muted her."""
    async with MockDuplexServer(deaf_to_tts=True) as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            ours = await duplex.request_reply(link.ReplySpec(text="她永远不会念这句"))
            await server.say("我先说了啊", ours=False)
            gone = await _next(events, link.ReplyDone)
            assert gone.handle is ours, "没开口的那条要让位"
            assert gone.status is link.ReplyStatus.CANCELLED
            started = await _next(events, link.ReplyStarted)
            assert started.handle.implicit, "她自己的轮次要booked得上"
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
    """The uplink vocabulary is closed, so a typo is a frame the endpoint
    refuses rather than a field it ignores.

    It is not, however, a dropped call. That was this docstring's claim until
    2026-09-21 ("fifteen guesses, fifteen 1006s") and the endpoint disagrees:
    an unknown name draws `45000000 unknown event name:<name>` and the socket
    stays up — see test_an_unknown_event_name_is_an_error_frame_not_a_hang_up.
    The rule this test keeps is still worth keeping; only the stated penalty
    was wrong."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.set_context("你叫豆腐")
            await duplex.add_context_item("[弹幕] 观众A: 在吗")
            await duplex.request_reply(link.ReplySpec(text="在的"))
            await _settles(
                lambda: server.recorded.count("speech_text_buffer.commit") == 1,
                "回复没发出去",
            )
            unknown = [f for f in server.recorded.frames if f.get("type") not in _VOCABULARY]
            assert not unknown, f"发了协议里没有的事件：{[f.get('type') for f in unknown]}"
        finally:
            await duplex.aclose()


async def test_a_persona_pushed_before_her_first_turn_rides_a_fresh_create() -> None:
    """dev-talk connects the link before the assembly has built a context, so
    every duplex session used to be created EMPTY and handed the persona by
    session.update. Her first turn does not hear that update: live, three runs
    out of three, the first answer to 「你叫什么名字」 was 「我叫豆包，是字节
    跳动开发的AI助手」, and the first 「别插话」 was answered aloud instead of
    marked (2026-09-24). Every stream opened with her off-persona and off the
    voice contract. The first persona on an empty session therefore goes out
    on a new session.create, which her first turn does obey."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.set_context("你叫豆腐")
            await _settles(
                lambda: server.heard_instructions == "你叫豆腐", "第一轮用的还不是这份人设"
            )
            creates = server.recorded.of("session.create")
            assert len(creates) == 2, "空会话拿到第一份人设，要带着它重建一次"
            assert creates[-1]["session"]["instructions"] == "你叫豆腐"
            assert "id" not in creates[-1]["session"], "空会话没什么可接续的，重建用新会话"
            assert server.recorded.count("session.close") == 1, "旧会话要按厂商要求正式关掉"
            assert server.connections == 2
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not [
                e for e in pending if isinstance(e, link.LinkDown | link.LinkUp)
            ], "重建是适配器自己的事，不该让上层看到一次断线"
        finally:
            await duplex.aclose()


async def test_the_uplink_keeps_flowing_through_the_recreate() -> None:
    """The recreate swaps the socket under a live uplink, and the uplink must
    not notice. Found live 2026-09-24: session.close went out while the link
    still called itself started, a concurrent push_audio landed on the closing
    socket and raised, and the caller's pump died on it. From then on nothing
    reached the server between the streamer's lines, the endpoint never saw
    the silence it waits for before finishing a reply, and every turn she
    ANSWERED hung until the watchdog (the ones the gate cancels were fine,
    which is why only half the acceptance set went quiet)."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        raised: list[BaseException] = []
        stop = asyncio.Event()

        async def pump() -> None:
            while not stop.is_set():
                try:
                    await duplex.push_audio(b"\x00" * 640)
                except Exception as exc:  # the assertion below reports it
                    raised.append(exc)
                    return
                await asyncio.sleep(0)

        task = asyncio.create_task(pump())
        try:
            await asyncio.sleep(0.05)
            await duplex.set_context("你叫豆腐")
            await _settles(lambda: server.connections == 2, "没有带着人设重建")
            before = server.recorded.count("input_audio_buffer.append")
            await asyncio.sleep(0.05)
            stop.set()
            await task
            assert not raised, f"换会话时上行推帧抛了异常，推帧的一方会就此停掉：{raised!r}"
            assert server.recorded.count("input_audio_buffer.append") > before, "重建之后上行没接上"
        finally:
            stop.set()
            await duplex.aclose()


async def test_a_later_persona_goes_out_in_place() -> None:
    """Once the session carries a persona, instructions ARE heard from the next
    turn — probed by name-swap on the real endpoint — so later pushes stay a
    session.update. Recreating on every push would throw away the history the
    server holds, and dev-talk pushes every few minutes."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.set_context("你叫豆腐")
            await _settles(lambda: server.connections == 2, "第一份人设没带着重建")
            await duplex.set_context("你叫豆腐，今天播到十点")
            await _settles(
                lambda: server.instructions == "你叫豆腐，今天播到十点", "后来的人设没推到服务端"
            )
            assert (
                server.recorded.count("session.create") == 2
            ), "会话有了人设之后，改人设不该再换会话"
            assert server.recorded.count("session.update") == 1
        finally:
            await duplex.aclose()


async def test_a_persona_set_before_connect_needs_no_second_session() -> None:
    """The order that never needed the fix: the context is there before the
    socket, so the one session.create already carries it."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.set_context("你叫豆腐")
        await duplex.connect()
        try:
            assert server.heard_instructions == "你叫豆腐"
            assert server.recorded.count("session.create") == 1
            assert server.recorded.count("session.update") == 0
        finally:
            await duplex.aclose()


async def test_what_the_streamer_said_lands_as_a_finished_line() -> None:
    """`UserTranscriptDone` is the streamer's side of the conversation.

    The chat page writes its line from it (ui/events.py:199), the console
    prints 你说： from it and `on_transcript` fires from it
    (dev_talk.py:1180). Sending only `SpeechStopped` left the panel showing
    her half of every exchange and none of his — which reads as a UI bug and
    is this adapter.

    The words come from the `completed` frame's `text`. This docstring used
    to say that frame's `transcript` was empty and the words had to be rebuilt
    from the last delta: true of `transcript`, and the endpoint never sends
    one — the field is `text` (real endpoint, 2026-10-05).
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("今天打算打几把游戏", flap="？")
            done = await _next(events, link.UserTranscriptDone)
            assert done.text == "今天打算打几把游戏？", "报的是转写最后定下来的那一版"
            assert done.line_id == server.items[0]
        finally:
            await duplex.aclose()


async def test_the_final_words_come_from_the_completed_frame() -> None:
    """The completed frame's `text` is the transcriber's own final version,
    and it can differ from the last delta — punctuation settled, a number
    re-read. The last delta is only a fallback for a frame that lacks it."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("现在基本上十秒以内", final="现在基本上 10 秒以内。")
            done = await _next(events, link.UserTranscriptDone)
            assert done.text == "现在基本上 10 秒以内。"
        finally:
            await duplex.aclose()


async def test_every_part_of_a_line_names_its_line_and_carries_the_whole_so_far() -> None:
    """The chat page draws ONE row per line and refreshes it in place, so
    each delta says which line it belongs to and what the line reads now —
    the increment alone cannot express the transcriber revising itself."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        deltas: list[link.UserTranscriptDelta] = []

        async def collect() -> link.UserTranscriptDone:
            async for event in events:
                if isinstance(event, link.UserTranscriptDelta):
                    deltas.append(event)
                elif isinstance(event, link.UserTranscriptDone):
                    return event
            raise AssertionError("事件流结束了")

        try:
            await server.hear("你叫什么")
            done = await asyncio.wait_for(collect(), timeout=2.0)
            line = server.items[0]
            assert [d.whole for d in deltas] == ["你", "你叫", "你叫什", "你叫什么"]
            assert {d.line_id for d in deltas} == {line}
            assert done.line_id == line
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
        wholes: list[str] = []

        async def collect() -> None:
            async for event in events:
                if isinstance(event, link.UserTranscriptDelta):
                    # A revision with nothing new still goes up, so the row can
                    # show it; its increment is empty.
                    if event.text:
                        seen.append(event.text)
                    wholes.append(event.whole)
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
            assert wholes[-3:] == ["你叫什么？", "你叫什么", "你叫什么？"], "整行跟着转写的改口走"
        finally:
            await duplex.aclose()


async def test_a_transcript_that_never_closes_still_hands_the_floor_back() -> None:
    """A start with no end wedges the gate shut for the rest of the session.

    `SpeechStopped` is the only thing that clears `streamer_speaking`
    (floor.py:97), and on this protocol its only source is the transcriber's
    completed frame — every other provider has a VAD edge that does not depend
    on transcription. A live session on 2026-09-17 went without that frame
    and she answered nothing afterwards, every intent gated
    `gate.host_speaking`. Whether the endpoint lost it or a reply we pushed
    mid-line abandoned the item is still to be verified (2026-10-05 probes:
    only the latter was caught).
    """
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=3.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我说到一半")
            await _next(events, link.SpeechStarted)
            await clock.advance(4.0)
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
        duplex = _link(server, clock=clock, speech_edge_s=3.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear("说完了")
            await _next(events, link.SpeechStopped)
            await clock.advance(10.0)
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


async def test_a_long_sentence_keeps_pushing_the_backstop_out() -> None:
    """The backstop counts quiet since the last delta, not time since the
    start — otherwise it cuts the streamer off mid-thought.

    Timing it from the start is what made a lost ending cost a flat twenty
    seconds of her ignoring everything, three or four times per five minutes.
    """
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=3.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我")
            await _next(events, link.SpeechStarted)
            # Still talking: a delta every two seconds for ten seconds, which
            # a start-relative deadline would have cut off three times over.
            for i in range(5):
                await clock.advance(2.0)
                await server.still_hearing("我" + "说" * (i + 1))
                await asyncio.sleep(0.02)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(
                isinstance(e, link.SpeechStopped) for e in pending
            ), f"她还在说就被收尾了：{[type(e).__name__ for e in pending]}"
            # Now the ending goes missing: deltas stop and nothing closes it.
            await clock.advance(4.0)
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_talking_on_past_the_backstop_reopens_the_turn() -> None:
    """The ending was late, not lost — and he is still going.

    Without reopening, the rest of the sentence falls outside any turn: the
    floor stays open while he speaks, and the `completed` that finally does
    arrive finds the turn closed and reports nothing, so everything after the
    timeout never reaches the chat page or memory. The two halves must also
    not overlap: the transcriber's deltas are cumulative from the start of the
    utterance and it does not know we gave up.
    """
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=3.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("今天我们打")
            await _next(events, link.SpeechStarted)
            await clock.advance(4.0)
            first = await _next(events, link.UserTranscriptDone)
            assert first.text == "今天我们打"

            # He never stopped; the transcriber picks up where it was, still
            # cumulative, and then finally closes.
            await server.still_hearing("今天我们打算打几把游戏")
            # Still talking, so the turn must be open again.
            await _next(events, link.SpeechStarted)
            await server.hear_tail("今天我们打算打几把游戏。")
            second = await _next(events, link.UserTranscriptDone)
            # One line, finished twice: the second supersedes the first under
            # the same id, so the chat page rewrites its row in place and the
            # console consumer passes on only the part it had not seen.
            assert second.text == "今天我们打算打几把游戏。"
            assert second.line_id == first.line_id
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_a_reply_goes_out_on_the_greeting_event_not_the_replacement() -> None:
    """The channel that actually speaks.

    `speech_text_buffer.replacement.*` is 干预模型回复 — it intervenes in a reply
    already in flight. With the microphone open and no reply of hers to
    replace, it produced audio zero times in forty sends across eight live
    sessions (runbook).

    `speech_text_buffer.commit` is 打招呼, the vendor's mapping of the legacy
    SayHello(300), and it starts a turn outright. Measured on the real endpoint
    2026-09-21 in four conditions — no uplink at all, silent uplink, and twice
    after the streamer really spoke and she really answered — audio every time,
    tts_type `chat_tts_text`. That is the one thing the module header said did
    not exist.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.request_reply(link.ReplySpec(text="谢谢小明送的火箭"))
            await _next(events, link.ReplyDone)
            sent = [str(f.get("type")) for f in server.recorded.frames]
            assert "speech_text_buffer.commit" in sent, f"没走打招呼那条：{sent}"
            assert not [
                s for s in sent if s.startswith("speech_text_buffer.replacement")
            ], f"还在用那条实盘 40/40 不出声的通道：{sent}"
            greet = next(
                f for f in server.recorded.frames if f.get("type") == "speech_text_buffer.commit"
            )
            assert greet.get("text") == "谢谢小明送的火箭", "词要在同一帧里，没有 append 那一步"
        finally:
            await duplex.aclose()


async def test_an_unknown_event_name_errors_first_and_then_hangs_up() -> None:
    """Both halves, measured 2026-09-21 — and the repo only ever had one each.

    duplex.py's header said "Not an error frame: a 1006". volcano.py:32-35 said
    the 2026-08-27 sweep got `unknown event name` back and never mentioned a
    close. The endpoint does both: `45000000 unknown event name:<name>` and
    then the hang-up 0.0-0.1 s later, with or without uplink keep-alive,
    polled once a second for 25 s.

    Which half you keep matters. The error frame is why a client can log the
    name it got wrong instead of guessing from a bare disconnect. The close is
    why sweeping for names still costs one session per guess — that part of
    the old belief was right.
    """
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex._send({"type": "definitely_not_an_event.create", "event_id": "x"})
            for _ in range(200):
                await asyncio.sleep(0.01)
                if duplex._ws is None or getattr(duplex._ws, "close_code", None) is not None:
                    break
            errs = [f for f in server.recorded.frames if f.get("type") == "error"]
            assert not errs, "错误帧是服务端发的，不该出现在收到的上行里"
        finally:
            await duplex.aclose()


# ------------------------------------------------------------ the reply hold


async def _drain_until(
    events: AsyncIterator[link.LinkEvent], stop: type[link.LinkEvent], timeout: float = 2.0
) -> list[link.LinkEvent]:
    """Everything up to and including the first `stop`."""
    got: list[link.LinkEvent] = []

    async def pump() -> None:
        async for event in events:
            got.append(event)
            if isinstance(event, stop):
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return got


async def test_a_reply_hold_of_zero_is_todays_behaviour() -> None:
    """Shipped default. Nothing waits, nothing is buffered, the old tests above
    keep meaning what they meant."""
    async with MockDuplexServer() as server:
        duplex = _link(server)  # reply_hold_ms defaults to 0
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.request_reply(link.ReplySpec(text="谢谢"))
            got = await _drain_until(events, link.ReplyDone)
            kinds = [type(e).__name__ for e in got]
            assert kinds[0] == "ReplyStarted" and kinds[-1] == "ReplyDone"
            assert duplex._hold_handle is None
        finally:
            await duplex.aclose()


async def test_a_held_reply_arrives_all_at_once_when_the_hold_expires() -> None:
    """The timer path. With a 500 ms hold, NOTHING reaches L3 for 500 ms —
    not the start, not the words, not the audio — and then all of it does, in
    the order it arrived. ReplyStarted is held too, on purpose: the voice gate
    opens on it and gives up on a silent turn after 0.6 s, so letting the
    start through early would time the gate out before the words came."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        server.finish_ours = False
        duplex = _link(server, clock=clock, reply_hold_ms=500)
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.request_reply(link.ReplySpec(text="谢谢小明"))
            await _settles(lambda: len(duplex._held) >= 3, "帧没进 hold 缓冲")
            assert duplex._events.qsize() == 0, "hold 期间不该有任何帧上去"
            await clock.advance(0.5)
            got = await _drain_until(events, link.ReplyAudioDelta)
            kinds = [type(e).__name__ for e in got]
            assert kinds[0] == "ReplyStarted", kinds
            assert "ReplyTextDelta" in kinds and "ReplyAudioDelta" in kinds
            assert duplex._hold_handle is None, "到点之后 hold 该清掉"
        finally:
            await duplex.aclose()


async def test_a_reply_cancelled_inside_its_hold_never_makes_a_sound() -> None:
    """The case the hold exists for: 「她刚开口就被打断」. Cancel lands while
    the frames are still held. The words and audio are dropped — the audience
    heard none of it, so nothing should reach the chat page or the speaker —
    but the lifecycle survives: ReplyStarted and ReplyDone(CANCELLED) still go
    up, so the scheduler's books close the way they do for any cancel that
    landed before the first frame."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        server.finish_ours = False
        duplex = _link(server, clock=clock, reply_hold_ms=500)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await duplex.request_reply(link.ReplySpec(text="谢谢小明"))
            await _settles(lambda: len(duplex._held) >= 3, "帧没进 hold 缓冲")
            await duplex.cancel(handle)
            got = await _drain_until(events, link.ReplyDone)
            kinds = [type(e).__name__ for e in got]
            assert kinds == ["ReplyStarted", "ReplyDone"], f"内容不该上去：{kinds}"
            assert got[-1].status is link.ReplyStatus.CANCELLED  # type: ignore[union-attr]
            assert not any(isinstance(e, link.ReplyAudioDelta) for e in got)
        finally:
            await duplex.aclose()


async def test_a_reply_that_finishes_inside_its_hold_flushes_at_once() -> None:
    """A short reply that completes before the timer must not wait the timer
    out — and its done must come AFTER its words, not before them."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, reply_hold_ms=2000)  # long; must not matter
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.request_reply(link.ReplySpec(text="好"))
            got = await _drain_until(events, link.ReplyDone)  # no clock.advance at all
            kinds = [type(e).__name__ for e in got]
            assert kinds[0] == "ReplyStarted" and kinds[-1] == "ReplyDone", kinds
            assert kinds.index("ReplyAudioDelta") < kinds.index("ReplyDone")
            assert got[-1].status is link.ReplyStatus.COMPLETED  # type: ignore[union-attr]
        finally:
            await duplex.aclose()


async def test_her_own_turn_is_held_whole_too() -> None:
    """Hers are the turns the barge-in case is really about (she answers the
    streamer, the streamer talks on). Same hold, same all-at-once release."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, reply_hold_ms=300)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.say("我在呢", ours=False, finish=False)
            await _settles(lambda: len(duplex._held) >= 2, "她的帧没进 hold")
            assert duplex._events.qsize() == 0
            await clock.advance(0.3)
            started = await _next(events, link.ReplyStarted)
            assert started.handle.implicit
        finally:
            await duplex.aclose()


async def test_the_hold_can_be_changed_live_and_the_next_turn_uses_it() -> None:
    """What lets the panel promise Reload.LIVE: the value is read when a turn
    starts, not when the link is built."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, reply_hold_ms=0)
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.request_reply(link.ReplySpec(text="一"))
            await _drain_until(events, link.ReplyDone)  # immediate at 0
            duplex.set_reply_hold_ms(400)
            server.finish_ours = False
            await duplex.request_reply(link.ReplySpec(text="二"))
            await _settles(lambda: len(duplex._held) >= 3, "改成 400 之后这一轮没被扣住")
            assert duplex._events.qsize() == 0
            await clock.advance(0.4)
            await _next(events, link.ReplyStarted)
        finally:
            await duplex.aclose()


@pytest.mark.parametrize(
    ("hold_ms", "marker_at_s", "expect_decided_at_s"),
    [
        (500, 0.35, 0.50),  # hold outlasts the streaming: decided when the hold lets go
        (200, 0.35, 0.35),  # streaming outlasts the hold: decided when the marker lands
    ],
    ids=["hold-longer", "stream-longer"],
)
async def test_the_hold_and_the_gates_wait_overlap_rather_than_add(
    hold_ms: int, marker_at_s: float, expect_decided_at_s: float
) -> None:
    """Does reply_hold_ms stack on top of the voice gate's wait, or run
    alongside it? The claim under test: total ≈ max(hold, time-for-the-marker-
    to-stream), NOT hold + that time. Measured with the real gate and the real
    adapter on one fake clock, not reasoned.

    Why it should be max: the gate's "wait" is not computation, it is waiting
    for the marker's closing bracket to arrive from the server. During the
    hold the server keeps streaming into the adapter's buffer, so by the time
    the hold releases the bracket is already there, and the gate — whose feed()
    is synchronous — reads the whole burst and sets `muted` in the same tick.

    `muted` is the decision instant (turn_protocol: the decision lands on the
    closing bracket). on_skip is NOT — it waits out the note window first.
    """
    from bilisama.clock import FakeClock
    from bilisama.config.enums import VoiceReplyMode
    from bilisama.director.turn_protocol import TurnPolicy
    from bilisama.director.voice_turn import VoiceTurnGate

    clock = FakeClock()
    gate = VoiceTurnGate(
        clock, policy=TurnPolicy(), mode=VoiceReplyMode.WHEN_ADDRESSED, on_skip=lambda s: None
    )
    gate.attach(lambda e: None)

    def decided() -> bool:
        return any(t.muted for t in gate._turns.values())

    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, reply_hold_ms=hold_ms)
        await duplex.connect()
        events = duplex.events()

        async def pump() -> None:
            async for event in events:
                gate.feed(event)

        pumping = asyncio.create_task(pump())
        try:
            ws = server._ws
            assert ws is not None
            t0 = clock.monotonic()
            # t=0: first half of the marker. Books her turn; the hold begins.
            await server._send(ws, {"type": "response.output_text.delta", "delta": "[SK"})
            await _settles(lambda: duplex._active is not None, "她的轮次没登记")
            assert not decided()

            # t=marker_at: the closing bracket streams in. Without any hold the
            # gate would decide right here.
            await clock.advance(marker_at_s)
            await server._send(ws, {"type": "response.output_text.delta", "delta": "IP] 在念弹幕"})
            await asyncio.sleep(0.05)  # let the socket deliver and the pump run
            if hold_ms / 1000 > marker_at_s:
                assert not decided(), "hold 还没到点，门不该看见任何帧"

            # advance to exactly the expected decision instant and check
            remaining = expect_decided_at_s - (clock.monotonic() - t0)
            if remaining > 0:
                await clock.advance(remaining)
            await _settles(decided, f"到 {expect_decided_at_s}s 了门还没判")
            decided_at = clock.monotonic() - t0
            assert abs(decided_at - expect_decided_at_s) < 0.02, (
                f"判在 {decided_at:.2f}s，期望 {expect_decided_at_s}s"
                f"（相加会是 {hold_ms / 1000 + marker_at_s:.2f}s）"
            )
        finally:
            pumping.cancel()
            await duplex.aclose()


# ------------------------------------------------------------ one line, one row


async def _dones_until_quiet(
    events: AsyncIterator[link.LinkEvent], *, quiet_s: float = 0.3
) -> list[link.UserTranscriptDone]:
    """Every UserTranscriptDone until the stream has been quiet for quiet_s."""
    found: list[link.UserTranscriptDone] = []

    async def pump() -> None:
        async for event in events:
            if isinstance(event, link.UserTranscriptDone):
                found.append(event)

    task = asyncio.create_task(pump())
    try:
        last = -1
        while last != len(found):
            last = len(found)
            await asyncio.sleep(quiet_s)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return found


async def test_a_line_restarted_after_our_reply_stays_one_line() -> None:
    """Measured on the real endpoint 2026-10-05: a reply of ours starting
    while the streamer is mid-line makes the endpoint abandon that item —
    no completed, ever — and re-transcribe the line from its first word under
    a NEW item. Read as two lines, the chat page showed the opening, then the
    whole thing again; read by item, nothing finishes the first line at all."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("大家好今天我们聊")
            await _next(events, link.SpeechStarted)
            await duplex.request_reply(link.ReplySpec(text="都这个点了还不睡"))
            await _settles(lambda: len(server.items) == 2, "假服务端该换新编号重新转写")
            await server.still_hearing("大家好今天我们聊新版本")
            await server.hear_tail("大家好，今天我们聊新版本。")
            dones = await _dones_until_quiet(events)
            assert [(d.text, d.line_id) for d in dones] == [
                ("大家好，今天我们聊新版本。", server.items[0])
            ], f"应当是一行、定稿一次：{dones}"
        finally:
            await duplex.aclose()


async def test_a_different_line_after_an_unfinished_one_gets_its_own_row() -> None:
    """The guard on that rule: a new item that does NOT start the way the
    unfinished line did is a new line. The old one is finished with what was
    heard, rather than overwritten by someone else's words."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我说到一半")
            await server.hear("换个话题聊聊")
            dones = await _dones_until_quiet(events)
            assert [(d.text, d.line_id) for d in dones] == [
                ("我说到一半", server.items[0]),
                ("换个话题聊聊", server.items[1]),
            ]
        finally:
            await duplex.aclose()


async def test_sound_on_the_uplink_holds_the_backstop_open() -> None:
    """Inside one line the transcriber goes over a second without a delta
    while he is still talking (real endpoint, 2026-10-05: up to 1.38 s in a
    recorded streamer, up to 1.0 s in synthetic monologues). Timed on
    deltas alone, the backstop split his line and handed the floor to a
    proactive topic mid-sentence. A frame the noise
    gate let through is him still making sound, so it restarts the same
    countdown a delta does — one setting, no second ceiling."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=2.0)
        await duplex.connect()
        events = duplex.events()
        voiced = b"\x10\x00" * 320
        silent = bytes(640)
        try:
            await server.hear_without_ending("我")
            await _next(events, link.SpeechStarted)
            for _ in range(6):  # six seconds with no delta, but sound throughout
                await duplex.push_audio(voiced)
                await clock.advance(1.0)
                await asyncio.sleep(0.01)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(
                isinstance(e, link.SpeechStopped) for e in pending
            ), f"他还在出声就被收尾了：{[type(e).__name__ for e in pending]}"
            for _ in range(3):  # now quiet on both counts
                await duplex.push_audio(silent)
                await clock.advance(1.0)
                await asyncio.sleep(0.01)
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


# ------------------------------------------------------------ a session that ends mid-line


async def _heard(events: AsyncIterator[link.LinkEvent], whole: str) -> link.UserTranscriptDelta:
    """Wait until the line reads `whole`; the fake streams it a word at a time."""
    delta = await _next(events, link.UserTranscriptDelta)
    while delta.whole != whole:
        delta = await _next(events, link.UserTranscriptDelta)
    return delta


async def test_pausing_mid_line_finishes_the_line_and_hands_the_floor_back() -> None:
    """The panel's 暂停 hangs up the session, and the streamer may be mid-line
    when he presses it (「我先暂停一下」, then the button). The completed frame
    that would have closed his line belonged to the session that just ended,
    so it never came: the floor held `streamer_speaking` through the pause and
    after 恢复, and every intent waited on gate.host_speaking until he finished
    a whole new line — forever, if he stayed quiet. A session that ends ends
    his line with it."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我先暂停一下")
            await _next(events, link.SpeechStarted)
            await _heard(events, "我先暂停一下")
            await duplex.suspend()
            done = await _next(events, link.UserTranscriptDone)
            assert done.text == "我先暂停一下", "暂停掐断的半句也要作为说完的一句报上去"
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_after_resume_his_next_line_opens_a_turn_and_a_row_of_its_own() -> None:
    """The other half of the same wedge. The adapter still believed the old
    line open, so after 恢复 his next line raised no SpeechStarted (no barge-in)
    and armed no backstop, and one that began with the same words was folded
    into the row the pause had cut off."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, speech_edge_s=1.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我们接着看这一关")
            await _next(events, link.SpeechStarted)
            before = await _heard(events, "我们接着看这一关")
            await duplex.suspend()
            await duplex.resume()
            await server.hear_without_ending("我们接着看这一关的boss")
            await _next(events, link.SpeechStarted)
            after = await _heard(events, "我们接着看这一关的boss")
            assert after.line_id != before.line_id, "恢复后的一句被并进了暂停前那一行"
            await clock.advance(1.5)
            await _next(events, link.SpeechStopped)
        finally:
            await duplex.aclose()


async def test_a_dropped_socket_ends_his_line_before_it_reports_the_drop() -> None:
    """The same rule when the network ends the session: his line is finished
    and the floor handed back first, then the drop is reported. Before, the
    floor waited on the backstop, which then logged duplex.speech_edge_missing
    and blamed the endpoint for a frame that could no longer come."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("网络好像卡了")
            await _next(events, link.SpeechStarted)
            await _heard(events, "网络好像卡了")
            await server.drop()
            got = [type(e).__name__ for e in await _drain_until(events, link.LinkDown)]
            assert got[-3:] == ["UserTranscriptDone", "SpeechStopped", "LinkDown"], got
        finally:
            await duplex.aclose()


@pytest.mark.parametrize("code", [1000, 1001])
async def test_a_polite_close_from_the_server_is_still_a_drop(code: int) -> None:
    """A normal close (1000, or 1001 going away) ends websockets' iteration
    instead of raising, and the receive loop used to fall out of it quietly:
    no LinkDown, no reconnect, `_started` still true. His open line waited on
    the backstop, which blamed the endpoint, and after that the link was dead
    while every health signal said it was fine — replies failed and nothing
    he said was transcribed until someone pressed 暂停 and 恢复 (plan #117)."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.hear_without_ending("我说到一半")
            await _next(events, link.SpeechStarted)
            await _heard(events, "我说到一半")
            await server.drop(code=code)
            got = [type(e).__name__ for e in await _drain_until(events, link.LinkDown)]
            assert got[-3:] == ["UserTranscriptDone", "SpeechStopped", "LinkDown"], got
            await clock.advance(1.5)
            await _next(events, link.LinkUp)
            assert server.connections == 2, "正常关闭之后没有重连"
        finally:
            await duplex.aclose()


async def test_resuming_after_a_drop_says_the_link_is_back() -> None:
    """After a drop, 暂停 during the reconnect backoff cancels the ladder, and
    恢复 reconnects without saying so. The scheduler keeps `_link_down` until a
    LinkUp arrives, so every danmaku and topic after that was skipped as
    link.down for the rest of the session (plan #119). A resume that brings
    back a link we reported down says it is back — once."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock, reconnect_backoff_s=3.0)
        await duplex.connect()
        events = duplex.events()
        try:
            await server.drop()
            await _next(events, link.LinkDown)
            await duplex.suspend()
            await duplex.resume()
            await _next(events, link.LinkUp)
            await clock.advance(10.0)
            await asyncio.sleep(0.05)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(isinstance(e, link.LinkUp) for e in pending), pending
        finally:
            await duplex.aclose()


async def test_a_plain_pause_and_resume_does_not_announce_a_recovery() -> None:
    """The other side of the same rule: LinkUp means the link came BACK. A
    pause with no drop before it must not print 「已恢复」 into the panel."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        try:
            await duplex.suspend()
            await duplex.resume()
            await asyncio.sleep(0.1)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(isinstance(e, link.LinkUp) for e in pending), pending
        finally:
            await duplex.aclose()


# ------------------------------------------------------------ a turn nobody heard


async def _her_turn(
    server: MockDuplexServer, events: AsyncIterator[link.LinkEvent], text: str = "[SKIP] 在聊天气"
) -> link.ReplyHandle:
    """Her own turn, left streaming so it can be stopped mid-way."""
    await server.say(text, ours=False, finish=False)
    started = await _next(events, link.ReplyStarted)
    assert started.handle.implicit
    return started.handle


async def test_a_skipped_turn_nobody_heard_runs_out_without_a_cancel() -> None:
    """The voice gate held every frame of her turn and ruled it not addressed
    to her. On this protocol a response.cancel scrambles the streamer's next
    transcription — items that never get a word, lines cut after 0.2-0.3 s,
    words lost at the seams — while letting her short [SKIP] line finish on
    the server kept every line whole (real endpoint, 2026-10-05). So the turn
    goes stale at once, nothing is sent, and it settles CANCELLED on the
    endpoint's own done; the slot is free again right after."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            assert handle.stale
            await server.more("改动")
            await server.end_reply()
            got = await _drain_until(events, link.ReplyDone)
            done = got[-1]
            assert isinstance(done, link.ReplyDone)
            assert done.handle is handle and done.status is link.ReplyStatus.CANCELLED
            late = [e.text for e in got if isinstance(e, link.ReplyTextDelta)]
            assert "改" not in late and "动" not in late, f"拦下之后的字不该再往上送：{late}"
            assert server.cancels == 0, "没人听见的那轮不该发取消"
            await asyncio.wait_for(duplex.request_reply(link.ReplySpec(text="下一条")), timeout=1.0)
        finally:
            await duplex.aclose()


async def test_stopping_her_turn_that_may_have_been_heard_still_cancels() -> None:
    """Sound may have left (a flush, the output guard, panic): on duplex the
    server does not stop by itself, so this one is still a real cancel."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle)
            done = await _next(events, link.ReplyDone)
            assert done.handle is handle and done.status is link.ReplyStatus.CANCELLED
            await _settles(lambda: server.cancels == 1, "可能被听见的那轮必须真的取消")
        finally:
            await duplex.aclose()


async def test_a_reply_of_ours_is_always_really_cancelled() -> None:
    """Only her own turns can run out; ours stop on the wire even if L3 says
    nobody heard them."""
    async with MockDuplexServer() as server:
        server.finish_ours = False
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await duplex.request_reply(link.ReplySpec(text="谢谢小明送的火箭"))
            started = await _next(events, link.ReplyStarted)
            assert started.handle is handle
            await duplex.cancel(handle, unheard=True)
            await _next(events, link.ReplyDone)
            await _settles(lambda: server.cancels == 1, "我们点的回复必须真的取消")
        finally:
            await duplex.aclose()


async def test_the_switch_puts_back_the_cancel_and_takes_effect_on_the_next_kill() -> None:
    """[speech.volcano_duplex] skip_without_cancel = false is today's
    behaviour, and the panel can flip it live: the next kill uses it."""
    async with MockDuplexServer() as server:
        duplex = _link(server, skip_without_cancel=False)
        await duplex.connect()
        events = duplex.events()
        try:
            first = await _her_turn(server, events)
            await duplex.cancel(first, unheard=True)
            await _next(events, link.ReplyDone)
            await _settles(lambda: server.cancels == 1, "开关关着就照旧取消")
            duplex.set_skip_without_cancel(True)
            second = await _her_turn(server, events, "[SKIP] 在调窗口")
            await duplex.cancel(second, unheard=True)
            await server.end_reply()
            done = await _next(events, link.ReplyDone)
            assert done.handle is second and done.status is link.ReplyStatus.CANCELLED
            assert server.cancels == 1, "开关打开后的那一次不该再发取消"
        finally:
            await duplex.aclose()


async def test_a_turn_that_never_finishes_is_cancelled_after_a_bounded_wait() -> None:
    """If the endpoint never sends the done, the slot cannot wait for the
    25-second watchdog: after run_out_s the turn is cancelled for real."""
    from bilisama.clock import FakeClock

    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = _link(server, clock=clock)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            await clock.advance(4.0)
            await asyncio.sleep(0.05)
            assert server.cancels == 0, "还没到时限就取消了"
            await clock.advance(2.0)
            done = await _next(events, link.ReplyDone)
            assert done.handle is handle and done.status is link.ReplyStatus.CANCELLED
            await _settles(lambda: server.cancels == 1, "超时以后要真的取消")
        finally:
            await duplex.aclose()


async def test_a_heard_stop_after_a_quiet_one_cancels_exactly_once() -> None:
    """A quiet stop, then one that may have been heard (panic, say): the
    second escalates to a real cancel, and a third adds nothing."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            await duplex.cancel(handle)
            await duplex.cancel(handle)
            got = await _drain_until(events, link.ReplyDone)
            assert sum(isinstance(e, link.ReplyDone) for e in got) == 1
            await asyncio.sleep(0.05)
            assert server.cancels == 1
        finally:
            await duplex.aclose()


async def test_a_reply_asked_for_while_her_turn_runs_out_waits_for_it() -> None:
    """The slot is hers until the endpoint is done with her turn; a commit
    sent into it would land on a server still busy with the [SKIP] line."""
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            pending = asyncio.create_task(duplex.request_reply(link.ReplySpec(text="下一条")))
            await asyncio.sleep(0.1)
            assert not pending.done(), "她那轮还没结束，下一条就抢了名额"
            assert server.recorded.count("speech_text_buffer.commit") == 0
            await server.end_reply()
            await asyncio.wait_for(pending, timeout=1.0)
            await _settles(
                lambda: server.recorded.count("speech_text_buffer.commit") == 1,
                "她那轮结束以后下一条才发出去",
            )
        finally:
            await duplex.aclose()


async def test_pausing_while_her_turn_runs_out_settles_it_without_a_cancel() -> None:
    async with MockDuplexServer() as server:
        duplex = _link(server)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            await duplex.suspend()
            done = await _next(events, link.ReplyDone)
            assert done.handle is handle and done.status is link.ReplyStatus.CANCELLED
            assert server.cancels == 0
        finally:
            await duplex.aclose()


@pytest.mark.parametrize("skip_without_cancel", [False, True])
async def test_only_the_cancel_scrambles_his_next_line(skip_without_cancel: bool) -> None:
    """The fake's `scramble_after_cancel` replays what the real endpoint did
    after a cancel on a real streamer recording (2026-10-05, 8 of 8): an item
    opens at the done and never gets a word — and the floor reads it as him
    talking. Letting the turn run out sends no cancel, so nothing opens."""
    async with MockDuplexServer() as server:
        server.scramble_after_cancel = True
        duplex = _link(server, skip_without_cancel=skip_without_cancel)
        await duplex.connect()
        events = duplex.events()
        try:
            handle = await _her_turn(server, events)
            await duplex.cancel(handle, unheard=True)
            if skip_without_cancel:
                await server.end_reply()
            await _drain_until(events, link.ReplyDone)
            await asyncio.sleep(0.1)
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            opened = any(isinstance(e, link.SpeechStarted) for e in pending)
            assert opened is not skip_without_cancel, pending
        finally:
            await duplex.aclose()
