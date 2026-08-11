"""Turn memory rows into the dynamic-tail segments.

Strings only — persona/prompt.py owns the assembly, this module owns what the
segments say. The clock line speaks stream time ("开播 1 小时 47 分"), not
wall-clock prose: the tail is re-pushed on change anyway, so unlike
openhanako's session-start snapshot, this clock actually moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bilisama.memory.store import STREAM_TZ, MemoryStore

if TYPE_CHECKING:
    from bilisama.clock import Clock

__all__ = ["MemorySegments", "memory_segments"]


@dataclass(frozen=True, slots=True)
class MemorySegments:
    """The memory-owned slice of persona.prompt.DynamicContext."""

    streamer_facts: str = ""
    session_progress: str = ""
    regulars: str = ""
    clock_line: str = ""


def _uptime_phrase(minutes: int, *, about: bool) -> str:
    prefix = "开播约" if about else "开播"
    hours, mins = divmod(max(minutes, 0), 60)
    if hours:
        return f"{prefix} {hours} 小时 {mins} 分"
    return f"{prefix} {mins} 分钟"


def clock_line(store: MemoryStore, clock: Clock, *, granularity_min: int = 1) -> str:
    """The time segment of the dynamic tail.

    granularity_min floors both numbers to that many minutes. The point is
    push cadence, not display: the assembled tail is re-pushed whenever its
    text changes, so a minute-precision clock forces one session.update per
    minute. At the default 5 the same push happens a fifth as often, and the
    wording turns approximate (「开播约」「左右」) so the model does not quote
    a floored value as exact.
    """
    started = store.stream_started_at()
    if started is None:
        return ""
    now = clock.wall()
    step = max(granularity_min, 1)
    minutes = int((now - started).total_seconds() // 60) // step * step
    # China time, same zone the 04:00 day boundary uses — one clock line must
    # not mix two zones (B4). wall() itself stays UTC in rows.
    local = now.astimezone(STREAM_TZ)
    hhmm = local.replace(minute=local.minute // step * step).strftime("%H:%M")
    about = step > 1
    tail = " 左右" if about else ""
    uptime = _uptime_phrase(minutes, about=about)
    return f"{uptime}，现在 {hhmm}{tail}，本周第 {store.streams_this_week()} 场"


def regulars_line(store: MemoryStore, *, limit: int = 5) -> str:
    parts = [
        f"{v.uname or v.identity}（第 {v.streams_seen} 次来）"
        for v in store.present_regulars(limit=limit)
    ]
    return "、".join(parts)


def streamer_facts_text(store: MemoryStore) -> str:
    return "\n".join(f"- {fact.text}" for fact in store.facts("streamer"))


def session_progress_text(store: MemoryStore) -> str:
    """The rolling ≤200-char summary the distiller maintains, keyed by stream."""
    rows = store.facts("stream", str(store.stream_id))
    return rows[-1].text if rows else ""


def memory_segments(
    store: MemoryStore, clock: Clock, *, clock_granularity_min: int = 1
) -> MemorySegments:
    return MemorySegments(
        streamer_facts=streamer_facts_text(store),
        session_progress=session_progress_text(store),
        regulars=regulars_line(store),
        clock_line=clock_line(store, clock, granularity_min=clock_granularity_min),
    )
