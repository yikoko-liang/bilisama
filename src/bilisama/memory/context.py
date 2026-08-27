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
from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.clock import Clock

__all__ = ["MemorySegments", "memory_segments"]

log = get_logger(__name__)


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
    """Who is here that has been here before, and what we know about them.

    The facts are the reason distillation runs at all, and until now they were
    written every stream and read by nothing — `scope="viewer"` had a writer
    (distill.py) and no reader, so 「阿强，第五次来了，上周送过舰长」 could name
    the person and never say the second half.

    Scoped to the people actually in the room, which is plan section 4.7's
    isolation rule and the cheap version of it: no query goes near a viewer who
    is not present, so one viewer's history cannot be answered to another.
    """
    parts: list[str] = []
    for viewer in store.present_regulars(limit=limit):
        who = f"{viewer.uname or viewer.identity}（第 {viewer.streams_seen} 次来）"
        known = "；".join(fact.text for fact in store.facts("viewer", viewer.identity))
        parts.append(f"{who}：{known}" if known else who)
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
    segments = MemorySegments(
        streamer_facts=streamer_facts_text(store),
        session_progress=session_progress_text(store),
        regulars=regulars_line(store),
        clock_line=clock_line(store, clock, granularity_min=clock_granularity_min),
    )
    # Sizes only for the three memory-fed segments: their text is distilled
    # FROM audience danmaku, so it belongs to the audience the same way the
    # danmaku does. The clock line is ours end to end and goes in verbatim —
    # it is the one segment where the value itself is the bug ("开播 0 分钟"
    # three hours in means stream_started_at never moved).
    #
    # Debug for the same reason as persona.prompt_assembled: once per rebuild,
    # not once per push.
    log.debug(
        "memory.segments_built",
        streamer_fact_chars=len(segments.streamer_facts),
        progress_chars=len(segments.session_progress),
        regulars_chars=len(segments.regulars),
        clock_line=segments.clock_line,
    )
    return segments
