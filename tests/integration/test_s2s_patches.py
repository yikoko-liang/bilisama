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


def _self_check_under_drift(patch: str, drift: str) -> dict[str, object]:
    """Mutate the freshly imported upstream module, then apply one patch.

    Simulates the upstream release that renames or reshapes something. Reports
    whether the self-check caught it, which is the whole point of the self-check:
    the alternative is a patch that reports success and fails on the first turn of
    a live stream.
    """
    return _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import PatchError, apply_patches\n"
        f"{drift}\n"
        "try:\n"
        f"    apply_patches(['{patch}'])\n"
        "except PatchError as exc:\n"
        "    print(json.dumps({'raised': True, 'message': str(exc)}))\n"
        "else:\n"
        "    print(json.dumps({'raised': False, 'message': ''}))\n"
    )


def _assert_caught(out: dict[str, object], symbol: str) -> None:
    assert out["raised"] is True, f"drift in {symbol} slipped past the self-check"
    message = out["message"]
    assert isinstance(message, str) and symbol in message, f"the error does not name {symbol}"


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


# ------------------------------------------------------------ 自检覆盖面
#
# Every symbol, field name and signature the patches touch gets its own drift
# injection here. A gap in the self-check is worse than an ordinary test gap: the
# patch reports success at startup and the failure lands on the first turn of a
# live stream, which is exactly what the self-check exists to prevent.


@pytest.mark.parametrize("symbol", ["RealtimeService", "ConnState"])
def test_self_check_catches_missing_module_global(symbol: str) -> None:
    """A class disappearing from the service module is caught, not dereferenced.

    Without the check, `hasattr(svc.RealtimeService, ...)` raises AttributeError,
    and __main__.py catches only PatchError and ImportError — the streamer gets a
    traceback instead of the exit code 3 and the zero-patch-mode hint.
    """
    out = _self_check_under_drift(
        "text_modality",
        "from speech_to_speech.api.openai_realtime import service as svc\n" f"del svc.{symbol}\n",
    )
    _assert_caught(out, symbol)


def test_self_check_catches_missing_state_accessor() -> None:
    """patched_handler calls self._state(conn_id); losing it must fail at startup."""
    out = _self_check_under_drift(
        "text_modality",
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "del svc.RealtimeService._state\n",
    )
    _assert_caught(out, "_state")


def test_self_check_catches_renamed_conn_state_field() -> None:
    """patched_handler assigns state.current_response_params.

    ConnState is a pydantic model, so an unknown attribute raises — but only on the
    first VAD turn, long after the patch reported success.
    """
    out = _self_check_under_drift(
        "text_modality",
        "from pydantic import BaseModel\n"
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "class Renamed(BaseModel):\n"
        "    response_params: str | None = None\n"
        "svc.ConnState = Renamed\n",
    )
    _assert_caught(out, "current_response_params")


def test_self_check_catches_renamed_output_modalities() -> None:
    """A renamed output_modalities produces no error anywhere.

    RealtimeResponseCreateParams is extra='allow', so our kwarg lands in an extra
    key and response_wants_audio() reads the missing field as audio. The stream
    silently goes back to upstream's TTS with the shim reporting success.
    """
    out = _self_check_under_drift(
        "text_modality",
        "from pydantic import BaseModel, ConfigDict\n"
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "class RenamedParams(BaseModel):\n"
        "    model_config = ConfigDict(extra='allow')\n"
        "    modalities: list[str] | None = None\n"
        "svc.RealtimeResponseCreateParams = RenamedParams\n",
    )
    _assert_caught(out, "output_modalities")


def test_self_check_catches_renamed_response_field() -> None:
    """A renamed response field is the other silent one.

    pydantic drops the unknown kwarg, the field upstream reads stays None, and
    every implicit turn produces audio without a single exception.
    """
    out = _self_check_under_drift(
        "text_modality",
        "from pydantic import BaseModel\n"
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "class RenamedRequest(BaseModel):\n"
        "    response_params: str | None = None\n"
        "svc.GenerateResponseRequest = RenamedRequest\n",
    )
    _assert_caught(out, "response")


def test_self_check_catches_handler_signature_change() -> None:
    """The name surviving is not enough: patched_handler takes (self, conn_id, event).

    Upstream dispatches it positionally, so an extra required parameter is a
    TypeError on the first VAD turn.
    """
    out = _self_check_under_drift(
        "text_modality",
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "def wider(self, conn_id, event, turn_id):\n"
        "    return []\n"
        "svc.RealtimeService._on_audio_input_completed = wider\n",
    )
    _assert_caught(out, "_on_audio_input_completed")


def test_self_check_catches_new_builder_keyword() -> None:
    """Patch B replaces the prompt builders with identity(prompt, tool_section).

    A builder that grows a third parameter would make the replacement raise
    TypeError on the first generated reply.
    """
    out = _self_check_under_drift(
        "raw_instructions",
        "from speech_to_speech.LLM import base_openai_compatible_language_model as mod\n"
        "def wider(session_prompt, *, tool_section='', style_section=''):\n"
        "    return session_prompt\n"
        "mod.build_voice_system_prompt = wider\n",
    )
    _assert_caught(out, "build_voice_system_prompt")


def test_self_check_leaves_upstream_untouched_when_it_fails() -> None:
    """A failed self-check must not have patched half of anything.

    Patch A mutates two module attributes. If it bailed between them, zero-patch
    mode would no longer be a real fallback.
    """
    out = _run(
        "import json\n"
        "from bilisama_s2s_shim.patches import PatchError, apply_patches\n"
        "from speech_to_speech.api.openai_realtime import service as svc\n"
        "before_cls = svc.GenerateResponseRequest\n"
        "before_handler = svc.RealtimeService._on_audio_input_completed\n"
        "del svc.RealtimeService._state\n"
        "try:\n"
        "    apply_patches(['text_modality'])\n"
        "except PatchError:\n"
        "    pass\n"
        "print(json.dumps({'cls': svc.GenerateResponseRequest is before_cls,"
        " 'handler': svc.RealtimeService._on_audio_input_completed is before_handler}))\n"
    )
    assert out["cls"] is True, "GenerateResponseRequest was replaced before the check failed"
    assert out["handler"] is True, "_on_audio_input_completed was replaced before the check failed"
