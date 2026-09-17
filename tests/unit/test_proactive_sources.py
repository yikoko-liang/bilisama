"""Where a proactive topic may come from, and what stops it from repeating.

The layer table is the counterpart of event_pacing's four bands: the band
says how often she may open, this says what she may open WITH. The ledger
is the memory of what she already opened with — literal similarity, the
layer, the viewer she named — so the next opening cannot be the last one
reheated (2026-09-15 16:02: the same danmaku answered three times).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.event_pacing import RoomActivity
from bilisama.proactive_sources import (
    Layer,
    TopicLedger,
    TopicPool,
    allowed_layers,
    topic_similarity,
)


def test_busier_rooms_allow_fewer_layers() -> None:
    assert allowed_layers(RoomActivity.BUSY) == frozenset()
    assert allowed_layers(RoomActivity.ACTIVE) == frozenset(
        {Layer.OWED, Layer.UNANSWERED, Layer.DISCUSSION, Layer.STREAMER}
    )
    assert allowed_layers(RoomActivity.SPARSE) == allowed_layers(RoomActivity.ACTIVE) | {
        Layer.STREAM
    }
    assert allowed_layers(RoomActivity.QUIET) == frozenset(Layer)
    assert allowed_layers(None) == frozenset(Layer), "no pacer: the legacy wiring is a quiet room"


def test_layers_are_ordered_by_how_close_they_sit_to_the_room() -> None:
    assert list(Layer) == [
        Layer.OWED,
        Layer.UNANSWERED,
        Layer.DISCUSSION,
        Layer.STREAMER,
        Layer.STREAM,
        Layer.MEMORY,
        Layer.POOL,
    ]
    assert Layer.OWED < Layer.POOL


@pytest.mark.parametrize(
    ("a", "b", "at_least"),
    [
        (
            "小满问为什么砍了自动分类，主播解释是怕测试太复杂",
            "刚才小满问自动分类为啥砍了，我解释过是怕测试太复杂",
            0.6,
        ),
        ("大家平时更常用哪个模型？", "大家平时更常用哪个模型呢", 0.9),
    ],
)
def test_similar_topics_score_high(a: str, b: str, at_least: float) -> None:
    assert topic_similarity(a, b) >= at_least


def test_different_topics_score_low() -> None:
    assert topic_similarity("大家平时更常用哪个模型？", "今天的配色你们觉得哪张最好看") < 0.3
    assert topic_similarity("", "什么都没有") == 0.0


def test_the_ledger_remembers_topics_and_flags_a_reheat() -> None:
    clock = FakeClock()
    ledger = TopicLedger(clock)
    ledger.note(
        "proactive:1", "小满问为什么砍了自动分类，主播解释是怕测试太复杂", layer=Layer.UNANSWERED
    )
    dup = ledger.duplicate_of("刚才小满问自动分类为啥砍了，我解释过是怕测试太复杂")
    assert dup is not None and dup.score >= 0.6
    assert ledger.duplicate_of("今天的配色你们觉得哪张最好看") is None
    assert ledger.last_layer == Layer.UNANSWERED
    assert "自动分类" in ledger.recent_lines()[0]


async def test_the_ledger_forgets_after_its_window() -> None:
    clock = FakeClock()
    ledger = TopicLedger(clock, window_s=100.0)
    ledger.note("proactive:1", "大家平时更常用哪个模型？", layer=Layer.POOL)
    await clock.advance(101)
    assert ledger.duplicate_of("大家平时更常用哪个模型？") is None
    assert ledger.recent_lines() == []


def test_the_ledger_counts_how_often_a_viewer_was_named() -> None:
    ledger = TopicLedger(FakeClock())
    ledger.note("p1", "小满你那个问题……", layer=Layer.UNANSWERED, named=("小满",))
    ledger.note("p2", "小满，再问你一句", layer=Layer.UNANSWERED, named=("小满",))
    assert ledger.times_named("小满") == 2
    assert ledger.times_named("白团") == 0


def test_the_pool_reads_one_question_per_line_and_hands_each_out_once(tmp_path: Path) -> None:
    (tmp_path / "aigc.md").write_text(
        "# AIGC 教学\n\n你们第一次用 AI 画图是哪一年？\n\n最想让 AI 帮你做掉的一件杂事是什么？\n",
        encoding="utf-8",
    )
    (tmp_path / "coding.md").write_text(
        "# coding\n改代码你更怕它不动手还是改太多？\n", encoding="utf-8"
    )
    pool = TopicPool.load(tmp_path)
    assert pool.size == 3
    drawn = pool.draw(5)
    assert len(drawn) == 3 and len(set(drawn)) == 3
    for line in drawn:
        pool.mark_used(line)
    assert pool.draw(5) == [], "each line once per stream"
    pool.reset()
    assert len(pool.draw(5)) == 3


def test_a_missing_pool_directory_is_an_empty_pool(tmp_path: Path) -> None:
    pool = TopicPool.load(tmp_path / "nowhere")
    assert pool.size == 0 and pool.draw(3) == []
