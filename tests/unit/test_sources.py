"""QueueSource lifecycle, backpressure, and merge() semantics.

The queue is bounded on purpose, so every test here has to hold two things at
once: a slow consumer must still stall its producer, and shutdown must never
depend on the consumer being alive.

Which makes the poison pill best-effort and `_stopped` the authority. The pill
only has to travel on an empty queue; on a full one it is dropped, and start()
gets out on the flag alone. Both halves are pinned below, because the bug this
module has now had twice is a shutdown that hangs on whichever half went untested.
"""

from __future__ import annotations

import asyncio

import pytest

from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.ingest.sources import EventSink, QueueSource, collect, merge

# Spelled out rather than imported from the source: a test that reads the default
# back out of QueueSource would be self-consistent with any default at all.
_DEFAULT_MAXSIZE = 256


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


async def test_start_returns_when_stop_had_to_drop_the_pill() -> None:
    """The other half of the stop() fix, and the load-bearing one.

    The comment at sources.py:66-73 argues that dropping the pill on a full queue
    costs nothing *because* start() re-reads _stopped on its next turn. Delete that
    read (`while True:`) and stop() still returns, so every other test here stays
    green — while start() drains the backlog and then parks in get() forever, with
    nobody left to feed it a pill. The hang just moves from stop() to start().
    """
    source = QueueSource(maxsize=2)
    emitting = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_sink(event: LiveEvent) -> None:
        seen.append(event.event_id)
        emitting.set()
        await release.wait()

    task = asyncio.create_task(source.start(slow_sink))
    await source.push(_queue_event(1))
    await asyncio.wait_for(emitting.wait(), timeout=1.0)

    # The consumer is parked inside emit, so both slots are ours. These two pushes
    # returning is itself the proof that the queue is now full: maxsize is 2.
    await asyncio.wait_for(source.push(_queue_event(2)), timeout=1.0)
    await asyncio.wait_for(source.push(_queue_event(3)), timeout=1.0)

    await asyncio.wait_for(source.stop(), timeout=1.0)
    release.set()

    await asyncio.wait_for(task, timeout=1.0)
    # Abandoned, not drained: stop() means stop, and the two queued events are the
    # ones the flag check has to skip for the dropped pill to be harmless.
    assert seen == ["q1"]


async def test_start_after_stop_abandons_a_queued_backlog() -> None:
    """Same flag check, seen from the ordering a supervisor actually produces.

    An in-process producer can fill the queue before the source task ever gets its
    first turn, so the pill lands *behind* real events. A start() that trusted the
    pill alone would emit the whole backlog on the way out — the events the
    supervisor already decided nobody wants.
    """
    source = QueueSource(maxsize=8)
    for n in (1, 2, 3):
        await source.push(_queue_event(n))
    await asyncio.wait_for(source.stop(), timeout=1.0)

    seen: list[str] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event.event_id)

    await asyncio.wait_for(source.start(sink), timeout=1.0)
    assert seen == []


async def test_push_after_stop_is_accepted_and_never_delivered() -> None:
    """A danmaku reader keeps arriving for a while after the supervisor says stop.

    push() stays a plain put(), so the producer sees no new exception on the way
    down — it is cancelled with the rest of the task group instead. What it pushes
    is dropped, because nothing consumes after stop().
    """
    source = QueueSource(maxsize=4)
    await asyncio.wait_for(source.stop(), timeout=1.0)

    await asyncio.wait_for(source.push(_queue_event(1)), timeout=1.0)

    seen: list[str] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event.event_id)

    await asyncio.wait_for(source.start(sink), timeout=1.0)
    assert seen == []


async def test_push_on_a_full_queue_after_stop_waits_to_be_cancelled() -> None:
    """The one part of shutdown that is not self-contained, pinned so it stays a
    deliberate choice.

    After stop() nothing drains the queue, so a producer already parked in push()
    can only be freed by cancellation — which is exactly what both callers do
    (merge() runs sources in a TaskGroup, collect() cancels at sources.py:106).
    Cannot flake: a full queue with no consumer can never free a slot. If push()
    ever has to fail fast instead, this is the test to rewrite on purpose.
    """
    source = QueueSource(maxsize=1)
    await source.push(_queue_event(1))
    await asyncio.wait_for(source.stop(), timeout=1.0)

    producer = asyncio.create_task(source.push(_queue_event(2)))
    _done, pending = await asyncio.wait({producer}, timeout=0.05)
    assert producer in pending, "push() cannot complete while the queue stays full"

    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer


async def test_cancelling_a_parked_start_propagates_the_cancellation() -> None:
    """collect() (sources.py:106) and merge()'s TaskGroup both shut a source down by
    cancelling start(); a QueueSource that swallowed CancelledError would hang both.
    """
    source = QueueSource(maxsize=2)

    async def sink(event: LiveEvent) -> None:
        raise AssertionError("nothing is ever pushed")

    task = asyncio.create_task(source.start(sink))
    # One yield is enough to run the task up to its first get(), where it parks.
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ------------------------------------------------------------ backpressure


async def test_push_still_applies_backpressure() -> None:
    """maxsize exists so a slow consumer stalls its producer. Keep it that way.

    Cannot flake: the awaited push can never complete while the queue stays full.
    """
    source = QueueSource(maxsize=1)
    await source.push(_queue_event(1))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(source.push(_queue_event(2)), timeout=0.05)


async def test_the_default_bound_is_the_documented_one() -> None:
    """The test above passes its own maxsize, so the default was pinned nowhere.

    Both directions matter and the default is what every real caller gets: 1 would
    serialise the ingest path behind one in-flight danmaku, and a huge value would
    quietly remove the "instead of letting the queue grow until the process dies"
    protection the comment at sources.py:49-50 promises.
    """
    source = QueueSource()

    async def fill_to_the_brim() -> None:
        for n in range(_DEFAULT_MAXSIZE):
            await source.push(_queue_event(n))

    await asyncio.wait_for(fill_to_the_brim(), timeout=2.0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(source.push(_queue_event(_DEFAULT_MAXSIZE)), timeout=0.05)


# ------------------------------------------------------------ collect


async def test_collect_returns_the_first_events() -> None:
    source = QueueSource(maxsize=8)
    for n in (1, 2, 3):
        await source.push(_queue_event(n))

    events = await asyncio.wait_for(collect(source, limit=3), timeout=2.0)

    assert [e.event_id for e in events] == ["q1", "q2", "q3"]


async def test_collect_returns_while_a_producer_keeps_the_queue_full() -> None:
    """The real caller, in the shape that hung: flood, then shut down.

    collect() awaits stop() before it cancels the start task (sources.py:105-106),
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
