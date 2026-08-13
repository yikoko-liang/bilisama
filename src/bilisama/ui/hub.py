"""Broadcast hub between the director wiring and UI clients.

The hub never imports director or realtime classes — dev-talk hands it plain
callables and already-translated frames. Three delivery rules, each covering a
way a browser can hurt the voice loop:

1. Per-client bounded queues, drop-oldest on overflow — the same discipline as
   dev-talk's _Fanout. A wedged tab loses frames; the loop never blocks.
2. Sticky state plus replay rings: a client that connects late (or reconnects)
   gets the newest voice.state/panel.state and the recent feed/log history, so
   the panel is never blank and never stale.
3. Log lines arrive from arbitrary threads (PortAudio callbacks log too), so
   the handler only appends to a deque; the run() tick moves them onto the
   asyncio side where put_nowait is legal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from bilisama.clock import Clock
from bilisama.ui.events import ServerEvent, frame

__all__ = ["UiHub", "VoiceSignals", "resolve_voice_state"]

# Sticky events: only the latest frame matters, and every client must have it.
_STICKY = (ServerEvent.VOICE_STATE, ServerEvent.PANEL_STATE)


@dataclass(frozen=True, slots=True)
class VoiceSignals:
    """The five observable inputs of the voice-state arbiter.

    All five are polled, not pushed — the floor has no change callbacks and
    growing them for a preview would touch L3 for display's sake.
    """

    streamer_speaking: bool  # floor.streamer_speaking
    dispatching: bool  # scheduler.status()["dispatching"]
    active: bool  # scheduler.status()["active_source"] is not None
    implicit: bool  # floor.implicit_active
    audio_busy: bool  # the local speaker is actually making sound


def resolve_voice_state(signals: VoiceSignals) -> str:
    """Fold the five signals into the one string the pet animates on.

    Priority order mirrors qwen-audio-agent's orb arbiter: listening wins
    (barge-in feedback must be instant), then speaking (sound is observable
    truth), then thinking (anything in flight), else idle.
    """
    if signals.streamer_speaking:
        return "listening"
    if signals.audio_busy:
        return "speaking"
    if signals.dispatching or signals.active or signals.implicit:
        return "thinking"
    return "idle"


class _StagingHandler(logging.Handler):
    """Appends formatted lines to the hub's staging deque, from any thread."""

    def __init__(self, staging: deque[str]) -> None:
        super().__init__()
        self._staging = staging

    def emit(self, record: logging.LogRecord) -> None:
        # The stdlib contract: a handler must never let an exception escape
        # emit(), whatever the record. handleError honours logging.raiseExceptions.
        try:
            self._staging.append(self.format(record))
        except Exception:
            self.handleError(record)


class UiHub:
    """Fan-out point for everything the pet page and the panel display.

    All methods except the log handler run on the event loop. broadcast() is
    synchronous so it can sit inside existing sync closures (verdict_sink,
    handle_line) without ceremony.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        queue_max: int = 256,
        feed_keep: int = 300,
        log_keep: int = 400,
    ) -> None:
        self._clock = clock
        self._queue_max = queue_max
        self._clients: list[asyncio.Queue[str | None]] = []
        self._sticky: dict[ServerEvent, str] = {}
        self._feed_ring: deque[str] = deque(maxlen=feed_keep)
        self._log_ring: deque[str] = deque(maxlen=log_keep)
        # Written by the logging handler from arbitrary threads; drained on the
        # loop by run(). deque.append is atomic under the GIL, which is the
        # whole reason this is a deque and not a Queue.
        self._log_staging: deque[str] = deque(maxlen=log_keep)
        self._log_handler = _StagingHandler(self._log_staging)
        self._closed = False

    # ------------------------------------------------------------ clients

    def attach(self) -> tuple[list[str], asyncio.Queue[str | None]]:
        """Register a client.

        Returns:
            (replay, queue): frames to send first — newest sticky states, then
            the feed and log history — and the live queue. A None from the
            queue means the hub closed; the reader should hang up.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self._queue_max)
        replay = [self._sticky[event] for event in _STICKY if event in self._sticky]
        replay.extend(self._feed_ring)
        replay.extend(self._log_ring)
        if self._closed:
            # A connection that squeezed in between aclose() and the server
            # actually stopping would otherwise wait on a queue nobody feeds
            # until the shutdown axe falls; hand it the hang-up straight away.
            queue.put_nowait(None)
            return replay, queue
        self._clients.append(queue)
        return replay, queue

    def detach(self, queue: asyncio.Queue[str | None]) -> None:
        with contextlib.suppress(ValueError):
            self._clients.remove(queue)

    @property
    def clients(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------ delivery

    def broadcast(self, event: ServerEvent, data: Mapping[str, Any]) -> None:
        """Deliver one frame to every client, stamping it with wall time.

        Never blocks and never raises on a slow client: a full queue drops its
        oldest frame. Sticky and ring events are recorded even with no client
        attached, so history exists before the first tab opens.
        """
        if self._closed:
            return
        payload = dict(data)
        # Local time, not UTC: log.line rows carry the formatter's local
        # timestamps, and one panel showing two clocks eight hours apart
        # reads as broken.
        payload.setdefault("ts", self._clock.wall().astimezone().isoformat(timespec="seconds"))
        line = frame(event, payload)
        if event in _STICKY:
            self._sticky[event] = line
        elif event is ServerEvent.EVENT_FEED:
            self._feed_ring.append(line)
        elif event is ServerEvent.LOG_LINE:
            self._log_ring.append(line)
        for queue in self._clients:
            self._offer(queue, line)

    @staticmethod
    def _offer(queue: asyncio.Queue[str | None], line: str) -> None:
        try:
            queue.put_nowait(line)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(line)

    # ------------------------------------------------------------ logging

    @property
    def log_handler(self) -> logging.Handler:
        """Hand this to obs.logging.setup(extra_handlers=...); it formats and
        stages lines, and run() turns them into log.line frames."""
        return self._log_handler

    def _drain_logs(self) -> None:
        while self._log_staging:
            self.broadcast(ServerEvent.LOG_LINE, {"line": self._log_staging.popleft()})

    # ------------------------------------------------------------ state loop

    async def run(self, read: Callable[[], VoiceSignals], *, poll_s: float = 0.1) -> None:
        """Poll the voice signals and ship state changes; drain staged logs.

        100ms is a display cadence, not a control one — nothing in the voice
        loop waits on this. Only changes are broadcast, so an idle stream costs
        one comparison per tick and zero frames.
        """
        last = ""
        while True:
            self._drain_logs()
            state = resolve_voice_state(read())
            if state != last:
                last = state
                self.broadcast(ServerEvent.VOICE_STATE, {"state": state})
            await self._clock.sleep(poll_s)

    # ------------------------------------------------------------ shutdown

    async def aclose(self) -> None:
        """Tell every client to hang up.

        Must run before the HTTP server's graceful stop: uvicorn waits for open
        connections, and a browser keeps its WebSocket open forever unless the
        reader sees the None sentinel and closes from our side.
        """
        self._closed = True
        for queue in self._clients:
            self._offer_sentinel(queue)
        self._clients.clear()
        await asyncio.sleep(0)

    @staticmethod
    def _offer_sentinel(queue: asyncio.Queue[str | None]) -> None:
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
