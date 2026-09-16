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

import pytest

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


async def test_a_handle_that_is_not_her_turn_or_already_over_is_ignored() -> None:
    async with _running() as (scheduler, server, _):
        await _her_turn_in_flight(scheduler, server)
        scheduler.skip_implicit(link.ReplyHandle(), reason=SkipReason.PANIC_MUTE)
        scheduler.skip_implicit(
            link.ReplyHandle(implicit=True, stale=True), reason=SkipReason.PANIC_MUTE
        )
        await asyncio.sleep(0.05)
        assert scheduler.verdicts == []
        assert server.recorded.count("response.cancel") == 0, "a stray handle must not cancel"


class _ScriptedLink:
    """A link whose event stream the test feeds by hand, so the order in
    which the scheduler's task sees frames is exactly the test's."""

    quiet_window_s = 1.0

    def __init__(self) -> None:
        self.queue: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self.cancelled: list[link.ReplyHandle] = []

    async def events(self) -> AsyncIterator[link.LinkEvent]:
        while True:
            yield await self.queue.get()

    async def cancel(self, handle: link.ReplyHandle) -> None:
        self.cancelled.append(handle)

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    async def suspend(self) -> None: ...

    async def resume(self) -> None: ...

    async def set_context(self, instructions: str) -> None: ...

    async def push_audio(self, pcm: bytes) -> None: ...

    async def add_context_item(self, text: str, *, role: str = "user") -> None: ...

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        return link.ReplyHandle()

    async def end_protection(self) -> None: ...


async def test_a_kill_that_lands_before_the_turn_started_is_honoured() -> None:
    """The gate decides inside the fan-out's pump, which can read a turn's
    ReplyStarted and its first delta from one queue without yielding — so the
    kill can reach the scheduler before its own task saw the turn start. The
    record is booked killed on the spot; the start keeps it, the done closes
    it, and nothing is spoken or remembered."""
    scripted = _ScriptedLink()
    hers: list[str] = []
    clock = SystemClock()
    scheduler = Scheduler(
        cast(link.SpeechLink, scripted),
        SpeakingFloor(clock),
        clock,
        implicit_spoken_sink=hers.append,
    )
    runner = asyncio.create_task(scheduler.run())
    try:
        handle = link.ReplyHandle(implicit=True)
        scripted.queue.put_nowait(link.ReplyStarted(handle))
        scripted.queue.put_nowait(link.ReplyTextDelta(handle, "[AUDIENCE] 讲故事"))
        # No await in between: the scheduler's task has not run yet.
        scheduler.skip_implicit(
            handle, reason=SkipReason.VOICE_NOT_ADDRESSED, detail="AUDIENCE · 讲故事"
        )
        assert scheduler.status()["implicit_active"] is True
        await asyncio.sleep(0.02)
        assert scripted.cancelled == [handle]
        assert scheduler.status()["implicit_active"] is True, "the start kept the killed record"
        scripted.queue.put_nowait(
            link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="[AUDIENCE] 讲故事")
        )
        await asyncio.sleep(0.02)
        assert scheduler.status()["implicit_active"] is False
        assert [v.reason for v in scheduler.verdicts] == [SkipReason.VOICE_NOT_ADDRESSED]
        assert hers == [], "a killed turn is not a spoken line, completed or not"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


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


# ------------------------------------------------ the audit of 2026-09-16


class _StubLink:
    """Just enough SpeechLink for skip_implicit's one send."""

    def __init__(self) -> None:
        self.cancels: list[link.ReplyHandle] = []

    async def cancel(self, handle: link.ReplyHandle) -> None:
        self.cancels.append(handle)


def _done_for(handle: link.ReplyHandle, status: link.ReplyStatus) -> link.ReplyDone:
    import dataclasses

    kw: dict[str, object] = {"handle": handle, "status": status}
    for f in dataclasses.fields(link.ReplyDone):
        if f.name not in kw and f.default is dataclasses.MISSING:
            kw[f.name] = ""
    return link.ReplyDone(**kw)  # type: ignore[arg-type]


async def test_a_skip_that_arrives_after_the_turn_completed_is_ignored() -> None:
    """skip_implicit read handle.stale as 「already over」, but only a
    NON-completed settle marks a handle stale. A turn that finished normally
    is never stale, so a late skip for it booked a fresh killed record:
    implicit_active stuck True, the panel showed her speaking, and panic
    would have walked the kill path again for a turn that was long over.
    """
    from bilisama.clock import FakeClock
    from bilisama.director.floor import SpeakingFloor
    from bilisama.director.scheduler import Scheduler
    from bilisama.obs.outcome import SkipReason

    clock = FakeClock()
    stub = _StubLink()
    sched = Scheduler(stub, SpeakingFloor(clock), clock)  # type: ignore[arg-type]
    hers = link.ReplyHandle(implicit=True)
    sched._open_implicit(hers)
    assert sched.status()["implicit_active"] is True
    sched._close_implicit(_done_for(hers, link.ReplyStatus.COMPLETED))
    assert sched.status()["implicit_active"] is False
    assert not hers.stale, "a completed handle is never stale — that was the whole trap"

    sched.skip_implicit(hers, reason=SkipReason.VOICE_NOT_ADDRESSED, detail="")
    await asyncio.sleep(0)
    assert sched.status()["implicit_active"] is False, "its done already came; nothing to re-book"
    assert stub.cancels == [], "and nothing to kill"


# ------------------------------------------ the ghostwriter seam (duplex)


class _TextLink(_StubLink):
    """Records the spec each dispatch actually sent."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: list[link.ReplySpec] = []

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        return None

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        self.specs.append(spec)
        return link.ReplyHandle()


def _danmaku_intent() -> Any:
    from bilisama.director.intent import Injection, Intent, Priority

    return Intent(
        source="danmaku",
        priority=Priority.DANMAKU,
        injection=Injection(
            reply=link.ReplySpec(instructions="回一句"),
            item_text="[弹幕] 观众A: 主播今天播到几点",
        ),
    )


async def test_a_link_that_composes_never_calls_the_ghostwriter() -> None:
    """Three of the four providers write her lines themselves. The seam must
    be invisible to them — no extra await, no text on the spec."""
    from bilisama.clock import FakeClock
    from bilisama.director.floor import SpeakingFloor

    clock = FakeClock()
    stub = _TextLink()
    called = False

    async def never(_intent: Any) -> str:
        nonlocal called
        called = True
        return "不该被叫到"

    sched = Scheduler(stub, SpeakingFloor(clock), clock, ghostwriter=never)  # type: ignore[arg-type]
    spec = await sched._with_text(_danmaku_intent())
    assert spec.text is None and not called


async def test_a_link_that_cannot_compose_gets_finished_words() -> None:
    """On duplex the endpoint only says what it is handed, so the words are
    written above and ride ReplySpec.text — and the instructions stay on the
    spec untouched, because they are what the ghostwriter was briefed with."""
    from bilisama.clock import FakeClock
    from bilisama.director.floor import SpeakingFloor

    clock = FakeClock()
    stub = _TextLink()
    seen: list[str] = []

    async def write(intent: Any) -> str:
        seen.append(intent.injection.item_text or "")
        return "谢谢关注呀，今天播到十点"

    sched = Scheduler(
        stub,  # type: ignore[arg-type]
        SpeakingFloor(clock),
        clock,
        ghostwriter=write,
        link_composes_on_demand=False,
    )
    spec = await sched._with_text(_danmaku_intent())
    assert spec.text == "谢谢关注呀，今天播到十点"
    assert spec.instructions == "回一句", "简报不变，代笔看的就是它"
    assert seen == ["[弹幕] 观众A: 主播今天播到几点"], "代笔拿到的是这条弹幕本身"


async def test_a_missing_ghostwriter_is_loud() -> None:
    """Silence would be the worst outcome: every danmaku would fail to
    dispatch with no reason anyone could act on."""
    from bilisama.clock import FakeClock
    from bilisama.director.floor import SpeakingFloor

    clock = FakeClock()
    sched = Scheduler(
        _TextLink(),  # type: ignore[arg-type]
        SpeakingFloor(clock),
        clock,
        link_composes_on_demand=False,
    )
    with pytest.raises(RuntimeError, match="代笔"):
        await sched._with_text(_danmaku_intent())
