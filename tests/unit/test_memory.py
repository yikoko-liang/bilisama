"""Tier 0 memory: the counters that make a regular feel recognised.

The centrepiece is the stage-3 acceptance number: replay the same viewers
across three streams and `streams_seen` must read exactly 3 — once per
stream, not once per event (plan section 9, stage 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.context import (
    clock_line,
    memory_segments,
    regulars_line,
    session_progress_text,
)
from bilisama.memory.store import MemoryStore, logical_date
from tests.fakes.replay import fixture, read_fixture


def _event(
    uid: int = 1001,
    name: str = "路人甲",
    text: str = "你好",
    kind: EventKind = EventKind.DANMAKU,
    value_cny: float = 0.0,
    uid_hash: str = "",
) -> LiveEvent:
    return LiveEvent(
        kind=kind,
        viewer=Viewer(uid=uid, name=name, uid_hash=uid_hash),
        text=text,
        value_cny=value_cny,
    )


@pytest.fixture()
def clock() -> FakeClock:
    # Mid-week, well past the 04:00 boundary, so week arithmetic is unambiguous.
    return FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))


@pytest.fixture()
def store(clock: FakeClock) -> MemoryStore:
    s = MemoryStore(":memory:", clock)
    s.begin_stream()
    return s


# ------------------------------------------------------------ tier 0 counters


def test_streams_seen_counts_streams_not_events(clock: FakeClock) -> None:
    """The acceptance number: three streams of the same crowd reads 3."""
    store = MemoryStore(":memory:", clock)
    for _ in range(3):
        store.begin_stream()
        for _at, event in read_fixture(fixture("returning_viewer.jsonl")):
            store.on_event(event)
        store.end_stream()

    viewer = store.viewer("uid:7001")
    assert viewer is not None
    assert viewer.streams_seen == 3, "once per stream, however many events inside"
    assert viewer.msg_count >= 3


def test_multiple_events_in_one_stream_bump_streams_seen_once(store: MemoryStore) -> None:
    for _ in range(5):
        store.on_event(_event())
    viewer = store.viewer("uid:1001")
    assert viewer is not None
    assert viewer.streams_seen == 1
    assert viewer.msg_count == 5


def test_gift_value_accumulates_and_gifts_do_not_count_as_messages(store: MemoryStore) -> None:
    store.on_event(
        _event(
            kind=EventKind.GIFT,
            text="",
            value_cny=52.0,
        )
    )
    store.on_event(_event(kind=EventKind.GIFT, text="", value_cny=10.0))
    viewer = store.viewer("uid:1001")
    assert viewer is not None
    assert viewer.gift_value_cny == 62.0
    assert viewer.msg_count == 0


def test_masked_viewers_keep_identity_via_uid_hash(store: MemoryStore) -> None:
    """uid 0 is a masked viewer, not a nobody — the N.E.K.O lesson."""
    store.on_event(_event(uid=0, uid_hash="abc123", name="***"))
    store.on_event(_event(uid=0, uid_hash="abc123", name="***"))
    viewer = store.viewer("hash:abc123")
    assert viewer is not None
    assert viewer.msg_count == 2


def test_events_refuse_to_land_without_an_open_stream(clock: FakeClock) -> None:
    store = MemoryStore(":memory:", clock)
    with pytest.raises(RuntimeError, match="begin_stream"):
        store.on_event(_event())


def test_recent_events_come_back_oldest_first_with_a_limit(store: MemoryStore) -> None:
    for i in range(6):
        store.on_event(_event(text=f"第{i}条"))
    lines = store.recent_events(limit=3)
    assert len(lines) == 3
    assert "第3条" in lines[0] and "第5条" in lines[-1]


def test_prune_drops_old_events_but_never_viewers(store: MemoryStore, clock: FakeClock) -> None:
    store.on_event(_event())
    # Move eight days: the event ages out, the viewer row must not.
    clock._now += 8 * 86400.0
    dropped = store.prune_events(retain_days=7)
    assert dropped == 1
    assert store.viewer("uid:1001") is not None


# ------------------------------------------------------------ facts


def test_replace_facts_is_delete_then_insert_per_scope_subject(store: MemoryStore) -> None:
    store.replace_facts("viewer", "uid:1", [("常来", "常客"), ("送过舰", "付费")])
    store.replace_facts("viewer", "uid:2", [("第一次来", "新人")])
    store.replace_facts("viewer", "uid:1", [("改口了", "更新")])

    assert [f.text for f in store.facts("viewer", "uid:1")] == ["改口了"]
    assert [f.text for f in store.facts("viewer", "uid:2")] == [
        "第一次来"
    ], "replacement scopes to its own subject"


# ------------------------------------------------------------ stream clock


def test_streams_this_week_respects_the_0400_boundary(clock: FakeClock) -> None:
    """A 03:00 stream belongs to the previous logical day (and its week)."""
    assert logical_date(datetime(2026, 8, 10, 3, 0, tzinfo=UTC)).isocalendar()[:2] == (
        logical_date(datetime(2026, 8, 9, 23, 0, tzinfo=UTC)).isocalendar()[:2]
    )

    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.end_stream()
    store.begin_stream()
    assert store.streams_this_week() == 2


def test_clock_line_reads_like_stream_time(store: MemoryStore, clock: FakeClock) -> None:
    clock._now += 107 * 60.0  # 1h47m into the stream
    line = clock_line(store, clock)
    assert "开播 1 小时 47 分" in line
    assert "本周第 1 场" in line
    assert "现在" in line


def test_clock_line_is_empty_with_no_open_stream(clock: FakeClock) -> None:
    store = MemoryStore(":memory:", clock)
    assert clock_line(store, clock) == ""


# ------------------------------------------------------------ context segments


def test_regulars_line_names_returning_viewers_only(clock: FakeClock) -> None:
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.on_event(_event(uid=1, name="阿强"))
    store.end_stream()
    store.begin_stream()
    store.on_event(_event(uid=1, name="阿强"))
    store.on_event(_event(uid=2, name="新人"))

    line = regulars_line(store)
    assert "阿强（第 2 次来）" in line
    assert "新人" not in line, "first-timers are not regulars"


def test_session_progress_reads_the_stream_scoped_fact(store: MemoryStore) -> None:
    assert session_progress_text(store) == ""
    store.replace_facts("stream", str(store.stream_id), [("刚修完一个 bug，弹幕在聊猫", "")])
    assert "修完" in session_progress_text(store)


def test_memory_segments_bundle_everything(store: MemoryStore, clock: FakeClock) -> None:
    store.replace_facts("streamer", "", [("主播在写编译器", "工作")])
    segments = memory_segments(store, clock)
    assert "编译器" in segments.streamer_facts
    assert "开播" in segments.clock_line
    assert segments.regulars == ""
    assert segments.session_progress == ""


# ------------------------------------------------------------ write-behind


def _independent_count(db_path: str, table: str) -> int:
    """Row count through a SECOND connection — sees only what was committed,
    never the buffer, which is exactly the point."""
    import sqlite3

    db = sqlite3.connect(db_path)
    try:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        db.close()


def test_write_through_is_the_default_and_lands_immediately(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = str(tmp_path / "wt.db")
    s = MemoryStore(path, clock)
    s.begin_stream()
    s.on_event(_event())
    assert _independent_count(path, "event") == 1, "batch off: every event lands at once"
    s.close()


def test_batched_writes_hold_until_a_read_flushes(tmp_path: Path, clock: FakeClock) -> None:
    path = str(tmp_path / "wb.db")
    s = MemoryStore(path, clock, write_batch_ms=200)
    s.begin_stream()
    s.on_event(_event(text="第一条"))
    assert _independent_count(path, "event") == 0, "inside the window: buffered, not committed"
    # Any read flushes first — the caller can never observe a stale answer.
    assert s.recent_events() == ["[danmaku] 路人甲: 第一条"]
    assert _independent_count(path, "event") == 1
    s.close()


def test_batch_flushes_when_the_window_ages_out(tmp_path: Path, clock: FakeClock) -> None:
    path = str(tmp_path / "age.db")
    s = MemoryStore(path, clock, write_batch_ms=200)
    s.begin_stream()
    s.on_event(_event(text="先攒着"))
    clock._now += 0.3  # past the 200ms window; sync code, direct nudge is fine
    s.on_event(_event(text="这条触发落盘"))
    assert _independent_count(path, "event") == 2
    s.close()


def test_batch_flushes_at_the_row_cap(tmp_path: Path, clock: FakeClock) -> None:
    path = str(tmp_path / "cap.db")
    s = MemoryStore(path, clock, write_batch_ms=5000)
    s.begin_stream()
    for i in range(200):
        s.on_event(_event(uid=2000 + i, text=f"第{i}条"))
    assert _independent_count(path, "event") == 200, "200 rows must not wait for the window"
    s.close()


def test_stream_end_flushes_and_aggregation_matches_write_through(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The acceptance property of write-behind: after a flush, the viewer
    aggregates are byte-for-byte what per-event writes would have produced."""
    path = str(tmp_path / "agg.db")
    s = MemoryStore(path, clock, write_batch_ms=5000)
    s.begin_stream()
    s.on_event(_event(uid=7, name="老板", text="来了", kind=EventKind.DANMAKU))
    s.on_event(_event(uid=7, name="老板", text="", kind=EventKind.GIFT, value_cny=52.0))
    s.end_stream()
    assert _independent_count(path, "event") == 2
    row = s.viewer("uid:7")
    assert row is not None
    assert row.streams_seen == 1, "two events in one stream still count one visit"
    assert row.msg_count == 1, "only the text-bearing event counts as a message"
    assert row.gift_value_cny == 52.0
    s.close()


def test_close_flushes_the_tail(tmp_path: Path, clock: FakeClock) -> None:
    path = str(tmp_path / "tail.db")
    s = MemoryStore(path, clock, write_batch_ms=5000)
    s.begin_stream()
    s.on_event(_event(text="最后一条"))
    s.close()
    assert _independent_count(path, "event") == 1, "close() must not drop the buffer"


# ------------------------------------------------------------ clock granularity


def test_clock_line_floors_both_numbers_to_the_granularity(
    store: MemoryStore, clock: FakeClock
) -> None:
    """granularity_min is the push-cadence knob: at 5, minutes 5-9 all render
    the same line, so the assembled tail stops changing every minute."""
    clock._now += 7 * 60  # wall follows monotonic: 04:00 CST -> 04:07
    exact = clock_line(store, clock)
    assert exact.startswith("开播 7 分钟，现在 04:07，")
    coarse = clock_line(store, clock, granularity_min=5)
    assert coarse.startswith("开播约 5 分钟，现在 04:05 左右，")
    clock._now += 2 * 60  # 04:09 — still inside the same 5-minute step
    assert clock_line(store, clock, granularity_min=5) == coarse
    clock._now += 60  # 04:10 — the step turns over
    assert clock_line(store, clock, granularity_min=5).startswith(
        "开播约 10 分钟，现在 04:10 左右，"
    )


def test_memory_segments_passes_the_granularity_through(
    store: MemoryStore, clock: FakeClock
) -> None:
    clock._now += 7 * 60
    segments = memory_segments(store, clock, clock_granularity_min=5)
    assert segments.clock_line.startswith("开播约 5 分钟，现在 04:05 左右，")
