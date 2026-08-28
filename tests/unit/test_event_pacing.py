"""Dynamic live-event pacing: room load bands and the shared ordinary budget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import Chattiness
from bilisama.event_pacing import EventPacer, RoomActivity
from tests.fakes.bili import danmaku_event, entry_event, gift_event


def _clock() -> FakeClock:
    return FakeClock(wall=datetime(2026, 8, 27, 12, 0, tzinfo=UTC))


def _pacer(level: Chattiness = Chattiness.MEDIUM) -> tuple[EventPacer, FakeClock]:
    clock = _clock()
    return EventPacer(clock, chattiness=lambda: level), clock


def _load(
    pacer: EventPacer, count: int, *, clock: FakeClock | None = None, step_s: float = 0.0
) -> None:
    for uid in range(1, count + 1):
        pacer.note_event(danmaku_event(f"问题 {uid} 是什么？", uid=uid))
        if clock is not None:
            clock._now += step_s


def _load_state(pacer: EventPacer, clock: FakeClock, state: RoomActivity) -> None:
    if state is RoomActivity.SPARSE:
        _load(pacer, 3)
    elif state is RoomActivity.ACTIVE:
        # Spread out so the 10s burst rule does not tip it into BUSY.
        _load(pacer, 9, clock=clock, step_s=2.0)
    elif state is RoomActivity.BUSY:
        _load(pacer, 31)


@pytest.mark.parametrize(
    ("count", "state"),
    [
        (0, RoomActivity.QUIET),
        (3, RoomActivity.SPARSE),
        (9, RoomActivity.ACTIVE),
        (31, RoomActivity.BUSY),
    ],
)
def test_room_activity_is_derived_only_from_live_events(count: int, state: RoomActivity) -> None:
    pacer, _clock_obj = _pacer()
    if state is RoomActivity.ACTIVE:
        _load(pacer, count, clock=_clock_obj, step_s=2.0)
    else:
        _load(pacer, count)
    assert pacer.snapshot().activity is state


def test_console_events_do_not_move_the_room_band() -> None:
    """room_id <= 0 means an injected console event, not the platform. A dev
    hammering the inject box must not convince the pacer the room is busy."""
    pacer, _clock_obj = _pacer()
    for uid in range(40):
        pacer.note_event(danmaku_event(f"本地注入 {uid}", uid=uid, room_id=0))
    assert pacer.snapshot().activity is RoomActivity.QUIET


def test_chattiness_is_a_relative_event_frequency_not_a_fixed_timer() -> None:
    rows: dict[Chattiness, list[float]] = {}
    for level in Chattiness:
        windows: list[float] = []
        for state in RoomActivity:
            pacer, clock = _pacer(level)
            _load_state(pacer, clock, state)
            windows.append(pacer.snapshot().danmaku_window_s)
        rows[level] = windows

    assert rows[Chattiness.HIGH][0] < rows[Chattiness.MEDIUM][0] < rows[Chattiness.LOW][0]
    assert rows[Chattiness.HIGH][-1] < rows[Chattiness.MEDIUM][-1] < rows[Chattiness.LOW][-1]
    for windows in rows.values():
        assert windows == sorted(windows), "the busier room must collect longer before speaking"


def test_ordinary_budget_allows_a_short_burst_then_throttles_without_a_cooldown() -> None:
    pacer, clock = _pacer(Chattiness.MEDIUM)
    assert pacer.try_consume("danmaku")
    assert pacer.try_consume("entry")
    assert not pacer.try_consume("danmaku")

    clock._now += pacer.snapshot().budget_refill_s
    assert pacer.try_consume("danmaku"), "one opportunity refills without a fixed post-reply wait"


def test_paid_and_vip_events_never_consume_the_ordinary_budget() -> None:
    pacer, _clock_obj = _pacer(Chattiness.LOW)
    before = pacer.status()["ordinary_tokens"]
    for lane in ("gift", "super_chat", "guard_buy", "vip_enter"):
        assert pacer.try_consume(lane)
    assert pacer.status()["ordinary_tokens"] == before


def test_can_consume_checks_without_spending() -> None:
    pacer, _clock_obj = _pacer(Chattiness.LOW)
    assert pacer.can_consume("danmaku")
    assert pacer.can_consume("danmaku"), "a check is not a spend"
    assert pacer.try_consume("danmaku")
    assert not pacer.can_consume("danmaku")


def test_repeated_entry_packets_from_one_viewer_do_not_fake_a_room_surge() -> None:
    pacer, _clock_obj = _pacer()
    for _ in range(20):
        pacer.note_event(entry_event(42))
    assert pacer.snapshot().activity is RoomActivity.QUIET


def test_one_gift_combo_does_not_fake_eight_paid_interactions() -> None:
    pacer, _clock_obj = _pacer()
    for _ in range(20):
        pacer.note_event(gift_event(uid=7, gift_id=88))
    assert pacer.snapshot().activity is RoomActivity.QUIET


def test_downshift_holds_for_thirty_seconds_before_relaxing() -> None:
    """One quiet gap between waves must not whipsaw the windows: the band
    steps down only after the calm has lasted the hold."""
    pacer, clock = _pacer()
    _load(pacer, 31)
    assert pacer.snapshot().activity is RoomActivity.BUSY

    clock._now += 61.0  # every sample ages out of the 60s window
    assert pacer.snapshot().activity is RoomActivity.BUSY, "the hold keeps the old band"

    clock._now += 31.0
    assert pacer.snapshot().activity is RoomActivity.QUIET


def test_three_minute_policy_matrix_livens_empty_rooms_without_crowding_busy_ones() -> None:
    opportunities: dict[Chattiness, dict[RoomActivity, float]] = {}
    for level in Chattiness:
        opportunities[level] = {}
        for state in RoomActivity:
            pacer, clock = _pacer(level)
            _load_state(pacer, clock, state)
            policy = pacer.snapshot()
            opportunities[level][state] = policy.budget_capacity + 180 / policy.budget_refill_s
            if state is RoomActivity.BUSY:
                assert not policy.proactive_enabled
            else:
                assert policy.proactive_enabled

        assert opportunities[level][RoomActivity.QUIET] > opportunities[level][RoomActivity.BUSY]

    for state in RoomActivity:
        assert (
            opportunities[Chattiness.LOW][state]
            < opportunities[Chattiness.MEDIUM][state]
            < opportunities[Chattiness.HIGH][state]
        )
