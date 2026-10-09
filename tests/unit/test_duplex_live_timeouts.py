"""Changing duplex silence deadlines during an active input segment."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from bilisama.clock import FakeClock
from bilisama.config.schema import VolcanoDuplexConfig
from bilisama.realtime import link
from bilisama.realtime.providers.duplex import DuplexLink
from tests.fakes.mock_duplex import MockDuplexServer


@asynccontextmanager
async def _session(
    *, edge_s: float = 1.0, commit_s: float = 0.0
) -> AsyncIterator[tuple[DuplexLink, MockDuplexServer, FakeClock]]:
    clock = FakeClock()
    async with MockDuplexServer() as server:
        duplex = DuplexLink(
            server.url,
            api_key="k",
            config=VolcanoDuplexConfig(),
            clock=clock,
            speech_edge_s=edge_s,
            speech_commit_s=commit_s,
        )
        await duplex.connect()
        try:
            yield duplex, server, clock
        finally:
            await duplex.aclose()


async def _settles(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(2.0):
        while not check():
            await asyncio.sleep(0.001)


async def _audio(
    duplex: DuplexLink, server: MockDuplexServer, pcm: bytes = b"\x10\x00" * 320
) -> None:
    """Wait for receipt so a later silence frame can also serve as a send barrier."""
    expected = server.recorded.count("input_audio_buffer.append") + 1
    await duplex.push_audio(pcm)
    await _settles(lambda: server.recorded.count("input_audio_buffer.append") == expected)


def _pending(duplex: DuplexLink) -> list[link.LinkEvent]:
    return [duplex._events.get_nowait() for _ in range(duplex._events.qsize())]


async def _next[E: link.LinkEvent](duplex: DuplexLink, wanted: type[E]) -> E:
    async with asyncio.timeout(2.0):
        while True:
            event = await duplex._events.get()
            if isinstance(event, wanted):
                return event


async def test_shortening_edge_uses_the_last_sound_instead_of_the_edit_time() -> None:
    async with _session(edge_s=4.0) as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(2.0)
        assert not _pending(duplex)
        duplex.set_speech_timeouts(edge_s=1.0, commit_s=0.0)
        await clock.advance(0)
        assert [type(e) for e in _pending(duplex)] == [link.SpeechStopped]
        await clock.advance(5.0)
        assert not _pending(duplex)


async def test_extending_edge_replaces_the_deadline_already_waiting() -> None:
    async with _session() as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(0.75)
        duplex.set_speech_timeouts(edge_s=2.0, commit_s=0.0)
        await clock.advance(0.5)
        assert not _pending(duplex), "The old one-second deadline still closed speech."
        await clock.advance(0.76)
        assert [type(e) for e in _pending(duplex)] == [link.SpeechStopped]


async def test_new_sound_still_resets_the_deadline_after_a_live_edit() -> None:
    async with _session() as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(0.25)
        duplex.set_speech_timeouts(edge_s=0.5, commit_s=0.0)
        await _audio(duplex, server)
        for _ in range(4):
            await clock.advance(0.4)
            await _audio(duplex, server)
            assert not _pending(
                duplex
            ), "A live edit stopped subsequent sound from resetting silence."
        await clock.advance(0.49)
        assert not _pending(duplex)
        await clock.advance(0.02)
        assert [type(e) for e in _pending(duplex)] == [link.SpeechStopped]


@pytest.mark.parametrize("quiet_s", [1.2, 4.0], ids=["before-deadline", "already-overdue"])
async def test_enabling_commit_after_local_stop_keeps_the_original_sound_time(
    quiet_s: float,
) -> None:
    async with _session() as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(quiet_s)
        assert [type(e) for e in _pending(duplex)] == [link.SpeechStopped]
        duplex.set_speech_timeouts(edge_s=1.0, commit_s=3.0)
        if quiet_s < 3.0:
            await clock.advance(3.0 - quiet_s - 0.01)
            assert server.recorded.count("input_audio_buffer.commit") == 0
            await clock.advance(0.02)
        else:
            await clock.advance(0)
        await _settles(lambda: server.recorded.count("input_audio_buffer.commit") == 1)
        assert not _pending(duplex), "Enabling commit reopened speech or repeated the local stop."


@pytest.mark.parametrize(
    ("old_commit_s", "edit_at_s", "new_commit_s"),
    [(5.0, 2.5, 2.0), (3.0, 2.0, 5.0)],
    ids=["shorten", "extend"],
)
async def test_changing_commit_recomputes_the_current_pending_deadline(
    old_commit_s: float, edit_at_s: float, new_commit_s: float
) -> None:
    async with _session(commit_s=old_commit_s) as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(edit_at_s)
        _pending(duplex)
        duplex.set_speech_timeouts(edge_s=1.0, commit_s=new_commit_s)
        if edit_at_s < new_commit_s:
            await clock.advance(new_commit_s - edit_at_s - 0.01)
            await _audio(duplex, server, bytes(640))
            assert server.recorded.count("input_audio_buffer.commit") == 0
            await clock.advance(0.02)
        else:
            await clock.advance(0)
        await _settles(lambda: server.recorded.count("input_audio_buffer.commit") == 1)
        assert not _pending(duplex)


async def test_disabling_commit_revokes_a_commit_waiting_for_the_send_lock() -> None:
    async with _session(commit_s=3.0) as (duplex, server, clock):
        await _audio(duplex, server)
        await duplex._send_lock.acquire()
        try:
            await clock.advance(3.01)
            assert server.recorded.count("input_audio_buffer.commit") == 0
            duplex.set_speech_timeouts(edge_s=1.0, commit_s=0.0)
            await clock.advance(0)
        finally:
            duplex._send_lock.release()
        await _audio(duplex, server, bytes(640))
        assert server.recorded.count("input_audio_buffer.commit") == 0
        await clock.advance(10.0)
        await _audio(duplex, server, bytes(640))
        assert server.recorded.count("input_audio_buffer.commit") == 0


async def test_changing_timeouts_cannot_commit_the_same_segment_twice() -> None:
    async with _session(commit_s=3.0) as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(3.01)
        await _settles(lambda: server.recorded.count("input_audio_buffer.commit") == 1)
        _pending(duplex)
        duplex.set_speech_timeouts(edge_s=1.0, commit_s=0.0)
        duplex.set_speech_timeouts(edge_s=0.5, commit_s=2.0)
        await clock.advance(10.0)
        await _audio(duplex, server, bytes(640))
        assert server.recorded.count("input_audio_buffer.commit") == 1
        assert not _pending(duplex)


async def test_extending_edge_after_local_stop_does_not_reopen_speech() -> None:
    async with _session() as (duplex, server, clock):
        await _audio(duplex, server)
        await clock.advance(1.01)
        assert [type(e) for e in _pending(duplex)] == [link.SpeechStopped]
        duplex.set_speech_timeouts(edge_s=5.0, commit_s=0.0)
        await server.hear_without_ending("这句是之前说的")
        await _settles(lambda: duplex._asr_text == "这句是之前说的")
        pending = _pending(duplex)
        assert pending and all(isinstance(e, link.UserTranscriptDelta) for e in pending)
        await clock.advance(10.0)
        assert not _pending(duplex)


@pytest.mark.parametrize("state", ["no-audio", "completed", "session-ended"])
async def test_live_timeout_edits_cannot_invent_an_unfinished_audio_segment(state: str) -> None:
    async with _session() as (duplex, server, clock):
        if state != "no-audio":
            await _audio(duplex, server)
        if state == "completed":
            await server.hear("这句已经说完了")
            await _next(duplex, link.UserTranscriptDone)
        elif state == "session-ended":
            await duplex.suspend()
        _pending(duplex)
        await clock.advance(5.0)
        duplex.set_speech_timeouts(edge_s=0.5, commit_s=1.0)
        await clock.advance(5.0)
        if state != "session-ended":
            await _audio(duplex, server, bytes(640))
        assert server.recorded.count("input_audio_buffer.commit") == 0
        assert not _pending(duplex)
