"""Keep remote input commits opt-in and later than the local speech release."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from bilisama.config import ProviderName, Settings, check
from bilisama.config._ui import Audience, Reload
from bilisama.config.schema import VolcanoDuplexConfig
from bilisama.config.ui_meta import UI_META
from bilisama.ui.config_edit import ConfigEditError, apply_checked_runtime_edit
from bilisama.ui.server import config_snapshot

_EDGE_FIELD = "speech.volcano_duplex.speech_edge_s"
_COMMIT_FIELD = "speech.volcano_duplex.speech_commit_s"


def _settings(edge_s: float, commit_s: float) -> Settings:
    return Settings.model_validate(
        {
            "speech": {
                "provider": "volcano_duplex",
                "volcano_duplex": {"speech_edge_s": edge_s, "speech_commit_s": commit_s},
            }
        }
    )


def test_remote_input_commit_ships_disabled() -> None:
    assert VolcanoDuplexConfig().speech_commit_s == 0.0
    config = Path(__file__).resolve().parents[2] / "config/bilisama.toml"
    shipped = tomllib.loads(config.read_text(encoding="utf-8"))
    assert shipped["speech"]["volcano_duplex"]["speech_commit_s"] == 0.0


@pytest.mark.parametrize("value", [-0.1, 30.1, float("inf"), float("nan")])
def test_remote_input_commit_rejects_invalid_seconds(value: float) -> None:
    with pytest.raises(ValidationError):
        VolcanoDuplexConfig(speech_commit_s=value)


@pytest.mark.parametrize("edge_s,commit_s", [(0.3, 0.0), (10.0, 0.0), (1.0, 2.5), (10.0, 30.0)])
def test_disabled_or_later_remote_commit_is_valid(edge_s: float, commit_s: float) -> None:
    assert not [p for p in check(_settings(edge_s, commit_s)) if p.field == _COMMIT_FIELD]


@pytest.mark.parametrize("edge_s,commit_s", [(1.0, 0.5), (1.0, 1.0), (10.0, 9.0)])
def test_remote_commit_cannot_run_before_or_with_local_release(
    edge_s: float, commit_s: float
) -> None:
    problems = [p for p in check(_settings(edge_s, commit_s)) if p.field == _COMMIT_FIELD]
    assert len(problems) == 1
    assert problems[0].fatal and problems[0].fix


def test_remote_commit_rule_ignores_an_inactive_provider() -> None:
    settings = _settings(1.0, 0.5)
    settings.speech.provider = ProviderName.DASHSCOPE
    assert not [p for p in check(settings) if p.field == _COMMIT_FIELD]


@pytest.mark.parametrize("path", [_EDGE_FIELD, _COMMIT_FIELD])
def test_speech_deadlines_are_live_operator_settings(path: str) -> None:
    meta = UI_META[path]
    assert meta.audience is Audience.OPERATOR
    assert meta.reload is Reload.LIVE
    assert meta.provider_scoped == "volcano_duplex"


def test_panel_rejects_either_edit_that_moves_commit_before_local_release() -> None:
    settings = _settings(1.0, 0.0)
    with pytest.raises(ConfigEditError):
        apply_checked_runtime_edit(settings, _COMMIT_FIELD, 1.0, config_dir=None)
    assert settings.speech.volcano_duplex.speech_commit_s == 0.0

    _, applied = apply_checked_runtime_edit(settings, _COMMIT_FIELD, 3.0, config_dir=None)
    assert applied == 3.0
    with pytest.raises(ConfigEditError):
        apply_checked_runtime_edit(
            settings, "speech.volcano_duplex.speech_edge_s", 3.0, config_dir=None
        )
    assert settings.speech.volcano_duplex.speech_edge_s == 1.0
    _, applied = apply_checked_runtime_edit(settings, _COMMIT_FIELD, 0.0, config_dir=None)
    assert applied == 0.0


@pytest.mark.parametrize(
    "path,value,minimum,maximum",
    [(_EDGE_FIELD, 1.0, 0.3, 10.0), (_COMMIT_FIELD, 0.0, 0.0, 30.0)],
)
def test_panel_snapshot_exposes_numeric_deadline_editors(
    path: str, value: float, minimum: float, maximum: float
) -> None:
    rows = {row["path"]: row for row in config_snapshot(_settings(1.0, 0.0))}
    row = rows[path]
    assert row["editable"] is True
    assert row["reload"] == "live"
    assert row["kind"] == "number"
    assert row["value"] == value
    assert (row["min"], row["max"]) == (minimum, maximum)


def test_deadline_editors_are_hidden_for_other_providers() -> None:
    settings = _settings(1.0, 0.0)
    settings.speech.provider = ProviderName.DASHSCOPE
    paths = {row["path"] for row in config_snapshot(settings)}
    assert _EDGE_FIELD not in paths
    assert _COMMIT_FIELD not in paths
