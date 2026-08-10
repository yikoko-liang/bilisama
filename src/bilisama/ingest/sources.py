"""Event sources.

One ABC, not two. The reference implementations keep a pull-style EventSource and
a push-style IntentSource side by side, which forces a conversion partway down the
same pipeline. One shape does the job.

The `speak` switch only gates the last step. Events always land in memory,
subtitles and the event feed; all the switch decides is whether an Intent gets
produced. "Not speaking" is not the same as "not knowing" — the assistant still
knows this is Aqiang's fifth visit, it just says nothing about it this time.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bilisama.ingest.events import LiveEvent
from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.clock import Clock

log = get_logger(__name__)

EventSink = Callable[[LiveEvent], Awaitable[None]]


@runtime_checkable
class Source(Protocol):
    """Implemented by the live feed, the replay fixture, and later the background
    runner.

    Which ones get registered is decided once at assembly time. That is the entire
    mechanism behind "turn on another interaction level" — it adds data, never a
    branch.
    """

    name: str

    async def start(self, emit: EventSink) -> None:
        """Produce events until `stop()` is called."""
        ...

    async def stop(self) -> None: ...


class QueueSource:
    """Wraps an asyncio.Queue as a Source, for tests and in-process injection."""

    def __init__(self, name: str = "queue", *, maxsize: int = 256) -> None:
        self.name = name
        # Bounded on purpose: a slow consumer applies backpressure instead of
        # letting the queue grow until the process dies.
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._stopped = asyncio.Event()

    async def push(self, event: LiveEvent) -> None:
        await self._queue.put(event)

    async def start(self, emit: EventSink) -> None:
        while not self._stopped.is_set():
            event = await self._queue.get()
            if event is None:
                return
            await emit(event)

    async def stop(self) -> None:
        self._stopped.set()
        # Never wait for a slot here: a full queue is when shutdown matters most,
        # and setting _stopped has already removed the consumer that would free one.
        # The pill only has to travel when start() is parked in get(), which only
        # happens on an empty queue. If the queue is full, start() is awake and sees
        # _stopped on its next turn, so dropping the pill costs nothing. push() keeps
        # using put(), so backpressure is unchanged.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)


class SupervisedSource:
    """Restart a crashing source with backoff instead of letting it die.

    merge() runs sources in a TaskGroup, where one unhandled exception cancels
    every sibling — exactly wrong for a live stream (backlog item 9). Wrapped,
    a source gets `max_restarts` more chances with exponential backoff on the
    injected clock; after that it logs and returns CLEANLY, so the survivors
    keep running and the outage is a log line plus a health entry, not a crash.
    """

    # A run this long counts as healthy and refills the restart budget: the
    # cap is for crash LOOPS, not for a source that hiccups once a day and
    # would otherwise die permanently on day four (D2).
    HEALTHY_RUN_S = 60.0

    def __init__(
        self,
        inner: Source,
        clock: Clock,
        *,
        max_restarts: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._max_restarts = max_restarts
        self._backoff_s = backoff_s
        self.name = inner.name
        self.gave_up = False

    async def start(self, emit: EventSink) -> None:
        restarts = 0
        while True:
            began = self._clock.monotonic()
            try:
                await self._inner.start(emit)
                return  # a clean exit is a clean exit
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._clock.monotonic() - began >= self.HEALTHY_RUN_S:
                    restarts = 0
                if restarts >= self._max_restarts:
                    self.gave_up = True
                    log.error("source.gave_up", source=self.name, error_text=str(exc))
                    return
                restarts += 1
                delay = self._backoff_s * (2 ** (restarts - 1))
                log.warning(
                    "source.restarting",
                    source=self.name,
                    attempt=restarts,
                    backoff_s=delay,
                    error_text=str(exc),
                )
                await self._clock.sleep(delay)

    async def stop(self) -> None:
        await self._inner.stop()


async def merge(sources: list[Source], emit: EventSink) -> None:
    """Run several sources concurrently.

    Mind the TaskGroup semantics: if one source raises, the others are cancelled
    and the caller gets an ExceptionGroup. A live assembly therefore wraps each
    source in SupervisedSource first — a supervised crash never escapes — and
    that is what app.Assembly does.
    """
    async with asyncio.TaskGroup() as tg:
        for source in sources:
            tg.create_task(source.start(emit), name=f"source:{source.name}")


async def collect(source: Source, *, limit: int) -> list[LiveEvent]:
    """Collect the first N events from a source. Test helper."""
    out: list[LiveEvent] = []
    done = asyncio.Event()

    async def sink(event: LiveEvent) -> None:
        out.append(event)
        if len(out) >= limit:
            done.set()

    task = asyncio.create_task(source.start(sink))
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        await source.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return out
