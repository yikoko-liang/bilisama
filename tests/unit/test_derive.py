"""The chattiness slider: what it actually derives.

`derive()` is the single writer of five thresholds that are deliberately absent
from the TOML (plan §2.7, restated at src/bilisama/config/derive.py:3-6):
idle_threshold_s, danmaku_window_s, score_threshold, cooldown_s and
max_output_tokens. Nothing downstream re-derives them, so a wrong row here is the
exact user-visible failure the module exists to answer — "why is it talking this
much?" — and there is no second source of truth to disagree with it and expose it.

These tests deliberately never compare `derive()` against `derive()`. A test shaped
that way passes for *any* self-consistent table, including one where `derive`
ignores its argument entirely and hands back the same row every time. That
mutation used to survive the whole suite. What is pinned here instead is the
contract: which way each threshold moves across the three levels, that the three
levels differ at all, that every level has a row, that an unmapped level fails
loudly — and, separately and on purpose, the tuned literals.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from bilisama.config import Chattiness, DerivedThresholds, Settings, derive

# Semantic order, quietest to chattiest. Chattiness is a StrEnum, so its own
# ordering is alphabetical and says nothing about how talkative a level is; every
# direction assertion below reads off this tuple instead.
_ASCENDING: tuple[Chattiness, ...] = (Chattiness.LOW, Chattiness.MEDIUM, Chattiness.HIGH)

# The four thresholds a chattier setting makes it easier to clear: shorter wait
# before the assistant breaks a silence, shorter danmaku window, lower score bar,
# shorter gate cooldown. Plan §2.7's table reads, low to high: 180/90/45s,
# 30/20/12s, a score bar rated high/medium/low, 20/12/5s. Reply length is the odd
# one out and gets its own test.
_LOWERED_BY_CHATTINESS: tuple[str, ...] = (
    "idle_threshold_s",
    "danmaku_window_s",
    "score_threshold",
    "cooldown_s",
)

_THRESHOLD_FIELDS: frozenset[str] = frozenset(
    {*_LOWERED_BY_CHATTINESS, "max_output_tokens"},
)


def _across_levels(field: str) -> list[float]:
    """One threshold read at every level, quietest first."""
    return [float(getattr(derive(level), field)) for level in _ASCENDING]


# ------------------------------------------------------------------ the contract


def test_the_ascending_order_covers_every_level() -> None:
    """Guards the fixture the direction tests are built on.

    A fourth chattiness level that nobody adds here would otherwise be silently
    exempt from every assertion in this file.
    """
    assert set(_ASCENDING) == set(Chattiness)
    assert len(_ASCENDING) == len(Chattiness)


def test_every_level_derives_a_row() -> None:
    """Normal path: the table is total over the enum.

    Adding a level without a row raises KeyError, and this is where that surfaces
    — at test time, not the first time a streamer drags the slider onto it.
    """
    for level in Chattiness:
        assert isinstance(derive(level), DerivedThresholds)


def test_each_level_derives_a_different_row() -> None:
    """The slider has to be wired to something.

    Distinctness, not the literals: this is what fails when `derive` ignores its
    argument, or when one row is pasted over another. Both leave a table that is
    still perfectly self-consistent, which is why comparing derive() to derive()
    cannot see them.
    """
    rows = [derive(level) for level in _ASCENDING]
    for i, first in enumerate(rows):
        for second in rows[i + 1 :]:
            assert first != second, f"two chattiness levels derive the same row: {first}"


@pytest.mark.parametrize("field", _LOWERED_BY_CHATTINESS)
def test_a_chattier_setting_lowers_the_bar(field: str) -> None:
    """Direction, per plan §2.7: more chattiness means every gate opens sooner.

    Strict on purpose. Two levels that tie on a threshold are half a slider —
    dragging between them changes nothing for that behaviour, which is the failure
    a user would report as "the slider does nothing".
    """
    low, medium, high = _across_levels(field)
    assert low > medium > high, f"{field} is not strictly decreasing: {[low, medium, high]}"


def test_a_chattier_setting_never_shortens_the_reply() -> None:
    """Reply length is the one threshold that rises with chattiness.

    Plan §2.7 rates this column short / medium / medium, low to high: low has to
    be the terse one, and medium and high are allowed to tie because past a point
    a longer cap stops being what "chattier" means. So: never decreasing, and low
    genuinely lower — a flat column would mean chattiness does not touch reply
    length at all.
    """
    low, medium, high = _across_levels("max_output_tokens")
    assert low < medium, f"low should be the terse one: {[low, medium, high]}"
    assert medium <= high, f"reply cap must not shrink as chattiness rises: {[medium, high]}"


def test_the_derived_set_is_exactly_the_five_thresholds() -> None:
    """Five numbers, no more (plan §2.7).

    `cli.cmd_show` republishes this model wholesale under `_derived`, and the
    settings page renders that block read-only (plan §7.5). A sixth field would
    reach the UI with no label and no bound; a missing one silently stops being
    derived from anything.
    """
    assert set(DerivedThresholds.model_fields) == _THRESHOLD_FIELDS


# ------------------------------------------------------------------ the literals


# Spelled out rather than computed. Plan §2.7 fixes three of the five columns
# outright — idle 180/90/45s, danmaku window 30/20/12s, cooldown 20/12/5s — and
# describes the other two qualitatively; the medium row is corroborated by the
# plan's own example TOML (idle_threshold_s = 90, score_threshold = 0.35,
# max_output_tokens = 120). The direction tests above cannot tell 45s from 40s,
# and these are values someone tuned against a live room. Restating them is the
# point: changing one should be a deliberate edit here, not a drift nobody sees.
_TUNED_TABLE: dict[Chattiness, dict[str, float]] = {
    Chattiness.LOW: {
        "idle_threshold_s": 180,
        "danmaku_window_s": 30,
        "score_threshold": 0.55,
        "cooldown_s": 20,
        "max_output_tokens": 70,
    },
    Chattiness.MEDIUM: {
        "idle_threshold_s": 90,
        "danmaku_window_s": 20,
        "score_threshold": 0.35,
        "cooldown_s": 12,
        "max_output_tokens": 120,
    },
    Chattiness.HIGH: {
        "idle_threshold_s": 45,
        "danmaku_window_s": 12,
        "score_threshold": 0.2,
        "cooldown_s": 5,
        "max_output_tokens": 120,
    },
}


@pytest.mark.parametrize("level", list(Chattiness), ids=[level.value for level in Chattiness])
def test_the_tuned_numbers_are_what_ships(level: Chattiness) -> None:
    """Every level, every number, against the table a human tuned."""
    assert derive(level).model_dump() == _TUNED_TABLE[level]


@pytest.mark.parametrize("level", list(Chattiness), ids=[level.value for level in Chattiness])
def test_no_row_is_degenerate(level: Chattiness) -> None:
    """Boundary: a zero or a negative in any row disables the thing it gates.

    cooldown_s = 0 removes the gate throttle, danmaku_window_s = 0 empties the
    candidate pool, max_output_tokens = 0 mutes the assistant. score_threshold is
    a normalised score bar, so above 1.0 nothing ever clears it — silence that
    looks like a broken pipeline rather than a setting.
    """
    row = derive(level)
    assert row.idle_threshold_s > 0
    assert row.danmaku_window_s > 0
    assert row.cooldown_s > 0
    assert row.max_output_tokens > 0
    assert 0.0 < row.score_threshold <= 1.0


# -------------------------------------------------------------- the single writer


def test_an_unmapped_level_raises_instead_of_falling_back() -> None:
    """Error path: no silent default.

    A `.get(level, MEDIUM)` here would be indistinguishable from a working slider
    for two of the three levels, and would turn a future fourth level into one that
    quietly behaves like medium — the same invisible failure as ignoring the
    argument, just narrower. KeyError is the loud version, and it is what makes
    `test_every_level_derives_a_row` above able to fail at all.

    A string the enum has never heard of is the only way to reach the miss from a
    test: loading one from a TOML gets rejected by pydantic first, so the real
    shape of this bug is a level the enum knows and the table does not.
    """
    with pytest.raises(KeyError):
        derive(cast(Chattiness, "chatty"))


@pytest.mark.parametrize(
    "key",
    [*sorted(_THRESHOLD_FIELDS), "window_s", "per_uid_cooldown_s"],
)
def test_the_toml_cannot_pin_a_derived_threshold(key: str) -> None:
    """The other half of "chattiness is the single writer" (plan §2.7).

    If the file could pin `window_s` and the slider also moved it, nothing would
    define which wins. `extra="forbid"` on InteractionConfig is what makes that
    unrepresentable rather than merely discouraged, so a config that tries gets
    rejected at load instead of having its value silently ignored. The last two
    keys are the spellings the plan's own example TOML uses, which is what someone
    copying from it would actually type.
    """
    payload: dict[str, Any] = {"interaction": {"chattiness": "low", key: 1}}
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_a_derived_row_cannot_be_rewritten_in_place() -> None:
    """The third way a second writer could appear, after the TOML and the slider.

    `derive()` hands out the table row itself, not a copy, so an unfrozen model
    would let one stray assignment rewrite the mapping for the whole process — and
    `config show` would then print the corrupted number as derived truth.
    """
    row = derive(Chattiness.HIGH)
    with pytest.raises(ValidationError):
        row.max_output_tokens = 4  # type: ignore[misc]
    assert derive(Chattiness.HIGH).max_output_tokens == row.max_output_tokens
