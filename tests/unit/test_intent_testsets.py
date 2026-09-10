"""Validate runnable intent fixtures without sending answer keys to a model."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast

import pytest
from pydantic import ValidationError

from bilisama.ingest.events import EventKind, GuardLevel
from bilisama.ui.test_runner import (
    MockEvent,
    MockTestCase,
    MockTestCatalog,
    ScenarioEvent,
    ScenarioStep,
    load_test_catalog,
)

_ROOT = Path(__file__).resolve().parents[2]
_TESTSETS = _ROOT / "config" / "testsets"
_LABELS = {"TO_ME", "AUDIENCE", "SELF_TALK", "READING", "GUEST", "UNSURE", "DECLINED"}
_SETUP_ROWS = {2, 7, 13, 18, 23, 28, 33, 37, 42, 50, 63, 68, 74, 83, 89}


def test_anchor_answer_case_has_real_ordered_signals(intent_catalog: MockTestCatalog) -> None:
    case = intent_catalog.case("hard-19")
    assert not case.source_rows
    assert not case.allow_proactive
    voice, wait = case.steps
    assert voice.kind == "voice" and wait.kind == "wait"
    question, answer = voice.events_during
    assert question.offset_s < answer.offset_s
    assert question.event.kind is answer.event.kind is EventKind.DANMAKU
    assert not question.event.viewer.is_anchor and answer.event.viewer.is_anchor
    assert question.event.route == answer.event.route == "crowd"
    assert question.event.viewer.name in answer.event.text
    assert wait.observe_s >= 15


_SOURCE_RANGES = (
    (2, 6),
    (7, 12),
    (13, 17),
    (18, 22),
    (23, 27),
    (28, 32),
    (33, 36),
    (37, 41),
    (42, 47),
    (48, 49),
    (50, 57),
    (58, 62),
    (63, 67),
    (68, 73),
    (74, 77),
    (78, 82),
    (83, 88),
    (89, 91),
)


class _SourceRow(TypedDict):
    source_row: int
    source_round: int | None
    source_text: str
    source_expected_short: str | None
    source_expected_detail: str | None


class _ExecutionNote(TypedDict):
    step_id: str
    source_row: int
    required_reply_source_row: int | None


class _SourceGroup(TypedDict):
    id: str
    reference: str
    source_rows: list[int]
    source_trace: list[_SourceRow]
    execution_notes: list[_ExecutionNote]


class _SourceReport(TypedDict):
    source: str
    header: dict[str, object]
    groups: list[_SourceGroup]
    setup_rows: list[int]
    behavior_rows: list[int]
    summary: dict[str, object]


def _event_payload() -> dict[str, object]:
    return {
        "at_s": 0,
        "kind": "danmaku",
        "viewer": {"uid": 1001, "name": "小禾"},
        "text": "这个工具离线也能用吗？",
    }


def _step_payload() -> dict[str, object]:
    return {
        "id": "s1",
        "kind": "voice",
        "text": "Mia，你帮我看看这个思路。",
        "expected": "主播说完后开口，回复主播。",
    }


def _case_payload() -> dict[str, object]:
    return {
        "id": "schema.valid",
        "group": "结构验证",
        "title": "语音和事件共存",
        "operator": "播放台词并观察实际回复。",
        "expected": ["语音输入只包含真实台词。"],
        "duration_s": 30,
        "steps": [_step_payload()],
    }


@pytest.fixture
def intent_catalog() -> MockTestCatalog:
    return load_test_catalog(_TESTSETS, intent=True)


@pytest.fixture
def source_report() -> _SourceReport:
    payload = json.loads((_TESTSETS / "intent-source-map.json").read_text(encoding="utf-8"))
    return cast(_SourceReport, payload)


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
def test_voice_step_rejects_missing_spoken_text(text: str) -> None:
    payload = _step_payload() | {"text": text}
    with pytest.raises(ValidationError, match="语音步骤必须包含实际台词"):
        ScenarioStep.model_validate(payload)


def test_event_step_requires_an_event() -> None:
    with pytest.raises(ValidationError, match="只有事件步骤必须提供 event"):
        ScenarioStep.model_validate(_step_payload() | {"kind": "event", "text": ""})


@pytest.mark.parametrize("kind", ["voice", "wait", "proactive"])
def test_non_event_step_rejects_an_event_payload(kind: str) -> None:
    with pytest.raises(ValidationError, match="只有事件步骤必须提供 event"):
        ScenarioStep.model_validate(_step_payload() | {"kind": kind, "event": _event_payload()})


@pytest.mark.parametrize("kind", ["event", "wait", "proactive"])
def test_only_voice_can_inject_events_while_playing(kind: str) -> None:
    payload = _step_payload() | {
        "kind": kind,
        "text": "",
        "events_during": [{"offset_s": 0.5, "event": _event_payload()}],
    }
    if kind == "event":
        payload["event"] = _event_payload()
    with pytest.raises(ValidationError, match="只有语音步骤支持语音中注入事件"):
        ScenarioStep.model_validate(payload)


def test_voice_can_keep_overlapping_event_and_expectation_separate() -> None:
    step = ScenarioStep.model_validate(
        _step_payload()
        | {
            "events_during": [{"offset_s": 0.5, "event": _event_payload(), "source_row": 8}],
            "expected_intent": "TO_ME",
        }
    )

    assert step.text == "Mia，你帮我看看这个思路。"
    assert step.expected_intent == "TO_ME"
    assert step.events_during[0].event.text == "这个工具离线也能用吗？"
    assert step.events_during[0].source_row == 8
    assert step.event is None


@pytest.mark.parametrize("after", ["reply_started", "reply_finished"])
def test_reply_gate_keeps_the_trigger_source_row(after: str) -> None:
    step = ScenarioStep.model_validate(
        _step_payload() | {"after": after, "source_row": 6, "reply_from_row": 5}
    )
    assert step.after == after
    assert step.source_row == 6
    assert step.reply_from_row == 5


def test_reply_gate_rejects_a_negative_source_row() -> None:
    with pytest.raises(ValidationError):
        ScenarioStep.model_validate(_step_payload() | {"reply_from_row": -1})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delay_s", -0.1),
        ("delay_s", 180.1),
        ("observe_s", -0.1),
        ("observe_s", 180.1),
        ("timeout_s", 0),
        ("timeout_s", -1),
        ("timeout_s", 180.1),
    ],
)
def test_step_rejects_invalid_timing_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ScenarioStep.model_validate(_step_payload() | {field: value})


@pytest.mark.parametrize("delay_s", [0, 180])
@pytest.mark.parametrize("observe_s", [0, 180])
def test_step_accepts_timing_boundaries(delay_s: float, observe_s: float) -> None:
    step = ScenarioStep.model_validate(
        _step_payload() | {"delay_s": delay_s, "observe_s": observe_s, "timeout_s": 180}
    )
    assert (step.delay_s, step.observe_s, step.timeout_s) == (delay_s, observe_s, 180)


@pytest.mark.parametrize("offset_s", [-0.1, 180.1])
def test_overlapping_event_rejects_invalid_offsets(offset_s: float) -> None:
    with pytest.raises(ValidationError):
        ScenarioEvent.model_validate({"offset_s": offset_s, "event": _event_payload()})


@pytest.mark.parametrize("offset_s", [0, 180])
def test_overlapping_event_accepts_offset_boundaries(offset_s: float) -> None:
    event = ScenarioEvent.model_validate({"offset_s": offset_s, "event": _event_payload()})
    assert event.offset_s == offset_s


@pytest.mark.parametrize(
    "update",
    [
        {"kind": "pretend_voice"},
        {"after": "pretend_reply"},
        {"expected_intent": "MADE_UP_INTENT"},
        {"id": ""},
        {"expected": ""},
        {"model_instruction": "请按预期分类"},
    ],
)
def test_step_rejects_unknown_kinds_labels_and_extra_fields(update: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScenarioStep.model_validate(_step_payload() | update)


def test_case_rejects_duplicate_step_ids() -> None:
    with pytest.raises(ValidationError, match="步骤 id 不能重复"):
        MockTestCase.model_validate(_case_payload() | {"steps": [_step_payload(), _step_payload()]})


@pytest.mark.parametrize("legacy_field", ["events", "bursts", "actions"])
def test_case_rejects_mixing_steps_with_a_legacy_timeline(legacy_field: str) -> None:
    timeline: dict[str, list[dict[str, object]]] = {
        "events": [_event_payload()],
        "bursts": [_event_payload() | {"duration_s": 1, "count": 2}],
        "actions": [{"at_s": 0, "kind": "sc_revoke", "target_group": "test-sc"}],
    }
    with pytest.raises(ValidationError, match="多轮语音步骤不能混用旧的固定时间线"):
        MockTestCase.model_validate(_case_payload() | {legacy_field: timeline[legacy_field]})


def test_case_public_view_does_not_claim_an_intent_classifier_exists() -> None:
    case = MockTestCase.model_validate(
        _case_payload() | {"context": ["刚才已说明工具只保存本地数据。"]}
    )
    public = case.public()

    assert public["execution"] == "voice"
    assert public["classification_available"] is False
    assert public["context"] == ["刚才已说明工具只保存本地数据。"]
    assert public["steps"] == [case.steps[0].model_dump(mode="json")]
    assert public["events"] == []


def test_catalog_contains_two_intent_sets_with_the_requested_counts(
    intent_catalog: MockTestCatalog,
) -> None:
    assert [(item.id, len(item.cases)) for item in intent_catalog.sets] == [
        ("simple", 14),
        ("hard", 19),
    ]
    cases = [case for test_set in intent_catalog.sets for case in test_set.cases]
    assert len({case.id for case in cases}) == 33
    assert all(case.steps for case in cases)
    assert all(not (case.events or case.bursts or case.actions) for case in cases)


def test_simple_set_has_two_cases_for_each_source_classification(
    intent_catalog: MockTestCatalog,
) -> None:
    cases = intent_catalog.sets[0].cases
    assert Counter(case.expected_intent for case in cases) == Counter(
        {label: 2 for label in _LABELS}
    )
    declined = [case for case in cases if case.expected_intent == "DECLINED"]
    assert len(declined) == 2
    for case in declined:
        assert any(step.kind == "proactive" for step in case.steps), case.id
        note = "\n".join([case.operator, *case.focus, *case.expected])
        assert "入口" in note, case.id
        assert any(word in note for word in ("未完成", "未接入", "缺少", "不支持")), case.id
        assert case.public()["classification_available"] is False


@pytest.mark.parametrize(
    ("case_id", "kind", "name"),
    [("simple-07", EventKind.DANMAKU, "小松"), ("simple-08", EventKind.GIFT, "小灯")],
)
def test_reading_cases_inject_the_referenced_platform_event_before_host_speech(
    intent_catalog: MockTestCatalog, case_id: str, kind: EventKind, name: str
) -> None:
    case = intent_catalog.case(case_id)
    assert [step.kind for step in case.steps] == ["event", "voice"]
    signal, voice = case.steps
    assert signal.event is not None
    assert signal.event.kind is kind
    assert signal.event.viewer.name == name
    assert signal.event.route == "crowd"
    assert signal.text == ""
    assert signal.observe_s == 0
    assert voice.after == "delay"
    assert voice.delay_s <= 0.1
    assert voice.expected_intent == "READING"
    assert case.context == [], "平台事件通过真实信号进入上下文，不再伪装成已发生的背景文字"
    if kind is EventKind.DANMAKU:
        assert signal.event.text == "这个工具会上传文件吗？"
        assert "小松问" in voice.text
    else:
        gift = signal.event.gift
        assert gift is not None
        assert (gift.name, gift.num, gift.unit_battery) == ("小花花", 3, 1)
        assert "谢谢小灯" in voice.text


def test_hard_platform_events_match_their_source_rows_and_preserve_identity(
    intent_catalog: MockTestCatalog,
) -> None:
    signals: list[tuple[int, MockEvent]] = []
    for case in intent_catalog.sets[1].cases:
        if not case.source_rows:
            continue
        for step in case.steps:
            if step.event is not None:
                signals.append((step.source_row, step.event))
            signals.extend((edge.source_row, edge.event) for edge in step.events_during)

    assert Counter(row for row, _event in signals) == {
        15: 1,
        25: 1,
        30: 1,
        32: 1,
        39: 1,
        44: 2,
        46: 2,
        48: 4,
        51: 1,
        52: 1,
        55: 1,
        56: 1,
        59: 1,
        60: 1,
        61: 1,
        62: 1,
        64: 1,
        66: 1,
        69: 1,
        70: 1,
        72: 1,
        73: 3,
        79: 1,
        85: 1,
        87: 1,
    }
    assert Counter(event.kind for _row, event in signals) == {
        EventKind.DANMAKU: 24,
        EventKind.GIFT: 2,
        EventKind.ENTRY: 6,
    }
    assert all(event.route == "crowd" for _row, event in signals)
    fleet = next(event for row, event in signals if row == 70)
    assert fleet.viewer.name == "南瓜"
    assert fleet.viewer.guard_level is GuardLevel.CAPTAIN
    for row, name, gift_name, num, battery in (
        (64, "小灯", "小花花", 3, 1),
        (66, "晚风", "鼓鼓掌", 1, 5),
    ):
        event = next(event for source_row, event in signals if source_row == row)
        assert event.viewer.name == name
        assert event.gift is not None
        assert (event.gift.name, event.gift.num, event.gift.unit_battery) == (
            gift_name,
            num,
            battery,
        )


def test_hard_set_preserves_all_workbook_groups_and_rows(
    intent_catalog: MockTestCatalog,
) -> None:
    cases = [case for case in intent_catalog.sets[1].cases if case.source_rows]
    assert [case.id for case in cases] == [f"hard-{index:02}" for index in range(1, 19)]
    all_rows: list[int] = []
    for case, (start, end) in zip(cases, _SOURCE_RANGES, strict=True):
        assert case.source_rows == list(range(start, end + 1)), case.id
        assert case.reference.endswith(f"测试集v0!A{start}:F{end}"), case.id
        all_rows.extend(case.source_rows)

    assert all_rows == list(range(2, 92))
    assert len(set(all_rows)) == 90


def test_hard_steps_trace_every_behavior_row_without_turning_setup_into_speech(
    intent_catalog: MockTestCatalog,
) -> None:
    observed_rows: set[int] = set()
    for case in intent_catalog.sets[1].cases:
        if not case.source_rows:
            continue
        for step in case.steps:
            rows = {step.source_row, *(item.source_row for item in step.events_during)}
            assert rows <= set(case.source_rows), (case.id, step.id, rows)
            assert not rows & _SETUP_ROWS, (case.id, step.id, rows)
            observed_rows.update(rows)

    assert observed_rows == set(range(2, 92)) - _SETUP_ROWS
    assert len(observed_rows) == 75


def test_hard_set_has_complete_voice_event_and_wait_inputs(
    intent_catalog: MockTestCatalog,
) -> None:
    steps = [
        step for case in intent_catalog.sets[1].cases if case.source_rows for step in case.steps
    ]

    assert len(steps) == 86
    assert Counter(step.kind for step in steps) == {"voice": 56, "event": 22, "wait": 8}
    assert sum(len(step.events_during) for step in steps) == 10
    assert Counter(step.after for step in steps) == {
        "delay": 80,
        "reply_started": 1,
        "reply_finished": 5,
    }
    assert all(step.text == "" for step in steps if step.kind != "voice")


def test_hard_barge_in_waits_for_actual_reply_start(
    intent_catalog: MockTestCatalog,
) -> None:
    steps = intent_catalog.case("hard-04").steps
    interrupt = next(step for step in steps if step.source_row == 21)
    before = steps[steps.index(interrupt) - 1]

    assert before.source_row == 20
    assert before.observe_s == 0
    assert interrupt.after == "reply_started"
    assert 0 < interrupt.delay_s <= 1
    assert interrupt.timeout_s > 0
    assert interrupt.text == "先停一下，这里我补充一下。"


def test_hard_optional_replies_are_not_rewritten_as_mandatory(
    intent_catalog: MockTestCatalog,
) -> None:
    steps = [step for case in intent_catalog.sets[1].cases for step in case.steps]
    row31 = next(step for step in steps if step.source_row == 31)
    row66 = next(step for step in steps if step.source_row == 66)

    assert "也可以保持沉默" in row31.expected
    assert "可选" in row66.expected
    assert "沉默" in row66.expected
    assert "再补" in row66.expected
    assert next(step for step in steps if step.source_row == 32).after == "reply_finished"


def test_source_map_preserves_original_headers_setup_and_behavior_rows(
    intent_catalog: MockTestCatalog, source_report: _SourceReport
) -> None:
    assert source_report["source"] == "bili-sama意图测试集(ing).xlsx"
    assert source_report["header"]["source_row"] == 1
    assert set(source_report["setup_rows"]) == _SETUP_ROWS
    assert set(source_report["behavior_rows"]) == set(range(2, 92)) - _SETUP_ROWS
    assert len(source_report["groups"]) == 18
    source_rows: list[int] = []
    for group in source_report["groups"]:
        case = intent_catalog.case(group["id"])
        assert group["reference"] == case.reference
        assert group["source_rows"] == case.source_rows
        assert [row["source_row"] for row in group["source_trace"]] == case.source_rows
        source_rows.extend(group["source_rows"])
        expected = "\n".join(case.expected)
        for row in group["source_trace"]:
            if row["source_row"] in _SETUP_ROWS:
                continue
            for value in (row["source_expected_short"], row["source_expected_detail"]):
                if value:
                    assert value.strip() in expected, (case.id, row["source_row"], value)

    assert source_rows == list(range(2, 92))
    assert source_report["summary"]["missing_behavior_rows"] == []
    assert source_report["summary"]["mapped_behavior_source_rows"] == 75


def test_source_map_does_not_invent_the_missing_sixth_round(
    intent_catalog: MockTestCatalog, source_report: _SourceReport
) -> None:
    group = next(item for item in source_report["groups"] if item["id"] == "hard-11")
    rounds = {row["source_row"]: row["source_round"] for row in group["source_trace"]}

    assert {row: rounds[row] for row in (55, 56, 57)} == {55: 5, 56: 7, 57: 8}
    assert 6 not in rounds.values()
    assert set(intent_catalog.case("hard-11").source_rows) == set(range(50, 58))


def test_required_reply_gates_reference_the_original_trigger_row(
    intent_catalog: MockTestCatalog, source_report: _SourceReport
) -> None:
    gates = 0
    for group in source_report["groups"]:
        case = intent_catalog.case(group["id"])
        steps = {step.id: step for step in case.steps}
        for note in group["execution_notes"]:
            trigger_row = note["required_reply_source_row"]
            if trigger_row is None:
                continue
            gates += 1
            step = steps[note["step_id"]]
            assert step.after in {"reply_started", "reply_finished"}
            assert step.reply_from_row == trigger_row
            assert trigger_row < step.source_row
            assert trigger_row in case.source_rows

    assert gates == 6


def test_runnable_inputs_do_not_contain_expectation_labels_or_stage_directions(
    intent_catalog: MockTestCatalog,
) -> None:
    forbidden = _LABELS | {
        "expected_intent",
        "金标准",
        "预期：",
        "预期:",
        "输出对象：",
        "输出对象:",
        "（主播",
        "（弹幕",
        "（助手",
        "（静默",
    }
    for test_set in intent_catalog.sets:
        for case in test_set.cases:
            texts = list(case.context)
            for step in case.steps:
                if step.kind == "voice":
                    texts.append(step.text)
                    assert step.text.strip(), (case.id, step.id)
                if step.event is not None:
                    texts.append(step.event.text)
                texts.extend(item.event.text for item in step.events_during)
            for text in texts:
                assert not any(token in text for token in forbidden), (case.id, text)


def test_missing_intent_catalog_names_the_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"测试集读不到：.*simple\.json"):
        load_test_catalog(tmp_path, intent=True)
