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

from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName
from bilisama.config.schema import HostedTurnConfig
from bilisama.director.floor import SpeakingFloor
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
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
