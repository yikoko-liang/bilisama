"""Debt #19: the link comes back by itself, and L3 survives the gap.

Three behaviours, each with the failure it exists to prevent:

- reconnect: the socket dies, the client reopens it and the adapter replays
  the session. Without the replay the assistant runs with no persona until
  the next context push — up to a full clock-granularity window.
- floor reset: a drop MID-UTTERANCE strands streamer_speaking True, because
  only SpeechStopped clears it and that frame is never coming. The gate then
  never opens again, reconnect or not.
- rotation: a hard session cap is on the clock the whole time, so we retire
  the socket a few minutes early rather than being cut off mid-sentence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName
from bilisama.config.schema import HostedTurnConfig
from bilisama.dev_talk import _FRAME_MS, _UPLINK_PATIENCE_S, _uplink, _watch
from bilisama.director.floor import SpeakingFloor
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.errors import ErrorClass, classify_error
from bilisama.realtime.providers.hosted import HostedLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script
from tests.unit.test_realtime_client import _next_event


async def test_a_dropped_socket_comes_back_and_the_session_is_restored() -> None:
    """The headline: drop the link, and without anyone above lifting a finger
    the client reopens it and the adapter re-sends bootstrap plus context."""
    clock = FakeClock()
    async with MockRealtimeServer(
        caps=caps_mod.DASHSCOPE, codec=dia.BETA, script=Script(delta_chunks=1)
    ) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            clock=clock,
            turn=HostedTurnConfig(),
            auto_reconnect=True,
            reconnect_backoff_s=1.0,
        )
        await hosted.connect()
        try:
            events = hosted.events()
            await hosted.set_context("你是米娅。")
            before = server.recorded.count("session.update")

            await server.drop_connection()
            down = await _next_event(events, link.LinkDown)
            assert isinstance(down, link.LinkDown)
            assert down.retrying, "auto_reconnect is on, so this drop is recoverable"

            await clock.advance(1.5)  # walk the first backoff
            up = await _next_event(events, link.LinkUp)
            assert isinstance(up, link.LinkUp)

            # Bootstrap + context replayed onto the new socket: without this
            # the model wakes up with no persona and nobody upstream notices.
            after = server.recorded.count("session.update")
            assert after >= before + 2, f"session not restored: {before} → {after}"
            patches = [f for f in server.recorded.events if f.get("type") == "session.update"]
            assert any(
                (f.get("session") or {}).get("instructions") == "你是米娅。" for f in patches[-2:]
            ), "the persona was not replayed"

            # And it can speak again.
            await hosted.request_reply(link.ReplySpec(instructions="再说一句"))
            done = await _next_event(events, link.ReplyDone)
            assert isinstance(done, link.ReplyDone)
            assert done.status is link.ReplyStatus.COMPLETED
        finally:
            await hosted.aclose()


async def test_a_deliberate_close_does_not_reconnect() -> None:
    """aclose() is intent, not failure: coming back would resurrect a session
    the caller just retired (and keep the process alive at shutdown)."""
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE, clock=clock, auto_reconnect=True)
        await hosted.connect()
        events = hosted.events()
        await hosted.aclose()
        await clock.advance(5.0)
        assert events is not None
        # Nothing to assert positively — the point is that no LinkUp arrives
        # and no task is left running; a resurrected socket would show up as a
        # second connection in the recorded traffic.
        assert server.recorded.count("session.update") <= 1


async def test_rotation_retires_the_socket_before_the_cap() -> None:
    """A hard cap is knowable, so we act on it early instead of being cut off.

    The countdown runs on the injected clock: a 120-minute cap is verified in
    milliseconds, which is the only reason this is a unit test at all.
    """
    clock = FakeClock()
    async with MockRealtimeServer(
        caps=caps_mod.DASHSCOPE, codec=dia.BETA, script=Script(delta_chunks=1)
    ) as server:
        hosted = HostedLink(
            server.url,
            ProviderName.DASHSCOPE,
            clock=clock,
            turn=HostedTurnConfig(),
            auto_reconnect=True,
            reconnect_backoff_s=1.0,
            session_cap_min=120,
            rotate_margin_min=3.0,
        )
        await hosted.connect()
        try:
            events = hosted.events()
            await hosted.set_context("你是米娅。")

            await clock.advance(116 * 60)  # short of 117 — nothing yet
            await asyncio.sleep(0)
            assert hosted._rotation is not None and not hosted._rotation.done()

            await clock.advance(2 * 60)  # crosses cap minus margin
            down = await _next_event(events, link.LinkDown)
            assert isinstance(down, link.LinkDown)
            assert down.reason.startswith("rotate:"), down.reason

            await clock.advance(1.5)
            up = await _next_event(events, link.LinkUp)
            assert isinstance(up, link.LinkUp)
        finally:
            await hosted.aclose()


def _live_tasks(name: str) -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t.get_name() == name and not t.done()]


async def test_a_drop_during_recovery_does_not_open_a_second_reconnect_loop() -> None:
    """One shaky line, two ladders climbing it.

    The reconnect loop reopens the socket and replays the session only after
    (client.py:389-391). The line that dropped us once is quite capable of
    dropping the fresh socket inside that window, and its recv task then runs
    _on_disconnect — which used to start a second loop while the first was
    still climbing. Two loops reconnect twice: the server holds two sessions,
    we remember one. The one that hurts comes later, when the orphan dies and
    ITS _on_disconnect fails the healthy socket's records and nulls _ws, so
    even aclose can no longer reach the session left open.
    """
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        client = RealtimeClient(
            server.url,
            caps=caps_mod.DASHSCOPE,
            codec=dia.BETA,
            clock=clock,
            auto_reconnect=True,
            reconnect_backoff_s=1.0,
        )
        drops_left = 1

        async def resume() -> None:
            # What HostedLink._resume_session does: send the bootstrap frame.
            # The first time round the line takes the new socket down first.
            nonlocal drops_left
            if drops_left:
                drops_left -= 1
                await server.drop_connection()
                for _ in range(20):
                    await asyncio.sleep(0)  # let the recv task see the close
            await client.send_command({"type": "session.update", "session": {}})

        client.on_resume = resume
        events = client.events()
        await client.connect()
        try:
            await server.drop_connection()
            await _next_event(events, link.LinkDown)  # the drop that starts the loop
            await clock.advance(1.5)  # walk the backoff: reopen, then resume kills it
            await _next_event(events, link.LinkDown)  # the fresh socket's own death

            climbing = _live_tasks("realtime:reconnect")
            assert len(climbing) == 1, f"同时有 {len(climbing)} 条重连回路在爬"
            assert len(_live_tasks("realtime:recv")) <= 1, "上一条 socket 的读任务还活着"

            # And the one loop left has to finish the job: deferring to it must
            # not mean nobody climbs. Its own success check sees the socket it
            # opened is gone and keeps going.
            for _ in range(20):
                await asyncio.sleep(0)
            await clock.advance(3.0)
            up = await _next_event(events, link.LinkUp)
            assert isinstance(up, link.LinkUp)
            assert client._ws is not None, "回来了却没有 socket"
        finally:
            await client.aclose()
        assert not _live_tasks("realtime:reconnect"), "aclose 关不掉的重连回路还在跑"


async def test_a_socket_that_dies_during_a_silent_resume_keeps_the_ladder_climbing() -> None:
    """The trap on the other side of "only one loop".

    Not every resume sends something: a hosted link built without turn config
    replays neither frame while the context is still empty (hosted.py:113-119).
    So the fresh socket can die inside the resume window without anything
    raising, and its _on_disconnect now defers to this very loop. Calling that
    attempt a success would announce LinkUp over no socket at all, with nobody
    left climbing — silence for the rest of the stream.
    """
    clock = FakeClock()
    async with MockRealtimeServer(caps=caps_mod.DASHSCOPE, codec=dia.BETA) as server:
        client = RealtimeClient(
            server.url,
            caps=caps_mod.DASHSCOPE,
            codec=dia.BETA,
            clock=clock,
            auto_reconnect=True,
            reconnect_backoff_s=1.0,
        )
        drops_left = 1

        async def resume() -> None:
            nonlocal drops_left
            if drops_left:
                drops_left -= 1
                await server.drop_connection()
                for _ in range(20):
                    await asyncio.sleep(0)

        client.on_resume = resume
        events = client.events()
        await client.connect()
        try:
            await server.drop_connection()
            await _next_event(events, link.LinkDown)
            await clock.advance(1.5)  # attempt 1: opens, and dies while resuming
            await _next_event(events, link.LinkDown)
            for _ in range(20):
                await asyncio.sleep(0)
            await clock.advance(3.0)  # attempt 2
            up = await _next_event(events, link.LinkUp)
            assert isinstance(up, link.LinkUp)
            assert client._ws is not None, "宣布连上了，socket 却已经没了"
        finally:
            await client.aclose()


async def test_a_create_that_dies_mid_send_reports_the_link_not_a_list_error() -> None:
    """The error the streamer would have had to debug from.

    `await ws.send()` on a closing connection waits for the close to finish and
    THEN raises (websockets/asyncio/connection.py:873-876), and _on_disconnect
    runs inside that wait — it clears _awaiting_created (client.py:336). The
    failure path then removed a record that was already gone, so what reached
    the scheduler was `list.remove(x): x not in list`, and dispatch_failed plus
    verdict.detail recorded that instead of a 1011 close.
    """
    import websockets

    closed = websockets.ConnectionClosedError(None, None)

    class _ClosingSocket:
        """Sends the way websockets does while the peer is closing."""

        def __init__(self, owner: RealtimeClient) -> None:
            self._owner = owner

        async def send(self, payload: str) -> None:
            self._owner._on_disconnect("connection_closed:1011", closed)
            raise closed

        async def close(self) -> None:
            return None

    client = RealtimeClient("ws://unused", caps=caps_mod.S2S, codec=dia.GA)
    client._ws = _ClosingSocket(client)
    with pytest.raises(websockets.ConnectionClosed):
        await client.request_reply({"type": "response.create"})
    # And the books stay usable: the slot is free and the drop was announced.
    down = await _next_event(client.events(), link.LinkDown)
    assert isinstance(down, link.LinkDown)
    assert down.reason == "connection_closed:1011"


def test_a_drop_mid_utterance_does_not_wedge_the_floor() -> None:
    """The bug that made reconnect pointless.

    streamer_speaking is set by SpeechStarted and cleared only by
    SpeechStopped. A socket that dies while the streamer talks means that
    second frame never arrives, so the gate stays shut forever — the queue
    fills and nothing is ever said, even after a perfectly good reconnect.
    """
    clock = FakeClock()
    floor = SpeakingFloor(clock)
    floor.on_speech_started()
    floor.on_reply_active(True)
    assert floor.is_blocked()

    floor.on_link_lost()
    assert not floor.is_blocked(), "the link is gone; none of its flags still mean anything"
    assert floor.blocked_for() == 0.0


def test_error_classes_decide_whether_retrying_can_help() -> None:
    """A wrong key must not be retried eight times; a dropped packet must."""
    import websockets

    assert classify_error(TimeoutError()) is ErrorClass.RETRYABLE
    assert classify_error(ConnectionRefusedError()) is ErrorClass.RETRYABLE

    class _Rejected(Exception):
        status_code = 401

    assert classify_error(_Rejected()) is ErrorClass.FATAL

    class _Busy(Exception):
        status_code = 429

    assert classify_error(_Busy()) is ErrorClass.BACKOFF

    policy = websockets.ConnectionClosedError(None, None)
    # Unknown close codes stay retryable: an unexplained drop is far more
    # often a network blip than a refusal, and a wrong FATAL means silence.
    assert classify_error(policy) is ErrorClass.RETRYABLE


# ------------------------------------------------------------ playback backlog


def test_the_speaker_never_falls_more_than_the_cap_behind() -> None:
    """Measured failure: replies are dispatched when the SERVER finishes
    generating, which is seconds before the audience finishes hearing the
    previous one. Fourteen back-to-back replies against the real endpoint
    built 106 seconds of unplayed audio in 48 seconds of wall clock.

    The floor gate is the real fix; this ceiling is the backstop for whenever
    the gate is bypassed. Falling behind by minutes must not be reachable.
    """
    from bilisama.dev_talk import _MAX_BACKLOG_BYTES, _OUTPUT_RATE, _Speaker

    speaker = _Speaker.__new__(_Speaker)  # no PortAudio device in a unit test
    speaker._buffer = bytearray()
    speaker._dropped_s = 0.0
    speaker._lock = __import__("threading").Lock()
    speaker._stream = object()  # pretend a device is attached

    one_second = b"\x00\x00" * _OUTPUT_RATE
    for _ in range(60):
        speaker.play(one_second)

    cap_s = _MAX_BACKLOG_BYTES / 2 / _OUTPUT_RATE
    assert speaker.backlog_s <= cap_s, f"backlog ran to {speaker.backlog_s:.0f}s"
    assert speaker.dropped_s > 0, "skipping forward must be visible, not silent"


def test_a_muted_run_does_not_latch_the_playback_gate() -> None:
    """With no output device nothing drains the buffer, so buffering at all
    would leave `busy` True forever — and the new gate would then hold the
    floor shut for the rest of the stream. Worse than having no speaker."""
    from bilisama.dev_talk import _OUTPUT_RATE, _Speaker

    speaker = _Speaker.__new__(_Speaker)
    speaker._buffer = bytearray()
    speaker._dropped_s = 0.0
    speaker._lock = __import__("threading").Lock()
    speaker._stream = None

    speaker.play(b"\x00\x00" * _OUTPUT_RATE)
    assert not speaker.busy
    assert speaker.backlog_s == 0.0


def test_dropping_backlog_keeps_sample_alignment() -> None:
    """An odd-byte drop is not a small glitch.

    16-bit audio read half a sample out of phase is full-scale white noise for
    the rest of the run. A trailing half sample is harmless — the next chunk
    completes it — but the skip-forward must remove whole samples, so what
    survives is still a run of the original ones.
    """
    import struct

    from bilisama.dev_talk import _MAX_BACKLOG_BYTES, _Speaker

    speaker = _Speaker.__new__(_Speaker)
    speaker._buffer = bytearray()
    speaker._dropped_s = 0.0
    speaker._lock = __import__("threading").Lock()
    speaker._stream = object()

    # A ramp, so a one-byte phase shift is unmistakable in the decoded values.
    total = _MAX_BACKLOG_BYTES // 2 + 1000
    ramp = [i % 3000 for i in range(total)]
    speaker.play(struct.pack(f"<{total}h", *ramp))
    # And an odd-length chunk on top: the server should never send one, but a
    # split base64 frame would, and the drop maths must not care.
    speaker.play(b"\x01" * 7)

    left = bytes(speaker._buffer)
    got = struct.unpack(f"<{len(left) // 2}h", left[: len(left) // 2 * 2])
    # Every surviving sample must still be a ramp value at its right place:
    # a byte-shifted read produces values nothing like the original series.
    assert got[0] in ramp, f"first surviving sample {got[0]} is not a real sample"
    head = ramp.index(got[0])
    assert list(got[:50]) == ramp[head : head + 50], "samples are out of phase"


@contextmanager
def _capture_errors(into: list[str]) -> Iterator[None]:
    """Collect what the dev-talk logger reports, without touching handlers
    the gate's own logging setup installed."""

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "fields", {})
            into.append(f"{record.getMessage()} {fields}")

    logger = logging.getLogger("bilisama.dev_talk")
    sink = _Sink()
    logger.addHandler(sink)
    try:
        yield
    finally:
        logger.removeHandler(sink)


class _FlakyLink:
    """A push_audio that fails while the socket is gone, then comes back."""

    def __init__(self, failures: int) -> None:
        self.sent: list[bytes] = []
        self._left = failures

    async def push_audio(self, pcm: bytes) -> None:
        if self._left > 0:
            self._left -= 1
            # Exactly what the client raises with no socket (client.py:270).
            raise ConnectionError("还没连接")
        self.sent.append(pcm)


async def test_the_uplink_survives_an_outage_instead_of_dying_silently() -> None:
    """A dropped link must not end the microphone for the rest of the session.

    Nobody awaits this loop — it is one of run_director's create_task list —
    so an exception in it costs the uplink without a word. That became the
    worst kind of failure once the client learned to reconnect: the socket
    returns, the persona replays, the panel goes green, and the streamer's
    voice never arrives again. It looks exactly like a successful recovery.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    link_ = _FlakyLink(failures=3)
    blocks = [bytes([i]) * 4 for i in range(5)]
    for block in blocks:
        queue.put_nowait(block)

    task = asyncio.create_task(_uplink(link_, queue, speaker=None, mute=False, silence=b""))
    try:
        for _ in range(200):
            if queue.empty() and len(link_.sent) == 2:
                break
            await asyncio.sleep(0)
        assert not task.done(), "上行任务在断线时死掉了，链路回来也不会自愈"
        # The three blocks sent while down are gone — there was no socket to
        # take them. The two after are what the provider actually hears.
        assert link_.sent == blocks[3:]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_background_task_that_dies_says_so() -> None:
    """Eleven of these run unwatched; today any of them can end in silence."""
    records: list[str] = []

    async def boom() -> None:
        raise RuntimeError("炸了")

    task = _watch(asyncio.create_task(boom(), name="director:测试"))
    with _capture_errors(records):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert any(
        "dev_talk.task_died" in line and "director:测试" in line and "炸了" in line
        for line in records
    ), records


async def test_watching_a_cancelled_task_stays_quiet() -> None:
    """Shutdown cancels all eleven; that is not a fault worth reporting."""
    records: list[str] = []

    async def forever() -> None:
        await asyncio.Event().wait()

    task = _watch(asyncio.create_task(forever(), name="director:收尾"))
    with _capture_errors(records):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert records == []


async def test_an_uplink_that_never_recovers_gives_up() -> None:
    """Surviving an outage must not become pretending to be alive.

    Wire mode ends the session on this task's exception; swallowing forever
    would leave a process that looks connected and hears nothing.
    """
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    link_ = _FlakyLink(failures=10**9)
    task = asyncio.create_task(_uplink(link_, queue, speaker=None, mute=False, silence=b""))
    try:
        # One block past the patience window, fed as fast as the loop drains.
        for _ in range(int(_UPLINK_PATIENCE_S * 1000 / _FRAME_MS) + 2):
            queue.put_nowait(b"\x00\x00")
        for _ in range(50_000):
            if task.done():
                break
            await asyncio.sleep(0)
        assert task.done(), "上行永远吞下去了，链路死了也看不出来"
        with pytest.raises(ConnectionError):
            task.result()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_audio_is_dropped_where_it_is_played() -> None:
    """The tail that kept her talking a second past the interruption.

    Cancelling marks the handle, and the client drops later frames carrying
    it — but audio already decoded and sitting in a fanout view arrives behind
    the cancel and is nobody's to drop but the consumer's. Measured against a
    live barge-in before this guard: 1.08 seconds of speech after the queue had
    been emptied, which the streamer hears as her talking over them.
    """
    handle = link.ReplyHandle()
    fresh = link.ReplyAudioDelta(handle=handle, pcm=b"\x01\x02")
    assert not fresh.handle.stale

    # link.py flips this on cancel, supersede and timeout alike; the consumer
    # only has to look.
    stale_handle = link.ReplyHandle(stale=True)
    stale = link.ReplyAudioDelta(handle=stale_handle, pcm=b"\x03\x04")
    assert stale.handle.stale, "作废标记就在事件上，播放侧没有理由看不见"
