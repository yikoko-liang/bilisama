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
        ("functional", 18),
        ("business", 36),
    ]
    functional = catalog.sets[0]
    covered = {event.kind for case in functional.cases for event in case.events}
    assert covered == set(EventKind)


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
        assert kinds == set(EventKind), candidate_id


def test_pressure_cases_expand_but_keep_the_ui_preview_compact() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    flood = catalog.case("biz.bettergi.maintainer-flood")
    entry = catalog.case("biz.shitong.demo-entry-flood")

    assert len(flood.timeline()) == 122
    assert len(entry.timeline()) == 200
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


def test_unknown_case_is_a_user_facing_error() -> None:
    catalog = load_test_catalog(_ROOT / "config" / "testsets")
    with pytest.raises(ValueError, match="找不到测试用例"):
        catalog.case("does.not.exist")
