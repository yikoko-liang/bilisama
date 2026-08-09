"""Runtime verification of the speech-to-speech patches.

Needs speech-to-speech installed in its own venv, so these are marked integration
and skipped by default::

    scripts/smoke_provider_b.sh install
    .venv/bin/python -m pytest tests/integration -m integration

Think of this file as a drift detector. If upstream changes shape, it goes red here
rather than failing silently halfway through a live stream.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VENV = Path(
    os.environ.get("BILISAMA_S2S_VENV", str(Path.home() / ".local/share/bilisama/engines/s2s"))
)
SHIM = Path(__file__).resolve().parents[2] / "tools" / "s2s_shim"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (VENV / "bin" / "python").exists(), reason="speech-to-speech is not installed"
    ),
]


def _run(snippet: str) -> dict[str, object]:
    """Run a snippet inside the s2s venv and parse the last JSON line it prints."""
    proc = subprocess.run(
        [str(VENV / "bin" / "python"), "-c", snippet],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail(f"subprocess failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    last = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1]
    parsed: dict[str, object] = json.loads(last)
    return parsed


def test_patches_apply_cleanly() -> None:
    """Both patches apply and pass their self-checks."""
    out = _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import apply_patches\n"
        "rs = apply_patches(['text_modality', 'raw_instructions'])\n"
        "print(json.dumps({'names': [r.name for r in rs], 'ok': all(r.applied for r in rs)}))\n"
    )
    assert out["ok"] is True
    assert out["names"] == ["text_modality", "raw_instructions"]


def test_patch_a_makes_implicit_turn_text_only() -> None:
    """The implicit VAD-driven turn produces text.

    Without this patch, setting output_modalities at session level has no effect:
    upstream builds the request without a response field and everything downstream
    reads that as "wants audio".
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
    assert out["implicit"] == ["text"], "the implicit turn is still producing audio"
    # setdefault semantics: an explicit request keeps its own parameters.
    assert out["explicit"] == ["audio"], "the patch overrode an explicit caller parameter"


def test_patch_b_stops_the_injected_tail() -> None:
    """The persona prompt goes out verbatim, with no Voice Rules appended."""
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
    assert out["out"] == "我是米娅。", "the persona prompt was rewritten"
    # This is why the patch exists: that constraint is hard, and a VTuber persona
    # uses action text constantly.
    assert out["bans_action_text"] is True
    assert isinstance(out["tail_len"], int) and out["tail_len"] > 500


def test_zero_patch_mode_touches_nothing() -> None:
    """Zero-patch mode leaves upstream completely alone.

    This is the fallback if a patch ever stops applying, so it had better really
    change nothing.
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
    """A misspelled patch name is an error, not a silent skip."""
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
    """Running under the wrong interpreter explains itself instead of dumping a
    traceback."""
    proc = subprocess.run(
        [sys.executable, "-m", "bilisama_s2s_shim", "serve", "x.json"],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 4
    assert "是不是没在它自己的 venv 里跑" in proc.stderr
