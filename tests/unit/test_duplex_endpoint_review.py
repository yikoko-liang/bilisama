"""Regression cases for endpoint ownership across local silence and item replacement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from bilisama.clock import FakeClock
from bilisama.config.schema import VolcanoDuplexConfig
from bilisama.realtime import link
from bilisama.realtime.providers.duplex import DuplexLink
from tests.fakes.mock_duplex import MockDuplexServer


async def _through(
    events: AsyncIterator[link.LinkEvent], accepted: Callable[[link.LinkEvent], bool]
) -> list[link.LinkEvent]:
    async def collect() -> list[link.LinkEvent]:
        seen: list[link.LinkEvent] = []
        async for event in events:
            seen.append(event)
            if accepted(event):
                return seen
        raise AssertionError("事件流提前结束")

    return await asyncio.wait_for(collect(), timeout=2.0)


async def _heard(events: AsyncIterator[link.LinkEvent], text: str) -> link.UserTranscriptDelta:
    seen = await _through(
        events, lambda event: isinstance(event, link.UserTranscriptDelta) and event.whole == text
    )
    last = seen[-1]
    assert isinstance(last, link.UserTranscriptDelta)
    return last


async def test_same_item_continued_after_local_timeout_completes_without_forced_commit() -> None:
    """A native final after genuine continuation also retires the new silence deadline."""
    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = DuplexLink(
            server.url,
            api_key="k",
            config=VolcanoDuplexConfig(),
            clock=clock,
            speech_edge_s=1.0,
            speech_commit_s=3.0,
        )
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.push_audio(b"\x10\x00" * 320)
            await server.hear_without_ending("我先想一下")
            await _heard(events, "我先想一下")
            await clock.advance(1.01)
            await _through(events, lambda event: isinstance(event, link.SpeechStopped))
            await duplex.push_audio(b"\x20\x00" * 320)
            await server.still_hearing("我先想一下现在想好了")
            await _heard(events, "我先想一下现在想好了")
            await server.hear_tail("我先想一下现在想好了。")
            await _through(events, lambda event: isinstance(event, link.UserTranscriptDone))
            await clock.advance(4.0)
            await duplex.push_audio(bytes(640))
            async with asyncio.timeout(2.0):
                while server.recorded.count("input_audio_buffer.append") != 3:
                    await asyncio.sleep(0.01)
            assert (
                server.recorded.count("input_audio_buffer.commit") == 0
            ), "同一 item 续讲后的自然 completed 未取消强制提交"
        finally:
            await duplex.aclose()


async def test_replaced_item_cannot_overwrite_its_successor_after_a_later_line_starts() -> None:
    """A replaced item's loss of row ownership survives a switch to another line."""
    async with MockDuplexServer() as server:
        duplex = DuplexLink(server.url, api_key="k", config=VolcanoDuplexConfig())
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.push_audio(b"\x10\x00" * 320)
            await server.hear_without_ending("大家好今天我们聊")
            first = await _heard(events, "大家好今天我们聊")
            await server.hear_without_ending("大家好今天我们聊新版本")
            second = await _heard(events, "大家好今天我们聊新版本")
            assert second.line_id == first.line_id
            await server.hear_without_ending("另外说一件事情")
            third = await _heard(events, "另外说一件事情")
            assert third.line_id != first.line_id
            ws = server._ws
            assert ws is not None
            await server._send(ws, server._completed(server.items[0], "大家好今天我们聊。"))
            await server.still_hearing("另外说一件事情继续")
            seen = await _through(
                events,
                lambda event: isinstance(event, link.UserTranscriptDelta)
                and event.whole == "另外说一件事情继续",
            )
            assert not any(
                isinstance(event, link.UserTranscriptDelta | link.UserTranscriptDone)
                and event.line_id == first.line_id
                for event in seen
            ), "切到下一行后，被替换的旧 item 又覆盖了前一行"
        finally:
            await duplex.aclose()


async def test_old_punctuation_during_new_audio_cannot_end_the_new_segment() -> None:
    """A punctuation-only revision supplies no evidence that an item covers new audio."""
    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = DuplexLink(
            server.url,
            api_key="k",
            config=VolcanoDuplexConfig(),
            clock=clock,
            speech_edge_s=1.0,
            speech_commit_s=3.0,
        )
        await duplex.connect()
        events = duplex.events()
        try:
            await duplex.push_audio(b"\x10\x00" * 320)
            await server.hear_without_ending("前一句话")
            await _heard(events, "前一句话")
            await clock.advance(1.01)
            await _through(events, lambda event: isinstance(event, link.SpeechStopped))
            await duplex.push_audio(b"\x20\x00" * 320)
            await server.still_hearing("前一句话。")
            await _heard(events, "前一句话。")
            await server.hear_tail("前一句话。")
            await _through(events, lambda event: isinstance(event, link.UserTranscriptDone))
            pending = [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]
            assert not any(
                isinstance(event, link.SpeechStopped) for event in pending
            ), "旧句子的标点修订把 completed 归给新一段声音，提前结束了本地等待"
            await clock.advance(3.01)
            await duplex.push_audio(bytes(640))
            async with asyncio.timeout(2.0):
                while server.recorded.count("input_audio_buffer.append") != 3:
                    await asyncio.sleep(0.01)
            assert (
                server.recorded.count("input_audio_buffer.commit") == 1
            ), "旧句子的 completed 取消了新一段声音应有的提交计时"
        finally:
            await duplex.aclose()
