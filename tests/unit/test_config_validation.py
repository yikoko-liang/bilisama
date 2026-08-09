"""Cross-field validation, and what the CLI does with it.

Two promises are under test. `Settings` stays constructible, because fixtures, the
schema export and the settings UI all need a default object. And a config that
cannot start is still refused — by `loader.load` now — with the field, the message
and the fix that plan §7.6 promises, never a pydantic traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bilisama import cli
from bilisama.config import ConfigError, ProviderName, Settings, check, load

_FATAL = """
config_version = 1
[speech]
provider = "s2s"
[speech.s2s]
llm_model = ""
"""

_WARN_ONLY = """
config_version = 1
[speech.s2s]
llm_model = "our-s2t-v1"
[audio]
output_route = "direct"
echo_guard = "off"
"""

_CLEAN = """
config_version = 1
[speech.s2s]
llm_model = "our-s2t-v1"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bilisama.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _strict_json(text: str) -> Any:
    """Parse the way every parser outside Python does: Infinity and NaN are not JSON."""

    def reject(token: str) -> object:
        raise AssertionError(f"{token} is not valid JSON")

    return json.loads(text, parse_constant=reject)


def test_defaults_construct() -> None:
    """A default Settings has to exist. Fixtures, the schema export and the settings
    UI all need one, and none of them has a room id or a model name yet."""
    s = Settings()
    assert s.speech.provider is ProviderName.S2S
    assert s.avatar.expression_source == "tag"


def test_default_settings_are_not_silently_valid() -> None:
    """Moving the check out of the model must not lose the check."""
    fatal = [p.field for p in check(Settings()) if p.fatal]
    assert "speech.s2s.llm_model" in fatal


def test_load_refuses_a_fatal_config(tmp_path: Path) -> None:
    """The loader is the gate: a fatal config never reaches a caller."""
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path, _FATAL))
    assert [p.field for p in exc.value.problems] == ["speech.s2s.llm_model"]
    assert all(p.fatal and p.fix for p in exc.value.problems)


def test_load_lets_a_warning_through(tmp_path: Path) -> None:
    """Only fatal problems refuse. A warning still starts."""
    settings = load(_write(tmp_path, _WARN_ONLY))
    assert settings.audio.output_route == "direct"
    assert [p.field for p in check(settings)] == ["audio.output_route"]


def test_load_non_strict_returns_the_broken_config(tmp_path: Path) -> None:
    """The inspection commands need the object in order to say what is wrong with it."""
    settings = load(_write(tmp_path, _FATAL), strict=False)
    assert [p.field for p in check(settings) if p.fatal] == ["speech.s2s.llm_model"]


def test_validate_reports_a_fatal_problem_in_plain_language(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fatal branch of `config validate` has to be reachable, and §7.6 fixes what
    it prints: which field, what is wrong, what to do — and no pydantic."""
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _FATAL))])
    out = capsys.readouterr().out
    assert code == 1
    assert "[错误] speech.s2s.llm_model" in out
    assert "怎么办：" in out
    for jargon in ("ValidationError", "pydantic", "Traceback", "value_error"):
        assert jargon not in out


def test_validate_exits_zero_on_warnings_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins the exit-code contract CI depends on: a warning does not fail the build."""
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _WARN_ONLY))])
    out = capsys.readouterr().out
    assert code == 0
    assert "[提醒] audio.output_route" in out


def test_validate_says_so_when_nothing_is_wrong(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _CLEAN))])
    assert code == 0
    assert "配置没问题" in capsys.readouterr().out


def test_show_refuses_a_fatal_config_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every CLI path onto a fatal config stops, and none of them shows pydantic."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "show", "--config", str(_write(tmp_path, _FATAL))])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "[错误] speech.s2s.llm_model" in err
    assert "pydantic" not in err


def test_render_s2s_refuses_a_fatal_config(tmp_path: Path) -> None:
    """Guard on the property the model validator used to provide: an invalid config
    cannot produce a launch artifact."""
    out = tmp_path / "bilisama-s2s.json"
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "config",
                "render-s2s",
                "--config",
                str(_write(tmp_path, _FATAL)),
                "--out",
                str(out),
            ]
        )
    assert exc.value.code == 2
    assert not out.exists()


def test_missing_config_file_is_a_sentence_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "show", "--config", str(tmp_path / "not-here.toml")])
    assert exc.value.code == 2
    assert "找不到配置文件" in capsys.readouterr().err


def test_malformed_toml_is_a_sentence_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "show", "--config", str(_write(tmp_path, "nope = = 1"))])
    assert exc.value.code == 2
    assert "配置有问题" in capsys.readouterr().err


def test_show_emits_json_a_browser_can_parse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The settings page consumes this output (§7.5), so `Infinity` is not an option.

    max_speech_ms defaults to inf and becomes null, the same statement the launch
    renderer makes by dropping the key (bootstrap/s2s_launch.py:90-92).
    """
    code = cli.main(["config", "show", "--config", str(_write(tmp_path, _CLEAN))])
    out = capsys.readouterr().out
    assert code == 0
    payload = _strict_json(out)
    assert payload["speech"]["s2s"]["turn"]["max_speech_ms"] is None
    assert payload["_derived"]["source"] == "chattiness"


def test_show_keeps_a_finite_limit_as_a_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only non-finite values are rewritten. A real limit stays a real number."""
    body = _CLEAN + "\n[speech.s2s.turn]\nmax_speech_ms = 30000\n"
    cli.main(["config", "show", "--config", str(_write(tmp_path, body))])
    payload = _strict_json(capsys.readouterr().out)
    assert payload["speech"]["s2s"]["turn"]["max_speech_ms"] == 30000
