"""补丁的运行时验证。

需要 speech-to-speech 装在它自己的 venv 里，所以标了 integration，默认不跑。
跑法：

    scripts/smoke_provider_b.sh install
    BILISAMA_S2S_VENV=~/.local/share/bilisama/engines/s2s \\
      .venv/bin/python -m pytest tests/integration -m integration

这批测试是「上游漂移探测器」：上游改了结构，这里先红，而不是等到直播中途
静默失效。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

VENV = Path(
    os.environ.get("BILISAMA_S2S_VENV", str(Path.home() / ".local/share/bilisama/engines/s2s"))
)
SHIM = Path(__file__).resolve().parents[2] / "tools" / "s2s_shim"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (VENV / "bin" / "python").exists(), reason="s2s 还没装"),
]


def _run(snippet: str) -> dict[str, object]:
    """在 s2s 的 venv 里跑一段代码，拿回它打印的最后一行 JSON。"""
    proc = subprocess.run(
        [str(VENV / "bin" / "python"), "-c", snippet],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"子进程失败（{proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
    last = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(last)


def test_patches_apply_cleanly() -> None:
    """两个补丁都能打上，且自检通过。"""
    out = _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import apply_patches\n"
        "rs = apply_patches(['text_modality', 'raw_instructions'])\n"
        "print(json.dumps({'names': [r.name for r in rs], 'ok': all(r.applied for r in rs)}))\n"
    )
    assert out["ok"] is True
    assert out["names"] == ["text_modality", "raw_instructions"]


def test_patch_a_makes_implicit_turn_text_only() -> None:
    """服务端 VAD 发起的隐式轮次会走纯文本。

    不打这个补丁的话，会话级设了 output_modalities 也没用,上游构造
    GenerateResponseRequest 时不带 response，下游就当成要音频。
    """
    out = _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import apply_patches\n"
        "apply_patches(['text_modality'])\n"
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig\n"
        "rc = RuntimeConfig()\n"
        "implicit = svc.GenerateResponseRequest(runtime_config=rc)\n"
        "explicit = svc.GenerateResponseRequest(runtime_config=rc,"
        " response=svc.RealtimeResponseCreateParams(output_modalities=['audio']))\n"
        "print(json.dumps({'implicit': implicit.response.output_modalities,"
        " 'explicit': explicit.response.output_modalities}))\n"
    )
    assert out["implicit"] == ["text"], "隐式轮次没有改成纯文本"
    # setdefault 语义：显式带了参数的不能被覆盖
    assert out["explicit"] == ["audio"], "补丁覆盖了调用方显式指定的参数"


def test_patch_b_stops_the_injected_tail() -> None:
    """人设按原样下发，不再被追加 Voice Rules。"""
    out = _run(
        "import json\n"
        "from speech_to_speech.LLM import voice_prompt\n"
        "tail = voice_prompt.VOICE_SYSTEM_PROMPT_TAIL\n"
        "from bilisama_s2s_shim.patches import apply_patches\n"
        "apply_patches(['raw_instructions'])\n"
        "from speech_to_speech.LLM import base_openai_compatible_language_model as m\n"
        "persona = '我是米娅。'\n"
        "print(json.dumps({'out': m.build_voice_system_prompt(persona),"
        " 'tail_len': len(tail), 'bans_action_text': '*laughs*' in tail}))\n"
    )
    assert out["out"] == "我是米娅。", "人设被改写了"
    # 这就是打这个补丁的理由：那条硬约束跟 VTuber 人设正面冲突
    assert out["bans_action_text"] is True
    assert isinstance(out["tail_len"], int) and out["tail_len"] > 500


def test_zero_patch_mode_touches_nothing() -> None:
    """零补丁模式：用它自带的 TTS 和提示词尾巴，一个字节都不碰。

    这是补丁出问题时的退路，要保证它真的什么都没改。
    """
    out = _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import apply_patches\n"
        "rs = apply_patches([])\n"
        "from speech_to_speech.LLM import base_openai_compatible_language_model as m\n"
        "print(json.dumps({'applied': [r.applied for r in rs],"
        " 'tail_still_there': len(m.build_voice_system_prompt('嗨')) > 100}))\n"
    )
    assert out["applied"] == [False]
    assert out["tail_still_there"] is True


def test_unknown_patch_name_fails_loudly() -> None:
    """拼错补丁名要报错，不能静默跳过。"""
    proc = subprocess.run(
        [
            str(VENV / "bin" / "python"),
            "-c",
            "from bilisama_s2s_shim.patches import apply_patches;"
            " apply_patches(['no_such_patch'])",
        ],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "不认识的补丁" in proc.stderr


def test_shim_reports_import_failure_clearly() -> None:
    """在错误的解释器里跑要给人话，不甩 traceback。"""
    proc = subprocess.run(
        [sys.executable, "-m", "bilisama_s2s_shim", "serve", "x.json"],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 4
    assert "是不是没在它自己的 venv 里跑" in proc.stderr
