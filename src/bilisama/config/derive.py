"""Thresholds derived from the chattiness setting.

These numbers are deliberately absent from the TOML file. If the config
pinned `window_s` and the slider also moved it, nothing would define which
wins. The slider is the single writer; this table is the only mapping.

Two of the five have grown second writers with their own precedence, made
explicit by `effective_thresholds` instead of a hidden table in dev-talk:
the danmaku window now comes from the event pacer (chattiness × measured
room load), and max_output_tokens from the independent reply_length slider.
The base table remains what `bilisama config chattiness` prints.
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
        score_threshold=0.5,
        cooldown_s=20,
        max_output_tokens=45,
    ),
    Chattiness.MEDIUM: DerivedThresholds(
        idle_threshold_s=90,
        danmaku_window_s=20,
        score_threshold=0.3,
        cooldown_s=12,
        max_output_tokens=120,
    ),
    Chattiness.HIGH: DerivedThresholds(
        idle_threshold_s=45,
        danmaku_window_s=12,
        score_threshold=0.15,
        cooldown_s=5,
        max_output_tokens=120,
    ),
}

# What the reply_length slider is worth in tokens, independent of chattiness.
# LOW is one short sentence, MEDIUM one or two, HIGH up to four — the persona
# templates carry the matching prose ({{replyLength}}).
_REPLY_TOKENS: dict[Chattiness, int] = {
    Chattiness.LOW: 45,
    Chattiness.MEDIUM: 120,
    Chattiness.HIGH: 180,
}


def derive(chattiness: Chattiness) -> DerivedThresholds:
    return _CHATTINESS_TABLE[chattiness]


def effective_thresholds(
    chattiness: Chattiness,
    *,
    reply_length: Chattiness,
    danmaku_window_s: int,
) -> DerivedThresholds:
    """The runtime row after the independent controls have spoken.

    Args:
        chattiness: The base slider; supplies everything not overridden.
        reply_length: The length slider; owns max_output_tokens.
        danmaku_window_s: The event pacer's current window; owns the field of
            the same name. Passed in rather than read here — the pacer is
            runtime state and this module stays pure.
    """
    base = derive(chattiness)
    return base.model_copy(
        update={
            "danmaku_window_s": danmaku_window_s,
            "max_output_tokens": _REPLY_TOKENS[reply_length],
        }
    )
