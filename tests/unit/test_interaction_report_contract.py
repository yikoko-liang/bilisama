"""State reports are conditional changes, not a per-reply heartbeat."""

from __future__ import annotations

from bilisama.director.interaction_state import REPORT_RULES, report_tool_spec


def test_no_state_change_does_not_require_an_empty_report() -> None:
    assert "没有变化也报告" not in REPORT_RULES
    assert "没有后台状态变化就不调用" in REPORT_RULES
    assert "普通问答、自言自语先听、面向观众讲解时的沉默" in REPORT_RULES
    assert "不要为了凑齐每轮输出而发送空events和全keep" in REPORT_RULES


def test_transitions_still_require_a_separate_same_response_report() -> None:
    for contract in (
        "确切的事件状态变化",
        "首次进入持续静默",
        "从持续静默恢复",
        "观点征集的开始、取消或完成",
        "主播明确委托总结弹幕时启动或取消一次总结",
        "已经处于静默且本轮继续先听，不重复enter",
        "原本没有静默，不因普通回答报告release",
        "再在同一响应中调用 report_interaction",
    ):
        assert contract in REPORT_RULES


def test_tool_description_matches_conditional_call_policy_without_schema_change() -> None:
    spec = report_tool_spec()
    assert "仅在需要改变后台状态时调用" in spec.description
    assert "没有变化不调用" in spec.description
    assert "其他回合用空events和keep" not in spec.description
    assert set(spec.parameters["required"]) == {"events", "silence", "discussion"}
    assert spec.parameters["properties"]["silence"]["enum"] == ["keep", "enter", "release"]
    assert spec.parameters["properties"]["danmaku_summary"]["properties"]["action"]["enum"] == [
        "keep",
        "start",
        "cancel",
    ]


def test_danmaku_summary_is_an_independent_report_dimension() -> None:
    from bilisama.director.interaction_state import parse_report

    report = parse_report(
        '{"events":[],"silence":"keep","discussion":{"action":"keep","topic":""},'
        '"danmaku_summary":{"action":"start"}}'
    )
    assert report.discussion.action == "keep"
    assert report.danmaku_summary.action == "start"
