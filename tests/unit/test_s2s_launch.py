"""Rendering the speech-to-speech launch config, and reconciling it upstream.

test_turn_fields_match_upstream is the gate that keeps two promises at once: every
turn-detection parameter is passed through, and a misspelled key cannot slip past
upstream's allow_extra_keys parsing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bilisama.bootstrap import s2s_launch
from bilisama.config import S2SConfig, TurnConfig

# The upstream checkout is normally a sibling of this repo. The env var wins so CI
# can point elsewhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
S2S_ROOT = Path(os.environ.get("BILISAMA_S2S_ROOT", _REPO_ROOT.parent / "speech-to-speech"))


def _cfg(**kw: object) -> S2SConfig:
    return S2SConfig(llm_model="our-s2t-v1", **kw)


def test_render_skips_stt_and_pins_chat_completions() -> None:
    payload = s2s_launch.render(_cfg())
    assert payload["stt"] == "none"
    assert payload["llm_backend"] == "chat-completions"
    assert payload["num_pipelines"] == 1
    # Burns CPU for nothing once STT is skipped.
    assert payload["enable_live_transcription"] is False


def test_render_never_sets_mac_optimal_settings() -> None:
    # It quietly moves a dozen other defaults, which is one more layer to debug.
    assert "mac_optimal_settings" not in s2s_launch.render(_cfg())


def test_render_drops_infinite_max_speech() -> None:
    # inf is not valid JSON, and upstream reads a missing value as "no limit".
    assert "max_speech_ms" not in s2s_launch.render(_cfg())
    payload = s2s_launch.render(_cfg(turn=TurnConfig(max_speech_ms=30_000)))
    assert payload["max_speech_ms"] == 30_000


def test_render_carries_every_turn_field() -> None:
    payload = s2s_launch.render(_cfg())
    for name in TurnConfig.model_fields:
        if name == "max_speech_ms":
            continue  # defaults to inf, covered separately above
        assert name in payload, f"turn-detection parameter {name} was not passed through"


def test_port_parsed_from_endpoint() -> None:
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1:9999/v1/realtime"))["port"] == 9999
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1/v1/realtime"))["port"] == 8765


def test_write_rejects_unknown_keys(tmp_path: Path) -> None:
    class Bogus(S2SConfig):
        pass

    original = s2s_launch.render

    def patched(cfg: S2SConfig) -> dict[str, object]:
        payload = original(cfg)
        payload["totally_made_up_flag"] = True
        return payload

    s2s_launch.render = patched
    try:
        with pytest.raises(s2s_launch.S2SConfigError, match="上游不认识"):
            s2s_launch.write(_cfg(), tmp_path / "c.json", s2s_root=S2S_ROOT)
    finally:
        s2s_launch.render = original


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="no local speech-to-speech checkout")
def test_turn_fields_match_upstream() -> None:
    """Our turn-detection field names must match upstream's word for word.

    Goes red if upstream renames something, and equally if we misspell something.
    """
    known = s2s_launch.upstream_field_names(S2S_ROOT)
    assert known, "scanned no field names from upstream, so this check proves nothing"

    ours = set(TurnConfig.model_fields)
    unknown = ours - known
    assert not unknown, f"upstream does not know these and would swallow them: {sorted(unknown)}"

    # And the other direction: anything upstream added that we have not picked up.
    vad_file = S2S_ROOT / "src/speech_to_speech/arguments_classes/vad_arguments.py"
    upstream_vad = set(s2s_launch._FIELD_RE.findall(vad_file.read_text(encoding="utf-8")))
    # These two are overridden by module_arguments, so we deliberately skip them.
    upstream_vad -= {"enable_realtime_transcription", "realtime_processing_pause"}
    missing = upstream_vad - ours
    assert (
        not missing
    ), f"upstream added turn-detection parameters we have not adopted: {sorted(missing)}"


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="no local speech-to-speech checkout")
def test_render_checked_reports_clean() -> None:
    result = s2s_launch.render_checked(_cfg(), S2S_ROOT)
    assert result.unknown_keys == ()
    assert result.missing_turn_fields == ()
