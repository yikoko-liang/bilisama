"""Public prompt snapshots must preserve source wording without local state."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bilisama.config.enums import Chattiness
from bilisama.config.schema import PersonaConfig
from bilisama.director.interaction_state import REPORT_RULES, report_tool_spec
from bilisama.persona.loader import template_variables
from bilisama.persona.prompt import LIVE_RULES

_ROOT = Path(__file__).resolve().parents[2]
_EXPORTER = _ROOT / "tools" / "export_interaction_prompts.py"


def _run(output: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_EXPORTER), "--output", str(output), *args],
        cwd=_ROOT,
        env={**os.environ, "XDG_DATA_HOME": str(output.parent / "private")},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_export_contains_complete_shipped_templates_and_report(tmp_path: Path) -> None:
    output = tmp_path / "prompts.md"
    result = _run(output)
    assert result.returncode == 0, result.stderr
    snapshot = output.read_text(encoding="utf-8")
    for relative in (
        "config/personas/tofu/identity.md",
        "config/personas/tofu/personality.md",
        "config/personas/live/voice_responses.md",
        "config/personas/live/voice_addressing.md",
        "config/personas/live/event_responses.md",
        "config/prompts/proactive.md",
    ):
        assert (_ROOT / relative).read_text(encoding="utf-8") in snapshot
    assert LIVE_RULES in snapshot
    assert REPORT_RULES in snapshot
    assert report_tool_spec().name in snapshot
    for label in ("SC", "普通礼物", "中额礼物", "高额礼物", "舰长", "提督", "总督", "观点征集"):
        assert label in snapshot


def test_export_keeps_dynamic_length_and_never_loads_private_persona(tmp_path: Path) -> None:
    private = tmp_path / "private" / "bilisama" / "personas" / "tofu"
    private.mkdir(parents=True)
    marker = "不得公开的本机人设内容"
    (private / "identity.md").write_text(marker, encoding="utf-8")
    output = tmp_path / "prompts.md"
    result = _run(output)
    assert result.returncode == 0, result.stderr
    snapshot = output.read_text(encoding="utf-8")
    assert marker not in snapshot
    assert "{{replyLength}}" in snapshot
    assert "{{username}}" in snapshot
    assert "{{candidate}}" in snapshot
    for length in Chattiness:
        wording = template_variables(PersonaConfig(), reply_length=length)["replyLength"]
        assert wording in snapshot
    assert "max_tokens" not in snapshot


def test_export_is_deterministic_and_check_does_not_write(tmp_path: Path) -> None:
    output = tmp_path / "prompts.md"
    assert _run(output).returncode == 0
    first = output.read_bytes()
    assert _run(output, "--check").returncode == 0
    assert _run(output).returncode == 0
    assert output.read_bytes() == first
    output.write_text("旧快照", encoding="utf-8")
    checked = _run(output, "--check")
    assert checked.returncode == 1
    assert output.read_text(encoding="utf-8") == "旧快照"


def test_check_missing_snapshot_has_clear_failure(tmp_path: Path) -> None:
    output = tmp_path / "missing.md"
    result = _run(output, "--check")
    assert result.returncode == 1
    assert "快照" in result.stdout
    assert not output.exists()


def test_export_gift_combo_uses_member_facts_and_marks_incomplete_prefix(tmp_path: Path) -> None:
    output = tmp_path / "prompts.md"
    result = _run(output)
    assert result.returncode == 0, result.stderr
    snapshot = output.read_text(encoding="utf-8")
    section = snapshot.split("### 礼物连击聚合：原始成员事实", 1)[1].split("## 6.", 1)[0]
    assert "本次只评估候选记录 {{event_ref_1}}、{{event_ref_2}}" in section
    assert "[记录 {{event_ref_1}}" in section
    assert "[记录 {{event_ref_2}}" in section
    assert "聚合不表示又发生一次送礼" in snapshot
    assert "[合计说明] 压缩前缀合计，逐笔明细已不完整" in section
    assert "不能确认某一笔已答谢就撤销整个合计" in section
    assert "public-prompt:" not in section
    assert "gift-combo:" not in section
