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
from typing import Protocol, runtime_checkable

from bilisama.ingest.events import LiveEvent

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


async def merge(sources: list[Source], emit: EventSink) -> None:
    """Run several sources concurrently.

    Mind the TaskGroup semantics: if one source raises, the others are cancelled
    and the caller gets an ExceptionGroup. Restarting is the caller's problem.

    A live stream really wants the opposite — one dead source should not take the
    rest down — which needs a supervising wrapper per source. Deferred until there
    is more than one source to supervise (backlog item 10, with tests).
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
