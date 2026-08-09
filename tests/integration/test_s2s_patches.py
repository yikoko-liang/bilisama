"""Runtime verification of the speech-to-speech patches.

Needs speech-to-speech installed in its own venv, so these are marked integration
and deselected by pyproject's addopts::

    scripts/smoke_provider_b.sh install
    .venv/bin/python -m pytest tests/integration -m integration

scripts/gate.sh runs them once that venv exists, and reports the skip in so many
words when it does not — for a while it did neither, and this whole file was green
only in the sense that nobody ran it. tests/unit/test_gate.py holds that line.

This file is a drift detector. If upstream changes shape, it goes red here rather
than failing silently halfway through a live stream.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
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
    """Running under the wrong interpreter explains itself.

    The main venv cannot import speech_to_speech, and the operator needs to be told
    that rather than handed an ImportError traceback.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "bilisama_s2s_shim", "serve", "x.json"],
        env={**os.environ, "PYTHONPATH": str(SHIM)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 4
    assert "是不是没在它自己的 venv 里跑" in proc.stderr


# ------------------------------------------------------------ Self-check coverage
#
# Every symbol, field name and signature the patches touch gets a row in _DRIFTS.
# A gap here is worse than an ordinary test gap: an unchecked dependency lets the
# patch report success at startup and fail on the first turn of a live stream.
#
# One table, one subprocess per row, two assertions on that run: the drift is
# caught, and the patch that rejected it changed nothing. Those used to be
# separate tests, and the atomicity one injected a drift that tripped a check
# near the top of the function — so every check added later was tested for the
# first property only, and moving a write above them stayed green. A new check
# now earns both the moment its row lands here.


@dataclass(frozen=True)
class Drift:
    """One reshaping upstream could ship, and what it would cost if it slipped through.

    Attributes:
        id: pytest parametrize id.
        patch: patch to apply once the drift is in place.
        symbol: substring the PatchError has to contain, so the operator is told
            which symbol moved rather than just that something did.
        why: what breaks, and how quietly, if this check ever goes missing.
        source: python that reshapes the freshly imported upstream module. It
            stands in for the upstream release that renames or rewraps
            something, which is otherwise only reproducible by pinning an old
            version.
    """

    id: str
    patch: str
    symbol: str
    why: str
    source: str


_IMPORT_SVC = "from speech_to_speech.api.openai_realtime import service as svc\n"
_IMPORT_MOD = "from speech_to_speech.LLM import base_openai_compatible_language_model as mod\n"

_DRIFTS: tuple[Drift, ...] = (
    Drift(
        id="missing_GenerateResponseRequest",
        patch="text_modality",
        symbol="GenerateResponseRequest",
        why="patched_request wraps this class, so its absence is an AttributeError "
        "raised from inside the patch. __main__.py catches PatchError and ImportError "
        "only: the streamer would get a traceback instead of exit code 3 and the "
        "zero-patch-mode hint.",
        source=_IMPORT_SVC + "del svc.GenerateResponseRequest\n",
    ),
    Drift(
        id="missing_RealtimeResponseCreateParams",
        patch="text_modality",
        symbol="RealtimeResponseCreateParams",
        why="text_only_params() builds one of these on every implicit turn. Same "
        "AttributeError-instead-of-PatchError failure as the class above.",
        source=_IMPORT_SVC + "del svc.RealtimeResponseCreateParams\n",
    ),
    Drift(
        id="missing_RealtimeService",
        patch="text_modality",
        symbol="RealtimeService",
        why="Without the check, hasattr(svc.RealtimeService, ...) two lines down "
        "raises AttributeError rather than PatchError, and the operator loses the "
        "message naming what moved.",
        source=_IMPORT_SVC + "del svc.RealtimeService\n",
    ),
    Drift(
        id="missing_ConnState",
        patch="text_modality",
        symbol="ConnState",
        why="The field check dereferences svc.ConnState.model_fields. Same "
        "AttributeError-instead-of-PatchError failure.",
        source=_IMPORT_SVC + "del svc.ConnState\n",
    ),
    Drift(
        id="missing_on_audio_input_completed",
        patch="text_modality",
        symbol="_on_audio_input_completed",
        why="This is the handler the patch wraps. If it were gone, _commit would "
        "happily create the attribute, and upstream would dispatch to whatever it "
        "renamed the real one to — our text-only params never set, silently back on "
        "the audio path.",
        source=_IMPORT_SVC + "del svc.RealtimeService._on_audio_input_completed\n",
    ),
    Drift(
        id="missing_state_accessor",
        patch="text_modality",
        symbol="_state",
        why="patched_handler calls self._state(conn_id) on every VAD turn. Losing it "
        "must fail at startup, not on the first thing the streamer says.",
        source=_IMPORT_SVC + "del svc.RealtimeService._state\n",
    ),
    Drift(
        id="renamed_current_response_params",
        patch="text_modality",
        symbol="current_response_params",
        why="patched_handler assigns state.current_response_params. ConnState is a "
        "pydantic model, so an unknown attribute raises — but only on the first VAD "
        "turn, long after the patch reported success.",
        source="from pydantic import BaseModel\n" + _IMPORT_SVC + "class Renamed(BaseModel):\n"
        "    response_params: str | None = None\n"
        "svc.ConnState = Renamed\n",
    ),
    Drift(
        id="renamed_output_modalities",
        patch="text_modality",
        symbol="output_modalities",
        why="A rename here produces no error anywhere. RealtimeResponseCreateParams "
        "is extra='allow', so our kwarg lands in an extra key and "
        "response_wants_audio() reads the missing field as audio (upstream "
        "utils/utils.py:20-23). The stream goes back to upstream's TTS with the shim "
        "reporting success.",
        source="from pydantic import BaseModel, ConfigDict\n"
        + _IMPORT_SVC
        + "class RenamedParams(BaseModel):\n"
        "    model_config = ConfigDict(extra='allow')\n"
        "    modalities: list[str] | None = None\n"
        "svc.RealtimeResponseCreateParams = RenamedParams\n",
    ),
    Drift(
        id="renamed_response_field",
        patch="text_modality",
        symbol="response",
        why="The other silent one. pydantic drops the unknown kwarg, the field "
        "upstream reads stays None, and every implicit turn produces audio without a "
        "single exception.",
        source="from pydantic import BaseModel\n"
        + _IMPORT_SVC
        + "class RenamedRequest(BaseModel):\n"
        "    response_params: str | None = None\n"
        "svc.GenerateResponseRequest = RenamedRequest\n",
    ),
    Drift(
        id="widened_handler_signature",
        patch="text_modality",
        symbol="_on_audio_input_completed",
        why="The name surviving is not enough: patched_handler takes (self, conn_id, "
        "event) and upstream dispatches it positionally (service.py:398), so an extra "
        "required parameter is a TypeError on the first VAD turn.",
        source=_IMPORT_SVC + "def wider(self, conn_id, event, turn_id):\n"
        "    return []\n"
        "svc.RealtimeService._on_audio_input_completed = wider\n",
    ),
    Drift(
        id="missing_build_text_system_prompt",
        patch="raw_instructions",
        symbol="build_text_system_prompt",
        why="Patch B checks two names and writes two names. Check only the first and "
        "_commit creates the second as a brand new attribute — our identity installed "
        "under a dead name while the renamed real builder keeps appending the tail to "
        "text replies.",
        source=_IMPORT_MOD + "del mod.build_text_system_prompt\n",
    ),
    Drift(
        id="widened_builder_signature",
        patch="raw_instructions",
        symbol="build_voice_system_prompt",
        why="Patch B replaces the builders with identity(prompt, tool_section). A "
        "builder that grows a third parameter would make the replacement raise "
        "TypeError on the first generated reply.",
        source=_IMPORT_MOD + "def wider(session_prompt, *, tool_section='', style_section=''):\n"
        "    return session_prompt\n"
        "mod.build_voice_system_prompt = wider\n",
    ),
)

# Everything a patch is allowed to write, as a dotted path from the module alias
# its import prelude binds. The probe watches exactly these: nothing else in the
# process can tell a rejected patch from one that never ran.
_WRITES: dict[str, tuple[tuple[str, ...], ...]] = {
    "text_modality": (
        ("svc", "GenerateResponseRequest"),
        ("svc", "RealtimeService", "_on_audio_input_completed"),
    ),
    "raw_instructions": (
        ("mod", "build_voice_system_prompt"),
        ("mod", "build_text_system_prompt"),
    ),
}

_IMPORTS: dict[str, str] = {
    "text_modality": _IMPORT_SVC,
    "raw_instructions": _IMPORT_MOD,
}

# Identity, not equality: a replacement can compare equal to what it replaced
# (two `identity` functions with the same code object would), and it is the swap
# we care about, not the shape of the thing swapped in.
_PROBE = """\
import json

{imports}{drift}
from bilisama_s2s_shim.patches import PatchError, apply_patches

MISSING = object()


def peek(root, *names):
    obj = root
    for name in names:
        obj = getattr(obj, name, MISSING)
        if obj is MISSING:
            return MISSING
    return obj


watched = [{watched}]
before = [peek(*w) for w in watched]
try:
    apply_patches([{patch!r}])
except PatchError as exc:
    outcome = {{"raised": True, "message": str(exc)}}
else:
    outcome = {{"raised": False, "message": ""}}
after = [peek(*w) for w in watched]
outcome["touched"] = [
    ".".join(names) for (_root, *names), was, now in zip(watched, before, after) if was is not now
]
print(json.dumps(outcome))
"""


def _probe(patch: str, drift: str = "") -> dict[str, object]:
    """Apply one patch, optionally under an injected drift, and see what it wrote.

    Args:
        patch: name of the patch to apply.
        drift: source that reshapes upstream first. Empty means the clean path.

    Returns:
        `raised` and `message` from the attempt, plus `touched`: the dotted names
        of the upstream attributes whose identity changed. Empty `touched` after a
        failure is the atomicity property — the process is exactly as the patch
        found it, so zero-patch mode is still a real fallback.
    """
    watched = ", ".join(
        "(" + ", ".join([path[0], *(repr(name) for name in path[1:])]) + ")"
        for path in _WRITES[patch]
    )
    return _run(_PROBE.format(imports=_IMPORTS[patch], drift=drift, watched=watched, patch=patch))


@pytest.mark.parametrize("drift", _DRIFTS, ids=[d.id for d in _DRIFTS])
def test_self_check_catches_drift_and_writes_nothing(drift: Drift) -> None:
    """A drifted upstream is refused, by name, without a byte of it being touched.

    The second half is what keeps zero-patch mode real: a patch that raises after
    swapping one attribute leaves a process that is neither patched nor pristine,
    and the fallback the module docstring promises stops existing.
    """
    out = _probe(drift.patch, drift.source)
    assert out["raised"] is True, f"drift slipped past the self-check. {drift.why}"
    message = out["message"]
    assert (
        isinstance(message, str) and drift.symbol in message
    ), f"the error does not name {drift.symbol}, so nobody can tell what moved: {message!r}"
    assert out["touched"] == [], (
        f"a rejected patch mutated upstream: {out['touched']}. Every check has to run "
        "before every write — see _commit in tools/s2s_shim/bilisama_s2s_shim/patches.py."
    )


@pytest.mark.parametrize("patch", sorted(_WRITES))
def test_a_clean_patch_writes_every_replacement(patch: str) -> None:
    """The other half of all-or-nothing: when the checks pass, all of it lands.

    A patch's writes are load-bearing together. Patch A's request class without
    its response params gets you text from the model under audio event names,
    which is worse than not patching; patch B's voice builder without its text
    builder strips the tail from one path only. A commit that stopped after the
    first write would be exactly the half-patched state the drift rows above
    forbid, and none of them can see it, because they never get that far.
    """
    out = _probe(patch)
    assert out["raised"] is False, f"the patch did not apply: {out['message']!r}"
    expected = [".".join(path[1:]) for path in _WRITES[patch]]
    assert out["touched"] == expected, "a successful patch left one of its writes undone"
