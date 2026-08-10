"""FakeClock has to behave like SystemClock everywhere it is not deliberately different.

Only the waiting itself is fake. Yielding on a zero sleep, dropping a cancelled
sleep, refusing to run backwards — those have to match, or a test that passes under
the fake will hang or lie under the real clock. Plan §10.3 makes this the clock for
every time-driven test in stages 2 and 3, so a divergence here buys green tests for
broken production code.

Some tests reach into `_waiters`. That is deliberate: the leak they cover has no
public observable, because `advance()` filters out done futures.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bilisama.clock import Clock, FakeClock, SystemClock

# Both clocks must satisfy what production code depends on. mypy checks these two
# lines; isinstance cannot, because Clock is a plain Protocol and not runtime
# checkable, so isinstance(x, Clock) raises TypeError.
_SYSTEM: Clock = SystemClock()
_FAKE: Clock = FakeClock()


# ------------------------------------------------------------ sleep fidelity


@pytest.mark.parametrize("clock", [SystemClock(), FakeClock()], ids=["system", "fake"])
@pytest.mark.parametrize("seconds", [0, 0.0, -1.0], ids=["int-zero", "float-zero", "negative"])
async def test_sleep_of_zero_or_less_yields_control(clock: Clock, seconds: float) -> None:
    """`await clock.sleep(0)` is how a caller hands another task a turn.

    asyncio.sleep folds everything <= 0 into exactly one loop turn, negatives
    included. A fake that returns without yielding lets a scheduling bug pass under
    test and reappear in production. The system rows are the executable spec.
    """
    order: list[str] = []

    async def other() -> None:
        order.append("other")

    task = asyncio.create_task(other())
    await clock.sleep(seconds)
    order.append("after-sleep")
    await task

    assert order == ["other", "after-sleep"]


async def test_cancelled_sleep_drops_its_waiter() -> None:
    """A cancelled sleep must not leave its deadline behind.

    advance() skips done futures, so the leak stays invisible until a long-lived
    test has piled up thousands of them.
    """
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(5.0))
    await asyncio.sleep(0)
    assert len(clock._waiters) == 1, "the sleep never registered, so the test proves nothing"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert clock._waiters == []


async def test_completed_sleep_drops_its_waiter() -> None:
    """The normal wake path: advance() removed the tuple, sleep() must not choke on that."""
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(5.0))
    await asyncio.sleep(0)

    await clock.advance(5.0)
    await task

    assert clock._waiters == []
    assert clock.monotonic() == 5.0


async def test_advance_ignores_a_cancelled_sleeper_and_still_wakes_the_rest() -> None:
    """One dead waiter must not swallow the wake of a live one behind it."""
    clock = FakeClock()
    woke: list[float] = []

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(clock.monotonic())

    doomed = asyncio.create_task(sleeper(1.0))
    live = asyncio.create_task(sleeper(2.0))
    await asyncio.sleep(0)
    assert len(clock._waiters) == 2

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    await clock.advance(5.0)
    await live

    assert woke == [2.0]
    assert clock._waiters == []


async def test_cancelling_one_of_two_sleepers_sharing_a_deadline_drops_only_its_own() -> None:
    """Boundary. sleep() removes its waiter by tuple equality, and deadlines collide.

    Tuple comparison falls through to the future, which has no __eq__, so identity
    decides. Without that, a cancelled sleeper could take a live one's slot with it.
    """
    clock = FakeClock()
    woke: list[str] = []

    async def sleeper(name: str) -> None:
        await clock.sleep(1.0)
        woke.append(name)

    doomed = asyncio.create_task(sleeper("doomed"))
    live = asyncio.create_task(sleeper("live"))
    await asyncio.sleep(0)
    assert [t for t, _ in clock._waiters] == [1.0, 1.0]

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert len(clock._waiters) == 1

    await clock.advance(1.0)
    await live

    assert woke == ["live"]
    assert clock._waiters == []


# ------------------------------------------------------------ advance


async def test_advance_refuses_to_move_time_backwards() -> None:
    """Error path. A rewound monotonic clock turns into a cooldown that never expires."""
    clock = FakeClock(start=10.0)

    with pytest.raises(ValueError, match="forward"):
        await clock.advance(-5.0)

    assert clock.monotonic() == 10.0


async def test_advance_of_zero_is_allowed_and_moves_nothing() -> None:
    """Boundary either side of the guard: zero is not negative."""
    clock = FakeClock(start=10.0)
    await clock.advance(0.0)
    assert clock.monotonic() == 10.0


async def test_advance_wakes_sleepers_in_deadline_order() -> None:
    """Registration order must not leak into wake order."""
    clock = FakeClock()
    woke: list[float] = []

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(clock.monotonic())

    tasks = [asyncio.create_task(sleeper(s)) for s in (3.0, 1.0, 2.0)]
    await asyncio.sleep(0)
    assert len(clock._waiters) == 3

    await clock.advance(5.0)
    await asyncio.gather(*tasks)

    assert woke == [1.0, 2.0, 3.0]
    assert clock.monotonic() == 5.0


async def test_a_sleeper_never_observes_a_time_past_its_own_deadline() -> None:
    """The claim advance()'s docstring makes, which nothing verified before.

    A cooldown test that reads the clock on wake must see its own deadline, not
    wherever the driver was heading. Otherwise it passes for the wrong reason.
    """
    clock = FakeClock()
    seen: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(1.0)
        seen.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)

    await clock.advance(5.0)
    await task

    assert seen == [1.0]
    assert clock.monotonic() == 5.0


async def test_advance_short_of_a_deadline_leaves_the_sleeper_asleep() -> None:
    """Boundary on `t <= target`: short of the deadline waits, exactly on it wakes."""
    clock = FakeClock()
    woke: list[float] = []

    async def sleeper() -> None:
        await clock.sleep(3.0)
        woke.append(clock.monotonic())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)

    await clock.advance(2.0)
    assert not task.done()
    assert woke == []
    assert clock.monotonic() == 2.0

    await clock.advance(1.0)
    await task

    assert woke == [3.0]
    assert clock.monotonic() == 3.0


async def test_advance_wakes_a_sleeper_that_sleeps_again_straight_away() -> None:
    """The audit question: does the one-at-a-time loop cope with a re-registration?

    The straight-away case needs no settle window at all — the re-sleep lands
    inside the single turn the wake yields. Stage 2 windows and cooldowns are
    this shape; the settle window exists for the shapes that are not (see the
    two tests below).
    """
    clock = FakeClock()
    ticks: list[float] = []

    async def poller() -> None:
        for _ in range(3):
            await clock.sleep(1.0)
            ticks.append(clock.monotonic())

    task = asyncio.create_task(poller())
    await asyncio.sleep(0)

    await clock.advance(10.0)
    await task

    assert ticks == [1.0, 2.0, 3.0], "a re-registered sleep due before target must still wake"
    assert clock.monotonic() == 10.0
    assert clock._waiters == []


async def test_a_resleep_behind_another_await_is_still_woken() -> None:
    """The settle window at work: intermediate awaits no longer cost the wake.

    This used to pin the opposite (a silent nine-second time jump, the KNOWN
    BROKEN note that was backlog item 6): one `await asyncio.sleep(0)` between
    tick and re-sleep cost the coroutine its only turn, advance() found nothing
    due and jumped to target. The bounded settle window closes that: the loop
    gets up to _SETTLE_TURNS turns to carry a woken coroutine to its next
    clock.sleep() before advance() gives up.
    """
    clock = FakeClock()
    ticks: list[float] = []

    async def poller() -> None:
        for _ in range(3):
            await clock.sleep(1.0)
            ticks.append(clock.monotonic())
            await asyncio.sleep(0)  # the turn that used to lose the wake

    task = asyncio.create_task(poller())
    await asyncio.sleep(0)

    await clock.advance(10.0)
    await task

    assert ticks == [1.0, 2.0, 3.0], "every deadline honoured despite the extra await"
    assert clock.monotonic() == 10.0
    assert clock._waiters == []


async def test_a_resleep_behind_a_queue_hop_is_still_woken() -> None:
    """The shape that motivated the fix: `await clock.sleep(w); await queue.get()`.

    The item arrives from another task, so the wake has to survive a cross-task
    hop — strictly more turns than one asyncio.sleep(0). This is the loop shape
    the proactive topic loop and the distiller both use.
    """
    clock = FakeClock()
    queue: asyncio.Queue[int] = asyncio.Queue()
    got: list[tuple[float, int]] = []

    async def feeder() -> None:
        for i in range(2):
            await queue.put(i)
            await asyncio.sleep(0)

    async def poller() -> None:
        for _ in range(2):
            await clock.sleep(1.0)
            item = await queue.get()
            got.append((clock.monotonic(), item))

    poll = asyncio.create_task(poller())
    feed = asyncio.create_task(feeder())
    await asyncio.sleep(0)

    await clock.advance(5.0)
    await asyncio.gather(poll, feed)

    assert got == [(1.0, 0), (2.0, 1)]
    assert clock.monotonic() == 5.0


async def test_a_chain_longer_than_the_settle_window_is_jumped_past() -> None:
    """The documented boundary of the fix, pinned so it stays a contract.

    _SETTLE_TURNS is a bounded heuristic, not quiescence detection: a coroutine
    that crosses more awaits than that before its next clock.sleep() is jumped
    past, exactly like before the fix. No production loop is that shape; if one
    ever is, this test is where the discussion starts.
    """
    from bilisama.clock import _SETTLE_TURNS

    clock = FakeClock()
    ticks: list[float] = []

    async def poller() -> None:
        for _ in range(2):
            await clock.sleep(1.0)
            ticks.append(clock.monotonic())
            for _ in range(_SETTLE_TURNS + 8):
                await asyncio.sleep(0)

    task = asyncio.create_task(poller())
    await asyncio.sleep(0)

    await clock.advance(10.0)

    assert ticks == [1.0], "beyond the settle window the old jump behaviour remains"
    assert clock.monotonic() == 10.0
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ------------------------------------------------------------ wall clock


async def test_wall_tracks_monotonic_and_stays_tz_aware() -> None:
    """Memory rows and subtitles are stamped from wall(). A naive datetime there is a bug."""
    start = datetime(2026, 6, 1, 20, 30, tzinfo=UTC)
    clock = FakeClock(wall=start)

    assert clock.wall() == start
    assert clock.wall().tzinfo is not None

    await clock.advance(60.0)

    assert clock.wall() - start == timedelta(seconds=60)
    assert clock.monotonic() == 60.0
    assert clock.wall().tzinfo is not None


def test_fake_wall_defaults_are_tz_aware_and_offset_by_start() -> None:
    """Boundary on the two constructor defaults.

    wall() is defined as an offset from the monotonic origin, so a non-zero `start`
    shifts it too. Pinned here so nobody later reads that as a bug.
    """
    assert FakeClock().wall().tzinfo is not None

    base = datetime(2026, 6, 1, tzinfo=UTC)
    assert FakeClock(start=10.0, wall=base).wall() == base + timedelta(seconds=10)


async def test_system_clock_measures_intervals_and_reports_utc() -> None:
    """Properties, not timings: a duration assertion here would flake on a loaded machine."""
    clock = SystemClock()

    first = clock.monotonic()
    await clock.sleep(0)
    second = clock.monotonic()

    assert isinstance(first, float)
    assert second >= first, "monotonic went backwards"

    now = clock.wall()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0), "wall() must report UTC, not local time"
    assert clock.wall() >= now
