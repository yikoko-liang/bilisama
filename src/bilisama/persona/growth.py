"""Growth-layer merge policy: hard budgets, slow swap rate.

Pure functions on purpose — the distiller calls them and logs what got
dropped; the store writes the result. Every constant here is an anti-drift
measure from plan section 4.6: budgets keep the layers bounded, the per-stream
swap cap gives the voice its inertia (style should creep, not lurch).
"""

from __future__ import annotations

import re

__all__ = [
    "RELATIONSHIP_MAX_CHARS",
    "RELATIONSHIP_MAX_ENTRIES",
    "VOICE_MAX_CHARS",
    "VOICE_MAX_LINES",
    "VOICE_SWAPS_PER_STREAM",
    "merge_relationship",
    "merge_voice",
]

RELATIONSHIP_MAX_ENTRIES = 30
RELATIONSHIP_MAX_CHARS = 800

VOICE_MAX_LINES = 12
VOICE_MAX_CHARS = 400
# At most this many new exemplar lines may enter per stream. The whole point
# of exemplars is a recognisable voice; letting a single stream replace the
# box would make the assistant sound like someone else overnight.
VOICE_SWAPS_PER_STREAM = 2


def _trim_oldest(entries: list[str], *, max_entries: int, max_chars: int) -> list[str]:
    # Oldest out first, never a silent mid-entry truncation (plan section 4.7:
    # budgets drop whole items with a warning, they do not shorten them).
    trimmed = list(entries)
    while trimmed and (len(trimmed) > max_entries or sum(len(e) for e in trimmed) > max_chars):
        trimmed.pop(0)
    return trimmed


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} ")


def _content(entry: str) -> str:
    """The entry minus its logical-date prefix — the dedup key.

    The same fact re-extracted on a later stream carries a different date, so
    comparing full strings let duplicates churn the whole budget (B3).
    """
    return _DATE_PREFIX.sub("", entry)


def merge_relationship(existing: list[str], fresh: list[str]) -> list[str]:
    """Append new shared-history entries, oldest out over budget. Dedup is by
    content, ignoring the date prefix."""
    merged = list(existing)
    seen = {_content(entry) for entry in merged}
    for entry in fresh:
        if entry and _content(entry) not in seen:
            merged.append(entry)
            seen.add(_content(entry))
    return _trim_oldest(
        merged, max_entries=RELATIONSHIP_MAX_ENTRIES, max_chars=RELATIONSHIP_MAX_CHARS
    )


def merge_voice(
    existing: list[str], candidates: list[str], *, max_swaps: int = VOICE_SWAPS_PER_STREAM
) -> list[str]:
    """Let at most `max_swaps` new exemplar lines in, oldest out over budget.

    Candidates beyond the swap cap are simply dropped — they had their chance
    this stream and better ones may come next stream.
    """
    fresh = [line for line in candidates if line and line not in existing]
    merged = list(existing)
    merged.extend(fresh[:max_swaps])
    return _trim_oldest(merged, max_entries=VOICE_MAX_LINES, max_chars=VOICE_MAX_CHARS)
