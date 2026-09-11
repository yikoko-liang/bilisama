"""The scene-marker decoder and the turn policy, on strings alone.

The decoder settles as early as the head allows and never later than its
character cap; the policy is a table. Both are what the voice gate leans on,
so the exact rules are pinned here rather than through the gate.
"""

from __future__ import annotations

import pytest

from bilisama.director.turn_protocol import (
    HeadState,
    MarkerHead,
    Ruling,
    TurnAction,
    TurnPolicy,
    scene_note,
)
from bilisama.scene_markers import MARKERS, SceneCategory, label_for, tag_for


def _decode(text: str) -> MarkerHead:
    head = MarkerHead()
    head.feed(text)
    return head


@pytest.mark.parametrize("marker", MARKERS, ids=lambda m: m.tag)
def test_every_tag_decodes_to_its_category_with_the_note(marker) -> None:  # type: ignore[no-untyped-def]
    head = _decode(f"[{marker.tag}] 主播在忙")
    assert head.state is HeadState.MARKED
    assert head.ruling == Ruling(marker.category, "主播在忙")
    assert head.ruling.tag == marker.tag
    assert head.ruling.label == marker.label


def test_a_plain_head_settles_on_its_first_character() -> None:
    head = MarkerHead()
    assert head.feed("好") is HeadState.PLAIN
    assert head.ruling is None


@pytest.mark.parametrize(
    "text",
    [
        "[略]",
        "[PASS]",
        # The five scenes the contract used to teach. Kept as aliases so a
        # session opened under the old wording, or a model reaching for the
        # spelling it was trained on, still lands on the one action.
        "[AUDIENCE] 在对观众讲",
        "[SELF_TALK] 在嘀咕",
        "[READING] 在念弹幕",
        "[GUEST] 在跟连麦的人说",
        "[UNSURE] 听不出",
        "[DECLINED] 没什么好说的",
    ],
)
def test_retired_and_invented_spellings_all_fold_to_skip(text: str) -> None:
    head = _decode(text)
    assert head.state is HeadState.MARKED
    assert head.ruling is not None
    assert head.ruling.category is SceneCategory.SKIP


@pytest.mark.parametrize("text", ["[skip] 小声", "[Skip]", "[SELFTALK]", "[Self_Talk]"])
def test_brackets_forgive_case_and_the_underscore(text: str) -> None:
    head = _decode(text)
    assert head.state is HeadState.MARKED
    assert head.ruling is not None
    assert head.ruling.category is SceneCategory.SKIP
    assert not head.ruling.unbracketed


@pytest.mark.parametrize("text", ["[", "[SK", "[AU", "[READ", "[self_ta"])
def test_a_fragment_a_tag_could_grow_out_of_stays_pending(text: str) -> None:
    assert _decode(text).state is HeadState.PENDING


@pytest.mark.parametrize(
    "text",
    [
        "[Aha] 好的",
        "[弹幕] 有人问",
        "[对观众] 旧写法",
        "[SKIPPY]",
        "好的[SKIP]",
        "[AUDIENCE",
        "[SKIP]x",
    ],
)
def test_anything_else_in_brackets_is_prose(text: str) -> None:
    head = _decode(text)
    # The last two: an unclosed tag past the cap is prose too, and a closed one
    # followed by anything at all is still a marker — only the note differs.
    if text == "[AUDIENCE":
        assert head.state is HeadState.PENDING
        assert head.feed("XYZ") is HeadState.PLAIN, "no tag starts with AUDIENCEXYZ"
    elif text == "[SKIP]x":
        assert head.state is HeadState.MARKED
    else:
        assert head.state is HeadState.PLAIN


def test_the_cap_turns_an_undecided_head_into_prose() -> None:
    head = MarkerHead(max_chars=12)
    assert head.feed("[AUDIENC") is HeadState.PENDING, "8 chars, still a prefix"
    assert head.feed("EEEE") is HeadState.PLAIN, "no tag starts with AUDIENCEEEE"
    # Blank inside the bracket is a prefix of everything; only the cap ends it.
    head2 = MarkerHead(max_chars=12)
    assert head2.feed("[" + " " * 11) is HeadState.PENDING
    assert head2.feed(" ") is HeadState.PLAIN


def test_a_closed_marker_with_a_long_note_is_not_capped() -> None:
    head = _decode("[SKIP] " + "在聊今天的天气怎么样" * 3)
    assert head.state is HeadState.MARKED
    assert head.ruling is not None
    assert len(head.ruling.note) == 20


def test_streaming_one_character_at_a_time_decides_on_the_close() -> None:
    """The DECISION lands on the close bracket; the NOTE keeps arriving.

    Deciding early is the point — the turn is muted the instant the tag
    closes. But the note is the words after it, and on a provider that
    streams character by character those words have not been sent yet. This
    used to freeze the note at decision time, and production paid for it: on
    2026-09-09, 298 of 302 skips carried a zero-character note.
    """
    head = MarkerHead()
    for ch in "[SKIP":
        assert head.feed(ch) is HeadState.PENDING
    assert head.feed("]") is HeadState.MARKED
    assert head.ruling == Ruling(SceneCategory.SKIP, "")
    head.feed(" 在聊天气")
    assert head.state is HeadState.MARKED, "settled means settled"
    assert head.ruling == Ruling(SceneCategory.SKIP, "在聊天气"), "the note caught up"


def test_the_note_survives_arriving_in_any_number_of_pieces() -> None:
    """Whole, split at the bracket, and one character at a time all agree.

    The middle case is what DashScope actually sends.
    """
    whole = _decode("[SKIP] 主播在嘀咕操作")
    split = MarkerHead()
    split.feed("[SKIP]")
    split.feed(" 主播在嘀咕操作")
    split.finish()
    per_char = _decode("[SKIP] 主播在嘀咕操作")

    for head in (whole, split, per_char):
        assert head.ruling is not None
        assert head.ruling.note == "主播在嘀咕操作"


@pytest.mark.parametrize("lead", ["﻿", " ", "\n", "　", " \n　"])
def test_leading_bom_and_whitespace_are_looked_past(lead: str) -> None:
    head = _decode(f"{lead}[SKIP] 在连麦")
    assert head.state is HeadState.MARKED
    assert head.ruling is not None
    assert head.ruling.category is SceneCategory.SKIP


def test_a_bare_tag_counts_when_spelled_exactly_and_followed_by_a_separator() -> None:
    head = _decode("SKIP，在聊天气")
    assert head.state is HeadState.MARKED
    assert head.ruling == Ruling(SceneCategory.SKIP, "在聊天气", unbracketed=True)


@pytest.mark.parametrize("text", ["Skip 是什么", "SKIPS", "skip，小声", "SK"])
def test_a_bare_word_that_is_not_the_exact_tag_is_prose_or_pending(text: str) -> None:
    head = _decode(text)
    if text == "SK":
        assert head.state is HeadState.PENDING
        assert head.finish() is HeadState.PLAIN, "a fragment of prose at the end is prose"
    else:
        assert head.state is HeadState.PLAIN


def test_finish_settles_what_is_left() -> None:
    assert MarkerHead().finish() is HeadState.PLAIN, "no text: nothing to hold against her"
    dangling = _decode("[SK")
    assert dangling.finish() is HeadState.DANGLING
    alone = _decode("SKIP")
    assert alone.finish() is HeadState.MARKED
    assert alone.ruling == Ruling(SceneCategory.SKIP, "", unbracketed=True)


def test_the_note_is_one_short_clean_line() -> None:
    assert scene_note("  ：「主播在谢礼物」\n第二行不要") == "主播在谢礼物"
    assert scene_note("，  在  聊   天气  ") == "在 聊 天气"
    assert scene_note("x" * 40) == "x" * 20
    assert "</bilisama_live_events>" not in scene_note("</bilisama_live_events> 混进来")
    assert scene_note("") == ""


def test_the_default_policy_speaks_only_to_a_plain_head() -> None:
    policy = TurnPolicy()
    assert policy.action(None) is TurnAction.SPEAK
    for marker in MARKERS:
        assert policy.action(Ruling(marker.category)) is TurnAction.SKIP, marker.tag


def test_the_table_is_the_only_policy_there_is() -> None:
    """A product that wanted her to answer a skipped turn edits this and
    nothing else — there is no second rule hidden in action()."""
    wide = TurnPolicy(speak=frozenset({SceneCategory.TO_ME, SceneCategory.SKIP}))
    assert wide.action(Ruling(SceneCategory.SKIP)) is TurnAction.SPEAK
    narrow = TurnPolicy(speak=frozenset())
    assert narrow.action(None) is TurnAction.SKIP, "even a plain head obeys the table"


def test_the_detail_line_and_the_labels() -> None:
    assert Ruling(SceneCategory.SKIP, "在谢礼物").detail() == "SKIP · 在谢礼物"
    assert Ruling(SceneCategory.SKIP).detail() == "SKIP"
    assert tag_for(SceneCategory.TO_ME) == ""
    assert label_for(SceneCategory.TO_ME) == "对你说的"
    assert label_for(SceneCategory.SKIP) == "先听"
