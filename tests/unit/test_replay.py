"""Replay source and the shipped fixtures.

Two things are under test here. First tests/fakes/replay.py itself — parsing,
stopping mid-file, looping, the errors it raises. Second the eight fixtures under
tests/fixtures, which are a stage 0 deliverable (plan §10.2) and each tied to an
interaction level in §2.7.

The fixtures need tests of their own because everything from stage 3 onward is
asserted against them. A fixture that has drifted from what it is for is worse than
no fixture: a test built on it still goes green, and it reads like coverage. So each
one gets a test for the property that makes it worth shipping — all uid 0, one combo
from one viewer, a gap long enough to go cold — not just "it parses".
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest

from bilisama.ingest.events import EventKind, LiveEvent, Medal, is_vip_entry
from tests.fakes.replay import (
    FIXTURE_DIR,
    ReplaySource,
    fixture,
    parse_line,
    read_fixture,
)

# The eight fixtures §10.2 asks for, one per interaction level. Kept explicit rather
# than globbed so that deleting one is a test failure and not a silent loss of a
# level's only coverage.
FIXTURE_NAMES = [
    "anonymous_masked.jsonl",
    "event_flood.jsonl",
    "gift_combo.jsonl",
    "injection_attempt.jsonl",
    "presence.jsonl",
    "quiet_stream.jsonl",
    "returning_viewer.jsonl",
    "superchat_during_speech.jsonl",
]

# The tag every live-event block is wrapped in (plan §4.5). injection_attempt.jsonl
# tries to close it, which is only an attack while the name matches.
WRAPPER_TAG = "bilisama_live_events"

# event_flood.jsonl ends with three gift lines whose at_s (3.1, 6.4, 9.2) rewinds
# from the 10.964 of the danmaku above them. ReplaySource emits in file order, so
# the paid events arrive after the flood instead of during it.
FLOOD_TIMELINE_IS_OUT_OF_ORDER = (
    "event_flood.jsonl: the three trailing gift lines rewind at_s to 3.1, so replay "
    "order does not match recorded order. Fixture defect, not a replay defect."
)


def _replay(name: str) -> list[LiveEvent]:
    return [event for _, event in read_fixture(fixture(name))]


def _timeline(name: str) -> list[tuple[float, LiveEvent]]:
    return list(read_fixture(fixture(name)))


def _window_count(moments: list[float], start: float, window_s: float) -> int:
    """How many events land in [start, start + window_s)."""
    return sum(1 for m in moments if start <= m < start + window_s)


# ------------------------------------------------------------ parse_line: medal

# No shipped fixture carries a medal block, so this branch and Medal.is_this_room()
# only run here. Real danmaku carries one on most events; see the report attached to
# backlog item 10.


def test_parse_line_builds_a_medal() -> None:
    raw = {
        "kind": "danmaku",
        "text": "又来啦",
        "viewer": {
            "uid": 7001,
            "name": "常客0",
            "medal": {
                "name": "小笨蛋",
                "level": 21,
                "up_name": "主播本人",
                "anchor_room_id": 12345,
            },
        },
    }
    medal = parse_line(raw).viewer.medal
    assert medal == Medal(name="小笨蛋", level=21, up_name="主播本人", anchor_room_id=12345)


def test_a_partial_medal_block_falls_back_field_by_field() -> None:
    """The platform omits fields on medals from other rooms, so no key is required."""
    medal = parse_line({"kind": "danmaku", "viewer": {"medal": {"name": "小笨蛋"}}}).viewer.medal
    assert medal == Medal(name="小笨蛋", level=0, up_name="", anchor_room_id=0)


@pytest.mark.parametrize("block", [None, {}], ids=["null", "empty"])
def test_an_absent_medal_block_leaves_medal_none(block: dict[str, object] | None) -> None:
    """An empty dict has to read as "no medal", not as a nameless one."""
    assert parse_line({"kind": "danmaku", "viewer": {"medal": block}}).viewer.medal is None
    assert parse_line({"kind": "danmaku", "viewer": {}}).viewer.medal is None


@pytest.mark.parametrize(
    ("medal", "expected"),
    [
        (Medal(name="小笨蛋", level=21, anchor_room_id=12345), True),
        (Medal(name="", level=21, anchor_room_id=12345), False),
        (Medal(name="小笨蛋", level=21, anchor_room_id=0), False),
        (Medal(), False),
    ],
    ids=["this-room", "nameless", "no-anchor", "empty"],
)
def test_is_this_room_needs_a_name_and_an_anchor_room(medal: Medal, expected: bool) -> None:
    """A medal worn for someone else's room must not read as loyalty to this one."""
    assert medal.is_this_room(medal.anchor_room_id or 1) is expected
    assert medal.is_this_room(999999) is False or medal.anchor_room_id == 999999


# ------------------------------------------------------------ parse_line: viewer


@pytest.mark.parametrize("uid", [None, 0, "0", ""], ids=["null", "zero", "str-zero", "empty"])
def test_a_masked_uid_still_produces_a_viewer(uid: object) -> None:
    """Masking arrives in several shapes and none of them may drop the event.

    N.E.K.O returns early on exactly this (neko_live/modules/live_events/module.py:238),
    which silences the whole stream once masking kicks in.
    """
    raw = {"kind": "danmaku", "text": "匿名弹幕", "viewer": {"uid": uid, "uid_hash": "h0000"}}
    event = parse_line(raw)
    assert event.viewer.uid == 0
    assert event.is_anonymous
    assert event.viewer.identity == "hash:h0000"


def test_the_room_id_argument_wins_over_the_payload() -> None:
    """Fixtures are recorded elsewhere, so the caller has to be able to say which
    room this replay stands for."""
    raw = {"kind": "danmaku", "room_id": 111}
    assert parse_line(raw).room_id == 111
    assert parse_line(raw, room_id=222).room_id == 222


def test_parse_line_keeps_the_raw_payload_for_debugging() -> None:
    raw = {"kind": "danmaku", "text": "嗨"}
    event = parse_line(raw)
    assert event.raw is raw
    assert event.redacted().raw is None  # and it is strippable before any prompt


# ------------------------------------------------------------ parse_line: money


def test_a_gold_gift_gets_its_value_derived() -> None:
    raw = {
        "kind": "gift",
        "gift": {"name": "小星星", "coin_type": "gold", "total_coin": 20000},
    }
    event = parse_line(raw)
    assert event.value_cny == 20.0
    assert event.is_paid


def test_a_silver_gift_is_worth_nothing() -> None:
    """Silver is free currency. Treating it as paid puts it in the protected lane."""
    raw = {"kind": "gift", "gift": {"name": "辣条", "coin_type": "silver", "total_coin": 500}}
    event = parse_line(raw)
    assert event.value_cny == 0.0
    assert not event.is_paid


def test_an_explicit_value_beats_the_derived_one() -> None:
    """Super chats carry their price directly; only gifts need the gold conversion."""
    raw = {
        "kind": "gift",
        "value_cny": 5.0,
        "gift": {"coin_type": "gold", "total_coin": 999999},
    }
    assert parse_line(raw).value_cny == 5.0


# ------------------------------------------------------------ parse_line: errors


def test_an_undefined_kind_is_rejected_loudly() -> None:
    """A typo in a fixture must not become a default event kind.

    EventKind is the taxonomy the config schema and the UI protocol both key off,
    so an unknown value has nowhere sensible to land.
    """
    with pytest.raises(ValueError, match="danmuku"):
        parse_line({"kind": "danmuku"})


def test_a_line_with_no_kind_at_all_is_rejected() -> None:
    with pytest.raises(KeyError, match="kind"):
        parse_line({"text": "嗨"})


# ------------------------------------------------------------ read_fixture


def test_read_fixture_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    """Fixtures open with a comment saying what they are for. Indented and trailing
    blank lines happen when someone edits one by hand."""
    path = tmp_path / "noise.jsonl"
    path.write_text(
        "# 说明：这条 fixture 是给测试用的\n"
        '{"at_s": 0.0, "kind": "danmaku", "text": "第一条", "event_id": "n1"}\n'
        "\n"
        "   # 缩进的注释\n"
        '   {"at_s": 1.5, "kind": "danmaku", "text": "第二条", "event_id": "n2"}\n'
        "\n",
        encoding="utf-8",
    )
    timeline = list(read_fixture(path))
    assert [at for at, _ in timeline] == [0.0, 1.5]
    assert [event.event_id for _, event in timeline] == ["n1", "n2"]


def test_read_fixture_defaults_at_s_to_zero(tmp_path: Path) -> None:
    """A fixture written without timings still replays, all at once."""
    path = tmp_path / "untimed.jsonl"
    path.write_text('{"kind": "danmaku", "text": "没时间戳"}\n', encoding="utf-8")
    assert [at for at, _ in read_fixture(path)] == [0.0]


def test_read_fixture_raises_on_malformed_json(tmp_path: Path) -> None:
    """Error path. read_fixture is a generator, so nothing happens until it is
    iterated — pinning that here stops a future caller from wrapping the call and
    thinking it caught the error."""
    path = tmp_path / "broken.jsonl"
    path.write_text('{"kind": "danmaku"\n', encoding="utf-8")
    reader = read_fixture(path)
    with pytest.raises(json.JSONDecodeError):
        list(reader)


def test_read_fixture_raises_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(read_fixture(tmp_path / "not_here.jsonl"))


# ------------------------------------------------------------ fixture()


def test_fixture_resolves_a_shipped_name() -> None:
    path = fixture("quiet_stream.jsonl")
    assert path.parent == FIXTURE_DIR
    assert path.is_file()


def test_fixture_raises_for_an_unknown_name() -> None:
    """Error path. The message carries the full path, because the usual cause is a
    typo and the second-most-usual is looking in the wrong directory."""
    with pytest.raises(FileNotFoundError, match=r"nope\.jsonl"):
        fixture("nope.jsonl")


# ------------------------------------------------------------ ReplaySource


async def test_replay_source_stops_mid_file() -> None:
    """stop() has to cut a replay short, or a test that has seen enough waits out
    the other 198 events."""
    source = ReplaySource(path=fixture("event_flood.jsonl"), speed=0)
    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)
        if len(seen) == 5:
            await source.stop()

    async with asyncio.timeout(5.0):
        await source.start(sink)

    assert len(seen) == 5
    assert len(_replay("event_flood.jsonl")) > 5  # there really was more to emit


async def test_stop_before_start_does_not_pre_arm_the_flag() -> None:
    """Boundary. stop() on a source that never started is a no-op, and start()
    afterwards replays the whole file — start() builds a fresh stop flag, so a
    ReplaySource is reusable rather than one-shot."""
    source = ReplaySource(path=fixture("quiet_stream.jsonl"), speed=0)
    await source.stop()

    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    async with asyncio.timeout(5.0):
        await source.start(sink)

    assert len(seen) == len(_replay("quiet_stream.jsonl"))


async def test_a_zero_speed_replay_still_yields_between_events() -> None:
    """speed=0 skips the waiting, not the scheduling. A source that never awaits
    starves the consumer it is feeding."""
    source = ReplaySource(path=fixture("quiet_stream.jsonl"), speed=0)
    order: list[str] = []

    async def other() -> None:
        order.append("other")

    async def sink(event: LiveEvent) -> None:
        order.append(event.event_id)

    task = asyncio.create_task(other())
    async with asyncio.timeout(5.0):
        await source.start(sink)
    await task

    assert order[0] == "other"  # the very first event waited its turn


# ------------------------------------------------------------ the fixture set


def test_the_shipped_fixtures_are_exactly_the_ones_the_plan_names() -> None:
    """§10.2 ties every fixture to an interaction level. One that nobody documented
    is one nobody can tell has drifted."""
    on_disk = sorted(p.name for p in FIXTURE_DIR.glob("*.jsonl"))
    assert on_disk == sorted(FIXTURE_NAMES)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_parses_into_live_events(name: str) -> None:
    """The cheapest thing that could go wrong: a typo in an event kind sits quietly
    in the file until stage 3 tries to use it."""
    timeline = _timeline(name)
    assert timeline, f"{name} is empty"
    kinds = set(EventKind)
    for at_s, event in timeline:
        assert isinstance(event, LiveEvent)
        assert event.kind in kinds
        assert at_s >= 0.0


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_event_has_its_own_dedup_key(name: str) -> None:
    """Two events sharing a dedup key inside one fixture would make a dedup test
    pass by accident."""
    events = _replay(name)
    keys = [event.dedup_key for event in events]
    assert len(set(keys)) == len(keys)
    assert all(event.event_id for event in events)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            n,
            marks=(
                pytest.mark.xfail(reason=FLOOD_TIMELINE_IS_OUT_OF_ORDER, strict=True)
                if n == "event_flood.jsonl"
                else ()
            ),
        )
        for n in FIXTURE_NAMES
    ],
)
def test_fixture_timelines_never_run_backwards(name: str) -> None:
    """File order is replay order, so at_s going backwards means the fixture no
    longer plays the scene it records.

    ReplaySource hides it — `max(0.0, at_s - cursor)` turns the rewind into a zero
    delay (tests/fakes/replay.py:133) — which is why it needs asserting here.
    """
    moments = [at_s for at_s, _ in _timeline(name)]
    assert moments == sorted(moments)


# ------------------------------------------------------------ what each one is for


def test_anonymous_masked_is_all_uid_zero_and_still_usable() -> None:
    """§10.2: proves masking does not silence the stream. That only holds if every
    event really is masked and every masked viewer still has a distinct handle."""
    events = _replay("anonymous_masked.jsonl")
    assert len(events) >= 10
    assert all(event.viewer.uid == 0 for event in events)
    assert all(event.is_anonymous for event in events)
    assert all(event.viewer.identity != "anon" for event in events)
    assert (
        len({event.viewer.identity for event in events}) > 1
    ), "masking must not collapse the room into one person"
    assert all(event.kind is EventKind.DANMAKU and event.text for event in events)


def test_quiet_stream_goes_quiet_long_enough_to_trigger_a_topic() -> None:
    """§10.2 L1: the proactive topic fires after 45-180s of silence (§2.7), so the
    fixture is only useful if it contains a gap in that range."""
    moments = [at_s for at_s, _ in _timeline("quiet_stream.jsonl")]
    gaps = [b - a for a, b in itertools.pairwise(moments)]
    assert max(gaps) >= 45.0
    assert len(moments) <= 5, "a quiet stream with a crowd in it is not a quiet stream"


def test_superchat_lands_mid_stream_and_is_paid() -> None:
    """§10.2 L2: the super chat must arrive while something else is in flight, or
    there is nothing to preempt and nothing to requeue."""
    events = _replay("superchat_during_speech.jsonl")
    kinds = [event.kind for event in events]
    assert kinds.count(EventKind.SUPER_CHAT) == 1
    index = kinds.index(EventKind.SUPER_CHAT)
    assert 0 < index < len(kinds) - 1
    super_chat = events[index]
    assert super_chat.is_paid
    assert super_chat.text, "a super chat with no words is nothing to answer"


def test_gift_combo_is_one_combo_from_one_viewer() -> None:
    """§10.2 L2: aggregating into a single thank-you only means something if the
    frames belong to one combo from one person and the last one closes it."""
    events = _replay("gift_combo.jsonl")
    gifts = [event.gift for event in events if event.gift is not None]
    assert len(gifts) == len(events) >= 3
    assert len({gift.combo_id for gift in gifts}) == 1
    assert all(gift.combo_id for gift in gifts)
    assert len({event.viewer.identity for event in events}) == 1
    counts = [gift.combo_count for gift in gifts]
    assert counts == sorted(counts)
    assert len(set(counts)) == len(counts)
    # The closing frame lands 1.3s after the one before it (gift_combo.jsonl:4-5),
    # past the 1s idle settlement in §2.7's funnel. Whoever writes the aggregator has
    # to close this combo on combo_end, not on the idle timer, or the fixture ends up
    # asserting two thank-yous.
    assert [gift.combo_end is True for gift in gifts] == [False] * (len(gifts) - 1) + [True]
    assert all(event.is_paid for event in events)


def test_event_flood_is_dense_enough_to_exercise_the_funnel() -> None:
    """§10.2 L3: the funnel in §2.7 only converges under tens of events per second.
    A fixture that trickles measures nothing."""
    timeline = _timeline("event_flood.jsonl")
    moments = [at_s for at_s, _ in timeline]
    assert len(timeline) >= 150
    assert max(_window_count(moments, at_s, 1.0) for at_s in moments) >= 15
    assert any(event.is_paid for _, event in timeline)
    assert (
        len({event.viewer.identity for _, event in timeline}) >= 20
    ), "per-uid rate limiting needs a crowd, not one person shouting"


def test_presence_has_a_quiet_head_a_burst_and_a_repeat_visitor() -> None:
    """§10.2 L2+L4 needs both densities in one file: the low stretch to check the
    guard is greeted by name exactly once per stream, the burst to check we do not
    turn into a greeting machine."""
    timeline = _timeline("presence.jsonl")
    vips = [event for _, event in timeline if event.kind is EventKind.VIP_ENTER]
    entries = [at_s for at_s, event in timeline if event.kind is EventKind.ENTRY]

    assert len(vips) >= 2
    assert (
        len({vip.viewer.identity for vip in vips}) == 1
    ), "greeting once per stream is only testable if the same guard enters twice"
    assert all(is_vip_entry(vip.viewer) for vip in vips)

    assert len(entries) >= 100
    burst_start = next((at_s for at_s in entries if _window_count(entries, at_s, 5.0) >= 30), None)
    assert burst_start is not None, "no burst: the high-density half is missing"
    assert (
        len([at_s for at_s, _ in timeline if at_s < burst_start]) <= 5
    ), "the low-density stretch is where a repeated greeting would show up"


async def test_returning_viewer_brings_the_same_uids_back_every_stream() -> None:
    """§10.2: the Tier-0 streams_seen counter is what this fixture is for, and it
    only counts if the same cohort comes back. loop_count is how one file becomes
    three streams."""
    source = ReplaySource(path=fixture("returning_viewer.jsonl"), speed=0, loop_count=3)
    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    async with asyncio.timeout(5.0):
        await source.start(sink)

    once = _replay("returning_viewer.jsonl")
    assert len(seen) == len(once) * 3
    cohort = {event.viewer.identity for event in once}
    assert len(cohort) >= 3
    for stream in range(3):
        pass_events = seen[stream * len(once) : (stream + 1) * len(once)]
        assert {event.viewer.identity for event in pass_events} == cohort
    assert any(
        event.is_paid for event in once
    ), "Tier-0 remembers what a viewer spent, so the cohort needs a paid event"


def test_injection_attempt_carries_every_attack_shape_the_plan_names() -> None:
    """§10.2 names three shapes: instruction override, "you are now ...", and a
    closing-tag break-out. The last one is only an attack while it closes the tag
    §4.5 actually opens."""
    events = _replay("injection_attempt.jsonl")
    texts = [event.text for event in events]
    assert any("忽略" in t and "指令" in t for t in texts)
    assert any("你是" in t for t in texts)
    assert any(f"</{WRAPPER_TAG}>" in t for t in texts)
    assert any(
        event.is_paid for event in events
    ), "the paid lane is the dangerous carrier — it skips the danmaku funnel"
