"""QueueSource lifecycle, backpressure, and merge() semantics.

The queue is bounded on purpose, so every test here has to hold two things at
once: a slow consumer must still stall its producer, and shutdown must never
depend on the consumer being alive.
"""

from __future__ import annotations

import asyncio

import pytest

from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.ingest.sources import EventSink, QueueSource, collect, merge


def _queue_event(n: int) -> LiveEvent:
    return LiveEvent(kind=EventKind.DANMAKU, text="来了", event_id=f"q{n}")


class _ParkedSource:
    """Blocks in start() until something cancels it, and remembers that it was."""

    name = "parked"

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.cancelled = False

    async def start(self, emit: EventSink) -> None:
        self.running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stop(self) -> None:
        return None


class _BoomSource:
    """Raises once `after` fires, so the failure order is fixed, not a race."""

    name = "boom"

    def __init__(self, after: asyncio.Event) -> None:
        self._after = after

    async def start(self, emit: EventSink) -> None:
        await self._after.wait()
        raise RuntimeError("source died")

    async def stop(self) -> None:
        return None


# ------------------------------------------------------------ shutdown


async def test_stop_does_not_block_on_a_full_queue() -> None:
    """Backlog item 3: stop() used to await the poison pill onto a full queue.

    A full queue with nobody draining it is exactly when shutting down matters,
    and it was the one case where stop() could never return.
    """
    source = QueueSource(maxsize=2)
    await source.push(_queue_event(1))
    await source.push(_queue_event(2))
    # Nobody is consuming, so the queue stays full for the whole test.
    await asyncio.wait_for(source.stop(), timeout=1.0)


async def test_stop_wakes_a_start_parked_on_an_empty_queue() -> None:
    """The pill is load-bearing: _stopped alone cannot wake a parked get().

    Guard rail against 'fixing' the deadlock by deleting the pill — that would
    leave start() hanging on an empty queue forever.
    """
    source = QueueSource(maxsize=2)
    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    task = asyncio.create_task(source.start(sink))
    # One yield is enough to run the task up to its first get(), where it parks.
    await asyncio.sleep(0)

    await source.stop()

    await asyncio.wait_for(task, timeout=1.0)
    assert seen == []


async def test_start_emits_then_returns_on_the_poison_pill() -> None:
    """The normal shutdown path: drain what arrived, then return on the pill."""
    source = QueueSource(maxsize=4)
    seen: list[LiveEvent] = []
    both = asyncio.Event()

    async def sink(event: LiveEvent) -> None:
        seen.append(event)
        if len(seen) == 2:
            both.set()

    task = asyncio.create_task(source.start(sink))
    await source.push(_queue_event(1))
    await source.push(_queue_event(2))
    await asyncio.wait_for(both.wait(), timeout=1.0)

    await source.stop()

    await asyncio.wait_for(task, timeout=1.0)
    assert [e.event_id for e in seen] == ["q1", "q2"]


async def test_stop_is_idempotent_on_a_single_slot_queue() -> None:
    """Two stops on an idle source deadlocked before the fix, with no flood needed.

    The first pill filled the only slot, so the second put() waited for a
    consumer that had already been told to quit.
    """
    source = QueueSource(maxsize=1)
    await asyncio.wait_for(source.stop(), timeout=1.0)
    await asyncio.wait_for(source.stop(), timeout=1.0)

    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    # A source started after stop() must return instead of serving the backlog.
    await asyncio.wait_for(source.start(sink), timeout=1.0)
    assert seen == []


# ------------------------------------------------------------ backpressure


async def test_push_still_applies_backpressure() -> None:
    """maxsize exists so a slow consumer stalls its producer. Keep it that way.

    Cannot flake: the awaited push can never complete while the queue stays full.
    """
    source = QueueSource(maxsize=1)
    await source.push(_queue_event(1))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(source.push(_queue_event(2)), timeout=0.05)


# ------------------------------------------------------------ collect


async def test_collect_returns_the_first_events() -> None:
    source = QueueSource(maxsize=8)
    for n in (1, 2, 3):
        await source.push(_queue_event(n))

    events = await asyncio.wait_for(collect(source, limit=3), timeout=2.0)

    assert [e.event_id for e in events] == ["q1", "q2", "q3"]


async def test_collect_returns_while_a_producer_keeps_the_queue_full() -> None:
    """The real caller, in the shape that hung: flood, then shut down.

    collect() awaits stop() before it cancels the start task (sources.py:97-98),
    so a blocking stop() took collect() down with it. `>= 2` because collect()
    has always been allowed to overshoot its limit.
    """
    source = QueueSource(maxsize=4)

    async def flood() -> None:
        n = 0
        while True:
            n += 1
            await source.push(_queue_event(n))

    producer = asyncio.create_task(flood())
    try:
        events = await asyncio.wait_for(collect(source, limit=2), timeout=2.0)
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)

    assert len(events) >= 2


# ------------------------------------------------------------ merge


async def test_merge_runs_every_source_until_each_one_stops() -> None:
    left = QueueSource("left", maxsize=4)
    right = QueueSource("right", maxsize=4)
    seen: list[str] = []
    both = asyncio.Event()

    async def sink(event: LiveEvent) -> None:
        seen.append(event.event_id)
        if len(seen) == 2:
            both.set()

    task = asyncio.create_task(merge([left, right], sink))
    await left.push(_queue_event(1))
    await right.push(_queue_event(2))
    await asyncio.wait_for(both.wait(), timeout=1.0)

    await left.stop()
    await right.stop()

    await asyncio.wait_for(task, timeout=1.0)
    assert sorted(seen) == ["q1", "q2"]


async def test_merge_cancels_the_survivors_when_one_source_raises() -> None:
    """Pins today's TaskGroup semantics, which a live stream does not want.

    One dead source takes the rest down and the caller gets an ExceptionGroup.
    The supervising wrapper that changes this is backlog item 10; when it lands,
    this test should be rewritten on purpose rather than break by surprise.
    """
    parked = _ParkedSource()
    boom = _BoomSource(after=parked.running)

    async def sink(event: LiveEvent) -> None:
        raise AssertionError("neither source emits")

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await asyncio.wait_for(merge([parked, boom], sink), timeout=1.0)

    assert [type(exc) for exc in excinfo.value.exceptions] == [RuntimeError]
    assert parked.cancelled, "TaskGroup cancels the survivors — that is the point here"
