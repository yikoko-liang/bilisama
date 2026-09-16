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
        await duplex.connect()
        try:
            assert second.context == [
                {"role": "user", "text": "[弹幕] 观众A: 主播今天播到几点"}
            ], "建会话时它必须走 dialog_context"
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
