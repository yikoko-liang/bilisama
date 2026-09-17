"""The product entry must wire the same report and lifecycle paths we test."""

import ast
from pathlib import Path

from bilisama.clock import FakeClock
from bilisama.director.interaction_state import REPORT_RULES, InteractionState
from tests.unit.conftest import build_assembly_kit
from tests.unit.test_dev_talk_uplink import _calls, _run_director


def keywords(name: str) -> dict[str | None, str]:
    return {kw.arg: ast.unparse(kw.value) for kw in _calls(_run_director(), name)[0].keywords}


def test_production_wires_one_shared_state_to_assembly_and_scheduler() -> None:
    assert keywords("Assembly")["interaction_state"] == "interaction_state"
    assert keywords("Scheduler")["interaction_state"] == "interaction_state"
    assert keywords("Scheduler")["on_interrupted"] == "on_interrupted"
    source = ast.unparse(_run_director())
    assert "speech.set_reports(interaction_reports)" in source
    assert "report_tool_spec()" in source
    assert "provider is ProviderName.DASHSCOPE" in source
    assert "qwen-audio-3.0-realtime-flash" in source


def test_production_keeps_skip_generation_and_delivers_proactive_results() -> None:
    source = ast.unparse(_run_director())
    assert "preserve_generation=interaction_state is not None" in source
    assert "proactive.note_verdict(verdict)" in source
    assert keywords("ProactiveTopicLoop")["revoke"] == "scheduler.revoke"
    assert (
        keywords("ProactiveTopicLoop")["collection_window_s"]
        == "float(settings.interaction.proactive.collection_window_s)"
    )
    assert "interaction_reports.status" in source


def test_production_wires_the_ledger_the_pool_and_the_answered_lines() -> None:
    """The three legs of "no repeated proactive topics" (2026-09-16): the
    scheduler hands her spoken words with the intent to the ledger, the pool
    files under config/prompts/topics/ are the quiet-room floor, and the
    verdict sink reaches assembly.note_verdict, which settles danmaku lines
    as answered."""
    scheduler = keywords("Scheduler")
    assert scheduler["on_spoken"] == "lambda intent, text: proactive.note_spoken(intent, text)"
    loop = keywords("ProactiveTopicLoop")
    assert "TopicPool.load(config_path.parent / 'prompts' / 'topics'" in loop["topic_pool"]
    assert "settings.interaction.proactive.topic_pool.split(',')" in loop["topic_pool"]
    assert loop["stream_intro"] == "lambda: settings.room.stream_intro"
    source = ast.unparse(_run_director())
    assert "assembly.note_verdict(verdict)" in source
    assert (
        Path(__file__).resolve().parents[2] / "config" / "prompts" / "topics" / "aigc.md"
    ).is_file()
    assert (
        Path(__file__).resolve().parents[2] / "config" / "prompts" / "topics" / "coding.md"
    ).is_file()


def test_reporting_instructions_follow_voice_contract_without_duplicating_it(
    tmp_path: Path,
) -> None:
    kit = build_assembly_kit(
        tmp_path, interaction_state=InteractionState(FakeClock()), voice_rules="语音正文的完整规则"
    )
    context = kit.assembly.build_context()
    assert context.index("语音正文的完整规则") < context.index(REPORT_RULES)
    assert context.count(REPORT_RULES) == 1
    assert REPORT_RULES in kit.assembly.build_public_context()
    kit.store.close()


def test_production_starts_the_summary_from_the_summary_marker() -> None:
    """The delegation rides the head marker, not the function report: the
    gate's skip callback is where the summary starts, with the speech edge
    the gate recorded as its boundary."""
    source = ast.unparse(_run_director())
    assert "ruling.category is SceneCategory.SUMMARY" in source
    assert "assembly.request_danmaku_summary(voice_started_at=skip.speech_started_at)" in source
