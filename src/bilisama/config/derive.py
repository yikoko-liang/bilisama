"""Thresholds derived from the chattiness setting.

The base table remains the CLI-visible product tuning. The control centre can
override reply length and the danmaku collection window independently; the
effective helper makes that precedence explicit instead of growing a second
hidden table inside dev-talk.
"""

from __future__ import annotations

from pydantic import BaseModel

from bilisama.config.enums import Chattiness


class DerivedThresholds(BaseModel):
    """The five numbers chattiness derives.

    Absent from the TOML on purpose: if the file pinned one and the slider also
    moved it, nothing would define which wins. `bilisama config show` marks these
    as derived.

    Frozen because `derive()` hands out the table row itself rather than a copy.
    Without this, one stray assignment anywhere rewrites the mapping for the whole
    process, and `config show` then reports the corrupted number as derived truth —
    a second writer, which is the exact thing this module exists to prevent.
    """

    model_config = {"frozen": True}

    idle_threshold_s: int
    danmaku_window_s: int
    score_threshold: float
    cooldown_s: int
    max_output_tokens: int


_CHATTINESS_TABLE: dict[Chattiness, DerivedThresholds] = {
    Chattiness.LOW: DerivedThresholds(
        idle_threshold_s=180,
        danmaku_window_s=30,
        score_threshold=0.55,
        cooldown_s=20,
        max_output_tokens=45,
    ),
    Chattiness.MEDIUM: DerivedThresholds(
        idle_threshold_s=90,
        danmaku_window_s=20,
        score_threshold=0.35,
        cooldown_s=12,
        max_output_tokens=120,
    ),
    Chattiness.HIGH: DerivedThresholds(
        idle_threshold_s=45,
        danmaku_window_s=12,
        score_threshold=0.2,
        cooldown_s=5,
        max_output_tokens=120,
    ),
}


def derive(chattiness: Chattiness) -> DerivedThresholds:
    return _CHATTINESS_TABLE[chattiness]


def effective_thresholds(
    chattiness: Chattiness,
    *,
    reply_length: Chattiness,
    danmaku_window_s: int,
) -> DerivedThresholds:
    """Return the runtime row after applying independent UI controls."""
    base = derive(chattiness)
    reply_tokens = {
        Chattiness.LOW: 45,
        Chattiness.MEDIUM: 120,
        Chattiness.HIGH: 180,
    }
    return base.model_copy(
        update={
            "danmaku_window_s": danmaku_window_s,
            "max_output_tokens": reply_tokens[reply_length],
        }
    )
