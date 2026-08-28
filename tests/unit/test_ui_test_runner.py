"""The UI test console injects its fixtures through the production event source."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.ingest.events import EventKind, LiveEvent
from bilisama.ingest.sources import QueueSource
from bilisama.ui.test_runner import MockEvent, MockTestRunner, load_test_catalog

_ROOT = Path(__file__).resolve().parents[2]


def test_catalog_has_exactly_two_sets_and_covers_every_event_kind() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")

    assert [(test_set.id, len(test_set.cases)) for test_set in catalog.sets] == [
        ("functional", 27),
        ("business", 36),
    ]
    functional = catalog.sets[0]
    covered = {event.kind for case in functional.cases for event in case.events}
    assert covered == set(EventKind) - {EventKind.VIP_ENTER}


def test_business_set_has_six_grounded_scenarios_per_candidate() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    cases = catalog.sets[1].cases
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.candidate_id] = counts.get(case.candidate_id, 0) + 1

    assert counts == {
        "ai-code-tudou": 6,
        "bettergi": 6,
        "qingyue": 6,
        "shitong": 6,
        "ai-suifeng": 6,
        "xiaoming": 6,
    }

    for candidate_id in counts:
        kinds = {
            item.kind
            for case in cases
            if case.candidate_id == candidate_id
            for _at_s, item in case.timeline()
            if isinstance(item, MockEvent)
        }
        assert kinds == set(EventKind) - {EventKind.VIP_ENTER}, candidate_id


def test_every_mock_gift_uses_explicit_consistent_battery_data() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    for test_set in catalog.sets:
        for case in test_set.cases:
            for _at_s, item in case.timeline():
                if not isinstance(item, MockEvent) or item.gift is None:
                    continue
                gift = item.gift
                assert gift.unit_battery >= 1, case.id
                assert gift.total_coin == gift.unit_battery * gift.num * 100, case.id
                assert item.value_cny == pytest.approx(gift.total_coin / 1000), case.id
                summary = item.summary()
                assert not summary.endswith("· 0 电池"), case.id
                assert "¥" not in summary, case.id


def test_entry_mocks_exercise_platform_identity_promotion() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    functional = catalog.sets[0]
    events = [
        item
        for case in functional.cases
        for _at_s, item in case.timeline()
        if isinstance(item, MockEvent)
    ]
    assert any(
        event.kind is EventKind.ENTRY
        and event.viewer.guard_level.value in {"captain", "admiral", "governor"}
        for event in events
    )
    assert any(
        event.kind is EventKind.ENTRY
        and event.viewer.medal is not None
        and event.viewer.medal.level >= 5
        for event in events
    )
    assert all(event.kind is not EventKind.VIP_ENTER for event in events)


def test_latest_functional_cases_cover_recent_prompt_pacing_and_anchor_changes() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    ids = {case.id for case in catalog.sets[0].cases}
    assert {
        "func.danmaku.agency",
        "func.danmaku.anchor-ignore",
        "func.entry.vip-tiers",
        "func.pacing.busy-room",
        "func.control.pause",
    } <= ids


def test_pressure_cases_expand_but_keep_the_ui_preview_compact() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    flood = catalog.case("biz.bettergi.maintainer-flood")
    entry = catalog.case("biz.shitong.demo-entry-flood")
    pacing = catalog.case("func.pacing.busy-room")

    assert len(flood.timeline()) == 122
    assert len(entry.timeline()) == 200
    assert len(pacing.timeline()) == 33
    public = flood.public()
    assert public["event_count"] == 122
    assert len(public["events"]) == 3  # type: ignore[arg-type]


async def test_runner_injects_timed_events_and_reports_completion() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    clock = FakeClock()
    source = QueueSource("test")
    seen: list[LiveEvent] = []
    statuses: list[dict[str, object]] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    source_task = asyncio.create_task(source.start(sink))
    runner = MockTestRunner(catalog, source, clock, statuses.append)
    await runner.start("func.sc.preempt")
    await clock.advance(0)
    assert [event.kind for event in seen] == [EventKind.DANMAKU]
    assert runner.state()["status"] == "event"

    await clock.advance(2)
    assert [event.kind for event in seen] == [EventKind.DANMAKU, EventKind.SUPER_CHAT]
    await clock.advance(12)
    assert runner.state()["status"] == "completed"
    assert statuses[-1]["text"] == "事件已注入，请按预期人工判断"

    await source.stop()
    await source_task


async def test_rerun_mints_fresh_event_ids_and_stop_cancels_the_timer() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    clock = FakeClock()
    source = QueueSource("test")
    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    source_task = asyncio.create_task(source.start(sink))
    runner = MockTestRunner(catalog, source, clock, lambda _data: None)
    await runner.start("func.voice.interrupt")
    await clock.advance(0)
    await runner.stop()
    await runner.start("func.voice.interrupt")
    await clock.advance(0)
    await runner.stop()

    assert len(seen) == 2
    assert seen[0].event_id != seen[1].event_id
    assert runner.state()["status"] == "stopped"

    await source.stop()
    await source_task


async def test_dedup_groups_and_sc_revoke_actions_reach_the_production_keys() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    clock = FakeClock()
    source = QueueSource("test")
    seen: list[LiveEvent] = []
    revoked: list[str] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    source_task = asyncio.create_task(source.start(sink))
    runner = MockTestRunner(catalog, source, clock, lambda _data: None, revoke=revoked.append)
    await runner.start("biz.shitong.support-dedup")
    await clock.advance(0.1)
    assert len(seen) == 2
    assert seen[0].event_id == seen[1].event_id
    await runner.stop()

    await runner.start("biz.qingyue.sc-revoke")
    await clock.advance(1.4)
    assert revoked == ["super_chat:ui-test:2:biz.qingyue.sc-revoke:qingyue-queued"]
    await runner.stop()
    await source.stop()
    await source_task


async def test_entry_pressure_run_injects_all_two_hundred_unique_viewers() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    clock = FakeClock()
    source = QueueSource("test", maxsize=512)
    seen: list[LiveEvent] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    source_task = asyncio.create_task(source.start(sink))
    runner = MockTestRunner(catalog, source, clock, lambda _data: None)
    await runner.start("biz.shitong.demo-entry-flood")
    await clock.advance(14)

    assert len(seen) == 200
    assert len({event.viewer.identity for event in seen}) == 200
    assert runner.state()["status"] == "completed"
    await source.stop()
    await source_task


@pytest.mark.parametrize("set_id", ["functional", "business"])
async def test_every_shipped_case_runs_to_completion_and_injects_its_full_timeline(
    set_id: str,
) -> None:
    """Regression for the actual catalog, not a hand-picked sample.

    Voice-only cases legitimately inject no LiveEvent; they still have to
    finish and publish the same completed state the UI uses to unlock manual
    judgment. Event cases must deliver every event and platform action.
    """
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    test_set = next(item for item in catalog.sets if item.id == set_id)
    clock = FakeClock()
    source = QueueSource("test-all", maxsize=2048)
    seen: list[LiveEvent] = []
    revoked: list[str] = []
    statuses: list[dict[str, object]] = []

    async def sink(event: LiveEvent) -> None:
        seen.append(event)

    source_task = asyncio.create_task(source.start(sink))
    runner = MockTestRunner(catalog, source, clock, statuses.append, revoke=revoked.append)
    try:
        for case in test_set.cases:
            before_events = len(seen)
            before_revokes = len(revoked)
            expected_events = [
                item for _at_s, item in case.timeline() if isinstance(item, MockEvent)
            ]
            expected_actions = [
                item for _at_s, item in case.timeline() if not isinstance(item, MockEvent)
            ]

            await runner.start(case.id)
            await clock.advance(case.duration_s + 0.01)
            await asyncio.sleep(0)

            assert runner.state()["status"] == "completed", case.id
            injected = seen[before_events:]
            assert len(injected) == len(expected_events), case.id
            assert all(event.raw == {"mock_test": case.id} for event in injected), case.id
            assert len(revoked) - before_revokes == len(expected_actions), case.id
            assert statuses[-1]["case_id"] == case.id
            assert statuses[-1]["text"] == "事件已注入，请按预期人工判断"
    finally:
        await runner.stop()
        await source.stop()
        await source_task


def test_unknown_case_is_a_user_facing_error() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    with pytest.raises(ValueError, match="找不到测试用例"):
        catalog.case("does.not.exist")
