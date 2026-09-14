"""Consume same-response control reports without delaying or regenerating speech."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bilisama.clock import Clock
from bilisama.director.interaction_state import REPORT_NAME, InteractionReport, parse_report
from bilisama.obs.logging import get_logger
from bilisama.realtime import link

log = get_logger(__name__)


@dataclass(slots=True)
class _Turn:
    received: bool = False
    reported: bool = False
    done: bool = False


class InteractionReports:
    """Observe protocol metadata; all semantic judgments remain model outputs."""

    def __init__(
        self,
        speech: link.ToolReportingLink,
        clock: Clock,
        *,
        apply: Callable[[InteractionReport, float | None], None],
        refresh_context: Callable[[], Awaitable[None]],
        notice: Callable[[str], None],
    ) -> None:
        self._speech = speech
        self._clock = clock
        self._apply = apply
        self._refresh_context = refresh_context
        self._notice = notice
        self.input_generation = 0
        self._voice_started_at: float | None = None
        self._session_id = ""
        self._turns: OrderedDict[int, _Turn] = OrderedDict()
        self._retired_handles: OrderedDict[int, None] = OrderedDict()
        self._retired_sessions: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._context_task: asyncio.Task[None] | None = None
        self._context_dirty = False
        self._closed = False
        self._counts = {
            "applied": 0,
            "without_report": 0,
            "invalid": 0,
            "stale": 0,
            "ack_failed": 0,
        }

    def status(self) -> dict[str, int | bool]:
        return {"enabled": not self._closed, **self._counts, "pending_acks": len(self._tasks)}

    def _turn(self, handle: link.ReplyHandle) -> _Turn:
        turn = self._turns.setdefault(handle.handle_id, _Turn())
        self._turns.move_to_end(handle.handle_id)
        while len(self._turns) > 256:
            old, _ = self._turns.popitem(last=False)
            self._retired_handles[old] = None
        self._trim_retired()
        return turn

    def _trim_retired(self) -> None:
        while len(self._retired_handles) > 512:
            self._retired_handles.popitem(last=False)
        while len(self._retired_sessions) > 32:
            self._retired_sessions.popitem(last=False)

    def _current(self, handle: link.ReplyHandle) -> bool:
        return (
            not self._closed
            and not handle.stale
            and handle.handle_id not in self._retired_handles
            and handle.input_generation is not None
            and handle.input_generation == self.input_generation
        )

    def observe(self, event: link.LinkEvent) -> None:
        """Never await a write here: the receive loop must reach response.done."""
        if self._closed:
            return
        if isinstance(event, link.SpeechStarted):
            self.input_generation += 1
            self._voice_started_at = self._clock.monotonic()
        elif isinstance(event, link.LinkDown):
            self._invalidate()
        elif isinstance(event, link.ReplyStarted):
            if self._current(event.handle):
                self._turn(event.handle)
        elif isinstance(event, link.ToolCall) and event.name == REPORT_NAME:
            self._report(event)
        elif isinstance(event, link.ReplyDone) and self._current(event.handle):
            turn = self._turn(event.handle)
            if not turn.done and not turn.received and event.status is link.ReplyStatus.COMPLETED:
                # No call is normal when no background state needs changing.
                # This is a neutral count, not a semantic failure detector.
                self._counts["without_report"] += 1
            turn.done = True

    def _report(self, call: link.ToolCall) -> None:
        if (
            not self._current(call.handle)
            or not call.call_id
            or not call.session_id
            or call.session_id in self._retired_sessions
            or (self._session_id and call.session_id != self._session_id)
        ):
            self._counts["stale"] += 1
            return
        self._session_id = call.session_id
        turn = self._turn(call.handle)
        if turn.reported or turn.done:
            return
        turn.received = True
        try:
            report = parse_report(call.arguments)
            self._apply(report, self._voice_started_at if call.handle.implicit else None)
        except ValueError as exc:
            self._counts["invalid"] += 1
            log.warning(
                "interaction.report_invalid", handle_id=call.handle.handle_id, error_text=str(exc)
            )
            self._notice(str(exc))
            self._queue_ack(
                call, "报告未接受，状态未更新；请继续按原回复协议，不另生成回复。", accepted=False
            )
            return
        turn.reported = True
        self._counts["applied"] += 1
        log.info(
            "interaction.report_applied",
            handle_id=call.handle.handle_id,
            event_count=len(report.events),
        )
        self._queue_ack(call, "状态已记录，不需要另生成回复。")
        self._queue_refresh()

    def _queue_ack(self, call: link.ToolCall, result: str, *, accepted: bool = True) -> None:
        if len(self._tasks) >= 8:
            self._counts["ack_failed"] += 1
            return
        output = json.dumps({"accepted": accepted, "message": result}, ensure_ascii=False)
        task = asyncio.create_task(self._ack(call, output), name="interaction:ack")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        # A provider-specific exception must be observed here without adding
        # protocol imports or letting a background writer kill the receiver.
        if task.get_name() == "interaction:context":
            log.error("interaction.context_failed", error_text=str(error)[:200])
        else:
            self._counts["ack_failed"] += 1
            log.error("interaction.ack_failed", error_text=str(error)[:200])

    async def _ack(self, call: link.ToolCall, result: str) -> None:
        try:
            async with asyncio.timeout(30):
                await self._speech.submit_tool_result(call, result)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            self._counts["ack_failed"] += 1
            log.warning("interaction.ack_failed", error_text=str(exc)[:200])

    def _queue_refresh(self) -> None:
        self._context_dirty = True
        if self._context_task is None or self._context_task.done():
            self._context_task = asyncio.create_task(self._refresh(), name="interaction:context")
            self._context_task.add_done_callback(self._task_done)

    async def _refresh(self) -> None:
        try:
            while self._context_dirty:
                self._context_dirty = False
                async with asyncio.timeout(30):
                    await self._refresh_context()
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            log.warning("interaction.context_failed", error_text=str(exc)[:200])

    def _invalidate(self) -> None:
        self._retired_handles.update((handle_id, None) for handle_id in self._turns)
        if self._session_id:
            self._retired_sessions[self._session_id] = None
        self._trim_retired()
        self._turns.clear()
        self._session_id = ""
        self.input_generation = 0
        self._voice_started_at = None
        self._context_dirty = False
        for task in self._tasks:
            task.cancel()
        if self._context_task is not None:
            self._context_task.cancel()

    async def drain(self) -> None:
        tasks = list(self._tasks)
        if self._context_task is not None:
            tasks.append(self._context_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reset(self) -> None:
        tasks = list(self._tasks)
        if self._context_task is not None:
            tasks.append(self._context_task)
        self._invalidate()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._context_task = None

    async def aclose(self) -> None:
        self._closed = True
        await self.reset()
