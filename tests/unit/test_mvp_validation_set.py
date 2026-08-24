"""Integrity checks for the MVP validation cards and replay fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from bilisama.director.intent import Priority
from bilisama.director.intents import intent_for
from bilisama.ingest.events import EventKind, LiveEvent
from tests.fakes.replay import FIXTURE_DIR, read_fixture

MVP_DIR = FIXTURE_DIR / "mvp"
MANIFEST_PATH = MVP_DIR / "manifest.json"


class CandidateCard(TypedDict):
    id: str
    name: str
    url: str
    working_profile: str
    status: str


class CaseCard(TypedDict):
    id: str
    module: str
    mode: str
    feature: str
    title: str
    fixture: str | None
    stimulus: str
    expected: str
    gate: str
    candidate: NotRequired[str]


class Manifest(TypedDict):
    schema_version: int
    description: str
    candidates: list[CandidateCard]
    cases: list[CaseCard]


def _manifest() -> Manifest:
    return cast(Manifest, json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


def _fixture_path(reference: str) -> Path:
    path = (FIXTURE_DIR / reference).resolve()
    assert path.is_relative_to(FIXTURE_DIR.resolve()), reference
    return path


def _events(reference: str) -> list[LiveEvent]:
    return [event for _, event in read_fixture(_fixture_path(reference))]


def test_manifest_has_unique_ids_and_complete_cards() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    cases = manifest["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert len(cases) >= 50
    assert {case["module"] for case in cases} == {"functional", "business", "boundary"}
    assert {"sandbox", "real_room", "automated", "manual"} <= {case["mode"] for case in cases}
    for case in cases:
        assert all(
            case[field].strip()
            for field in (
                "id",
                "module",
                "mode",
                "feature",
                "title",
                "stimulus",
                "expected",
                "gate",
            )
        )


def test_each_business_candidate_has_a_profile_and_four_cases() -> None:
    manifest = _manifest()
    candidates = manifest["candidates"]
    candidate_ids = {candidate["id"] for candidate in candidates}
    assert len(candidates) == 6
    assert len(candidate_ids) == len(candidates)
    assert all(candidate["url"].startswith("https://") for candidate in candidates)

    business = [case for case in manifest["cases"] if case["module"] == "business"]
    assert all(case.get("candidate") in candidate_ids for case in business)
    counts = {candidate_id: 0 for candidate_id in candidate_ids}
    for case in business:
        candidate = case.get("candidate")
        assert candidate is not None
        counts[candidate] += 1
    assert set(counts.values()) == {4}


def test_every_referenced_replay_fixture_exists_and_is_well_formed() -> None:
    references = {case["fixture"] for case in _manifest()["cases"] if case["fixture"] is not None}
    assert references
    for reference in sorted(references):
        path = _fixture_path(reference)
        assert path.is_file(), reference
        timeline = list(read_fixture(path))
        assert timeline, reference
        moments = [at_s for at_s, _ in timeline]
        assert moments == sorted(moments), reference
        event_ids = [event.event_id for _, event in timeline]
        assert all(event_ids), reference
        assert len(event_ids) == len(set(event_ids)), reference


def test_full_event_fixture_covers_the_runtime_taxonomy() -> None:
    assert {event.kind for event in _events("mvp/functional_all_events.jsonl")} == set(EventKind)


def test_priority_collision_fixture_pins_the_configured_ladder() -> None:
    events = {event.event_id: event for event in _events("mvp/priority_collision.jsonl")}

    def priority(event_id: str) -> Priority:
        intent = intent_for(events[event_id], now=1.0)
        assert intent is not None
        return intent.priority

    assert priority("pc-dm-1") is Priority.DANMAKU
    assert priority("pc-gift-normal") is Priority.DANMAKU
    assert priority("pc-gift-medium") is Priority.VIP_ENTER
    assert priority("pc-vip-1") is Priority.VIP_ENTER
    assert priority("pc-guard-1") is Priority.GUARD_BUY
    assert priority("pc-gift-high") is Priority.BIG_GIFT
    assert priority("pc-sc-1") is Priority.SUPERCHAT


def test_business_fixtures_are_domain_crowds_not_single_prompts() -> None:
    references = {
        case["fixture"]
        for case in _manifest()["cases"]
        if case["module"] == "business" and case["fixture"] is not None
    }
    assert len(references) == 6
    for reference in references:
        events = _events(reference)
        assert len(events) >= 9, reference
        assert sum(event.kind is EventKind.DANMAKU for event in events) >= 7, reference
        assert any(event.kind is EventKind.GIFT and event.is_paid for event in events), reference
        assert any(
            event.kind is EventKind.SUPER_CHAT and event.is_paid for event in events
        ), reference
        assert len({event.viewer.identity for event in events}) == len(events), reference


def test_uncertain_candidate_profiles_have_a_confirmation_gate() -> None:
    manifest = _manifest()
    uncertain = {
        candidate["id"] for candidate in manifest["candidates"] if "确认" in candidate["status"]
    }
    confirmation_cases = {
        case.get("candidate")
        for case in manifest["cases"]
        if case["feature"] == "profile_confirmation"
    }
    assert uncertain == confirmation_cases == {"qingyue_ai_news", "xiaoming_model_eval"}
