"""Tier 0 memory: the counters that make a regular feel recognised.

The centrepiece is the stage-3 acceptance number: replay the same viewers
across three streams and `streams_seen` must read exactly 3 — once per
stream, not once per event (plan section 9, stage 3).
"""

from __future__ import annotations

from datetime import UTC, datetime

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
