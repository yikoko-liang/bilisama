"""The three scheduler entry points the operator drives, not the pipeline.

Everything in test_director.py enters through submit() and comes out as a
verdict. These three do not: `status()` is read by the health card and the panel
(dev_talk.py:1218, :1376, :1707), `release_panic()` is the only way out of
panic-mute (:1391), and `notify()` is how whoever owns the speaker tells the
dispatch loop that playback finished — an event the link never sends. All three
had runtime readers and no test.

panic-mute matters most of the two directions: it is the plan's one 「一键闭嘴」,
its only entry point is the web panel (ledger #47), and until now the release
side was pinned by nothing at all. A panic you cannot leave is a stream that
stays silent until a restart.

Kept out of test_director.py deliberately for now — that file is being edited in
parallel — and it should move in beside the rest of the scheduler tests once
that settles.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from bilisama.clock import SystemClock
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.scheduler import Scheduler
from bilisama.obs.outcome import Outcome, SkipReason
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime.link import ReplySpec
from bilisama.realtime.providers.s2s import S2SLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script


def _intent(source: str = "danmaku", *, dedup: str = "") -> Intent:
    return Intent(
        source=source,
        priority=Priority.DANMAKU,
        injection=Injection(reply=ReplySpec(instructions="回一句"), item_text="[弹幕] 观众A: 你好"),
        dedup_key=dedup,
    )


@contextlib.asynccontextmanager
async def _running(
    script: Script | None = None,
) -> AsyncIterator[tuple[Scheduler, SpeakingFloor]]:
    """A scheduler running against the fake server, torn down on the way out.

    Owns the server as well as the scheduler: none of the tests below assert on
    frames the server received, so handing one out would only buy a second
    context manager per test.
    """
    clock = SystemClock()
    async with MockRealtimeServer(caps=caps_mod.S2S, script=script or Script()) as server:
        linkobj = S2SLink(server.url)
        await linkobj.connect()
        floor = SpeakingFloor(clock)
        scheduler = Scheduler(linkobj, floor, clock)
        runner = asyncio.create_task(scheduler.run())
        try:
            yield scheduler, floor
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            await linkobj.aclose()


async def _wait_verdicts(scheduler: Scheduler, count: int, *, timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if len(scheduler.verdicts) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等到 {len(scheduler.verdicts)} 条终局，要 {count} 条")


# ------------------------------------------------------------ status()


async def test_status_reports_an_idle_scheduler() -> None:
    """The shape the health card renders, on a scheduler with nothing to do.

    Four keys, and none of them optional: the card and the panel both index into
    this dict, so a renamed key is a KeyError on the operator's screen rather
    than a missing line.
    """
    async with _running() as (scheduler, _):
        assert scheduler.status() == {
            "panicked": False,
            "queued": 0,
            "active_source": None,
            "dispatching": False,
        }


async def test_status_counts_work_the_floor_is_holding() -> None:
    """Queued depth is the number the operator watches when nothing is speaking.

    Held behind the floor rather than raced against a live dispatch: the intents
    have to still be in the heap when the count is read, and a scheduler that
    dispatched one would report a queue of one with an active source, which is a
    different assertion.
    """
    async with _running() as (scheduler, floor):
        floor.streamer_speaking = True  # the gate that blocks every dispatch
        scheduler.submit(_intent(dedup="a"))
        scheduler.submit(_intent(dedup="b"))
        await asyncio.sleep(0.05)

        status = scheduler.status()
        assert status["queued"] == 2, status
        assert status["active_source"] is None, status
        assert status["panicked"] is False


async def test_status_names_the_source_that_is_speaking() -> None:
    """Not just "busy": which lane holds the floor is the operator's next question."""
    script = Script(delta_chunks=6, delta_interval_s=0.05)
    async with _running(script) as (scheduler, _):
        scheduler.submit(_intent("super_chat", dedup="sc_1"))
        for _ in range(200):
            if scheduler.status()["active_source"] is not None:
                break
            await asyncio.sleep(0.01)

        assert scheduler.status()["active_source"] == "super_chat"


async def test_status_says_so_while_panicked() -> None:
    """The flag the panel keys the 「已闭嘴」 badge on.

    Read straight after panic_mute, before any verdict lands: the badge has to
    flip on the click, not on the queue draining.
    """
    async with _running() as (scheduler, _):
        scheduler.panic_mute()

        assert scheduler.status()["panicked"] is True


# ------------------------------------------------------------ release_panic()


async def test_release_panic_lets_work_through_again() -> None:
    """The way out of the one switch that stops everything.

    Ledger #47 notes panic-mute has a single entry point, the web panel. The exit
    had a single entry point and no test — so a release that quietly failed
    would look exactly like a co-host with nothing to say, and the fix would be
    a restart.
    """
    script = Script(delta_chunks=2, delta_interval_s=0.01)
    async with _running(script) as (scheduler, _):
        scheduler.panic_mute()
        scheduler.submit(_intent(dedup="during"))
        await _wait_verdicts(scheduler, 1)
        assert scheduler.verdicts[-1].reason is SkipReason.PANIC_MUTE

        scheduler.release_panic()
        assert scheduler.status()["panicked"] is False
        scheduler.submit(_intent(dedup="after"))
        await _wait_verdicts(scheduler, 2)

        outcomes = [v.outcome for v in scheduler.verdicts]
        assert outcomes.count(Outcome.SPOKEN) == 1, [str(v) for v in scheduler.verdicts]
        assert outcomes.count(Outcome.SKIPPED) == 1, [str(v) for v in scheduler.verdicts]


async def test_release_panic_on_a_scheduler_that_never_panicked_is_harmless() -> None:
    """Boundary: the panel sends this on every 「恢复」 click, panicked or not."""
    async with _running() as (scheduler, _):
        scheduler.release_panic()

        assert scheduler.status()["panicked"] is False


async def test_release_panic_does_not_resurrect_what_panic_drained() -> None:
    """Error path: the queue is gone, not paused.

    panic_mute writes a verdict for everything in the heap on the way through.
    A release that somehow brought them back would speak a burst of stale lines
    into a stream the operator just silenced — the opposite of what the button
    is for.
    """
    async with _running() as (scheduler, floor):
        floor.streamer_speaking = True
        scheduler.submit(_intent(dedup="a"))
        scheduler.submit(_intent(dedup="b"))
        await asyncio.sleep(0.05)
        assert scheduler.status()["queued"] == 2

        scheduler.panic_mute()
        await _wait_verdicts(scheduler, 2)
        scheduler.release_panic()
        floor.streamer_speaking = False
        scheduler.notify()
        await asyncio.sleep(0.1)

        assert scheduler.status()["queued"] == 0
        assert len(scheduler.verdicts) == 2, [str(v) for v in scheduler.verdicts]


# ------------------------------------------------------------ notify()


async def test_notify_wakes_a_loop_that_a_gate_left_asleep() -> None:
    """Why the method exists: playback finishing is nobody's link event.

    The dispatch loop sleeps until something wakes it. Flags on the floor are
    flipped by whoever owns the speaker, and clearing one changes no state the
    loop is watching — so without this call the queued intent waits for the next
    unrelated event. Asserted as a sequence, because the claim is about the
    order: still queued after the gate opens, dispatched after the nudge.
    """
    script = Script(delta_chunks=2, delta_interval_s=0.01)
    async with _running(script) as (scheduler, floor):
        floor.queued_audio = True  # the speaker still has audio to play
        scheduler.submit(_intent(dedup="held"))
        await asyncio.sleep(0.05)
        assert scheduler.status()["queued"] == 1, "the gate did not hold it"

        floor.queued_audio = False
        scheduler.notify()
        await _wait_verdicts(scheduler, 1)

        assert scheduler.verdicts[-1].outcome is Outcome.SPOKEN, str(scheduler.verdicts[-1])


async def test_notify_on_an_empty_queue_does_nothing_visible() -> None:
    """Boundary: the console calls it on every playback receipt, queue or not.

    A wake that invented work would turn "the speaker went quiet" into a reply
    nobody asked for.
    """
    async with _running() as (scheduler, _):
        scheduler.notify()
        await asyncio.sleep(0.05)

        assert scheduler.verdicts == []
        assert scheduler.status()["queued"] == 0
