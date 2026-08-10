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


def _uptime_phrase(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    if hours:
        return f"开播 {hours} 小时 {mins} 分"
    return f"开播 {mins} 分钟"


def clock_line(store: MemoryStore, clock: Clock) -> str:
    started = store.stream_started_at()
    if started is None:
        return ""
    now = clock.wall()
    minutes = int((now - started).total_seconds() // 60)
    # China time, same zone the 04:00 day boundary uses — one clock line must
    # not mix two zones (B4). wall() itself stays UTC in rows.
    hhmm = now.astimezone(STREAM_TZ).strftime("%H:%M")
    return f"{_uptime_phrase(minutes)}，现在 {hhmm}，本周第 {store.streams_this_week()} 场"


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


def memory_segments(store: MemoryStore, clock: Clock) -> MemorySegments:
    return MemorySegments(
        streamer_facts=streamer_facts_text(store),
        session_progress=session_progress_text(store),
        regulars=regulars_line(store),
        clock_line=clock_line(store, clock),
    )
