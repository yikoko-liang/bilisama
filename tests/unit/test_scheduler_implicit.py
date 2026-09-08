"""The provider's own VAD turn under the scheduler's one kill path.

Her own microphone turns used to be invisible to everything above the link:
the scheduler only flipped the floor for them, so the output guard, panic and
memory all passed them by — the red button left her talking to the end. These
tests pin the new books (_Implicit) and the single kill path (skip_implicit)
that the voice gate, the guard and panic share.

Every test runs against the fake server's implicit reply on the s2s shape,
which announces nothing and is booked from its first frame — the harder of the
two shapes for a client. The deltas are spaced so the turn is still in flight
when the test acts on it; the fake ignores a cancel before the first token
(rule 3), so every kill lands after one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, cast

from bilisama.clock import SystemClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.scheduler import PlaybackClear, Scheduler
from bilisama.obs.outcome import Outcome, Phase, SkipReason
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import link
from bilisama.realtime.providers.s2s import S2SLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script

_SLOW = Script(delta_chunks=8, delta_interval_s=0.05)


class _Tee:
    """The link, with every started handle noted on the way past.

    skip_implicit takes the turn's handle, which in production the voice gate
    reads off the fan-out's copy of ReplyStarted. Here the scheduler is the
    only consumer, so the tee is where a test learns the handle.
    """

    def __init__(self, inner: S2SLink) -> None:
        self._inner = inner
        self.started: list[link.ReplyHandle] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def events(self) -> AsyncIterator[link.LinkEvent]:
        async for event in self._inner.events():
            if isinstance(event, link.ReplyStarted):
                self.started.append(event.handle)
            yield event


@contextlib.asynccontextmanager
async def _running(
    script: Script = _SLOW,
    *,
    guard: Callable[[str], bool] | None = None,
    on_hit: Literal["drop_sentence", "mute_all"] = "drop_sentence",
    spoken_sink: Callable[[str], None] | None = None,
    implicit_spoken_sink: Callable[[str], None] | None = None,
) -> AsyncIterator[tuple[Scheduler, MockRealtimeServer, _Tee]]:
    clock = SystemClock()
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        tee = _Tee(linkobj)
        scheduler = Scheduler(
            cast(link.SpeechLink, tee),
            SpeakingFloor(clock),
            clock,
            guard=guard,
            on_hit=on_hit,
            spoken_sink=spoken_sink,
            implicit_spoken_sink=implicit_spoken_sink,
        )
        runner = asyncio.create_task(scheduler.run())
        try:
            yield scheduler, server, tee
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            await linkobj.aclose()


async def _until(predicate: Callable[[], bool], *, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"等了 {timeout}s 也没等到：{what}")


async def _her_turn_in_flight(scheduler: Scheduler, server: MockRealtimeServer) -> None:
    await server.emit_implicit_reply()
    await _until(lambda: scheduler.status()["implicit_active"], what="她自起的回复开始")


def _drain(scheduler: Scheduler) -> list[PlaybackClear]:
    out: list[PlaybackClear] = []
    while not scheduler.controls.empty():
        out.append(scheduler.controls.get_nowait())
    return out


async def test_her_own_turn_is_booked_while_it_lives_and_not_after() -> None:
    async with _running() as (scheduler, server, _):
        assert scheduler.status()["implicit_active"] is False
        await _her_turn_in_flight(scheduler, server)
        await _until(lambda: not scheduler.status()["implicit_active"], what="她自起的回复结束")
        assert scheduler.verdicts == [], "a turn that was spoken gets no verdict"


async def test_a_completed_turn_reaches_the_implicit_sink_and_only_that_one() -> None:
    hers: list[str] = []
    ours: list[str] = []
    async with _running(
        Script(reply_text="我自己说的"), spoken_sink=ours.append, implicit_spoken_sink=hers.append
    ) as (_scheduler, server, _):
        await server.emit_implicit_reply()
        await _until(lambda: hers == ["我自己说的"], what="文字到达 implicit_spoken_sink")
        assert ours == [], "the dispatched-reply sink is not hers to fill"


async def test_skip_implicit_kills_once_with_one_cancel_and_one_verdict() -> None:
    hers: list[str] = []
    async with _running(implicit_spoken_sink=hers.append) as (scheduler, server, tee):
        await _her_turn_in_flight(scheduler, server)
        handle = tee.started[-1]
        assert handle.implicit
        for _ in range(2):
            scheduler.skip_implicit(
                handle, reason=SkipReason.VOICE_NOT_ADDRESSED, detail="AUDIENCE · 在聊天气"
            )
        await _until(lambda: not scheduler.status()["implicit_active"], what="被杀的回复结束")
        assert len(scheduler.verdicts) == 1
        verdict = scheduler.verdicts[0]
        assert verdict.source == "voice"
        assert verdict.intent_id == f"voice:{handle.handle_id}"
        assert (verdict.outcome, verdict.phase) == (Outcome.SKIPPED, Phase.GENERATING)
        assert verdict.reason is SkipReason.VOICE_NOT_ADDRESSED
        assert verdict.detail == "AUDIENCE · 在聊天气"
        assert server.recorded.count("response.cancel") == 1
        assert _drain(scheduler) == [], "nothing of a held turn reached a speaker"
        assert hers == [], "a killed turn is not a spoken line"


async def test_skip_implicit_can_ask_for_the_speakers_to_be_flushed() -> None:
    async with _running() as (scheduler, server, tee):
        await _her_turn_in_flight(scheduler, server)
        scheduler.skip_implicit(
            tee.started[-1], reason=SkipReason.VOICE_NOT_ADDRESSED, clear_playback=True
        )
        assert _drain(scheduler) == [PlaybackClear(reason="voice_not_addressed")]


async def test_a_handle_the_scheduler_never_saw_start_is_ignored() -> None:
    async with _running() as (scheduler, server, _):
        await _her_turn_in_flight(scheduler, server)
        scheduler.skip_implicit(link.ReplyHandle(implicit=True), reason=SkipReason.PANIC_MUTE)
        await asyncio.sleep(0.05)
        assert scheduler.verdicts == []
        assert server.recorded.count("response.cancel") == 0, "a stray handle must not cancel"


async def test_the_output_guard_covers_her_own_turn() -> None:
    async with _running(
        Script(reply_text="禁词禁词禁词禁词禁词禁词", delta_chunks=3, delta_interval_s=0.05),
        guard=lambda text: "禁词" in text,
    ) as (scheduler, server, _):
        await server.emit_implicit_reply()
        await _until(lambda: len(scheduler.verdicts) == 1, what="守卫命中的判决")
        verdict = scheduler.verdicts[0]
        assert verdict.source == "voice"
        assert (verdict.outcome, verdict.phase) == (Outcome.FAILED, Phase.SPEAKING)
        assert verdict.reason is SkipReason.OUTPUT_BLOCKED
        assert verdict.detail == "", "the blocked word stays out of the record"
        assert _drain(scheduler) == [PlaybackClear(reason="output_blocked")]
        await _until(lambda: not scheduler.status()["implicit_active"], what="被守卫杀掉的回复结束")
        assert server.recorded.count("response.cancel") == 1
        assert scheduler.status()["panicked"] is False


async def test_mute_all_escalates_from_her_own_turn() -> None:
    async with _running(
        Script(reply_text="禁词禁词禁词禁词禁词禁词", delta_chunks=3, delta_interval_s=0.05),
        guard=lambda text: "禁词" in text,
        on_hit="mute_all",
    ) as (scheduler, server, _):
        await server.emit_implicit_reply()
        await _until(lambda: scheduler.status()["panicked"], what="升级成紧急闭嘴")
        assert len(scheduler.verdicts) == 1, "panic finds the turn already killed"
        assert [c.reason for c in _drain(scheduler)] == ["output_blocked", "panic_mute"]


async def test_panic_kills_her_own_turn_too() -> None:
    async with _running() as (scheduler, server, _):
        await _her_turn_in_flight(scheduler, server)
        scheduler.panic_mute()
        await _until(lambda: len(scheduler.verdicts) == 1, what="紧急闭嘴的判决")
        verdict = scheduler.verdicts[0]
        assert verdict.source == "voice"
        assert (verdict.outcome, verdict.phase) == (Outcome.CANCELLED, Phase.SPEAKING)
        assert verdict.reason is SkipReason.PANIC_MUTE
        # One clear, from panic itself: the kill path must not add a second.
        assert [c.reason for c in _drain(scheduler)] == ["panic_mute"]
        await _until(lambda: not scheduler.status()["implicit_active"], what="被闭嘴的回复结束")
        assert server.recorded.count("response.cancel") == 1


async def test_the_streamer_speaking_over_her_is_the_providers_cut_not_ours() -> None:
    """s2s sends done(cancelled) before speech_started: by the time the
    scheduler hears the streamer, the turn is already over. A cancel from our
    side would land on whoever holds the slot next (client.py:284-286)."""
    async with _running() as (scheduler, server, _):
        await _her_turn_in_flight(scheduler, server)
        await server.barge_in()
        await _until(lambda: not scheduler.status()["implicit_active"], what="被主播打断的回复结束")
        await asyncio.sleep(0.05)
        assert server.recorded.count("response.cancel") == 0
        assert scheduler.verdicts == [], "the provider's own cut is not a verdict"
        assert _drain(scheduler) == []
