"""Layering rules for `config.load` (plan §7.4).

packaged defaults < bilisama.toml global < active profile < runtime panel override

The order matters twice over: an override that names a profile has to select that
profile, and an override that names an ordinary field still has to beat the profile
it just selected.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bilisama.config import Chattiness, load

BASE = """\
active_profile = "normal"

[room]
room_id = 12345

[speech.s2s]
llm_model = "our-s2t-v1"

[interaction]
chattiness = "medium"

[interaction.speak]
danmaku = true

[runtime]
log_level = "info"
"""

DEBUG_PROFILE = """\
active_profile = "debug"

[interaction]
chattiness = "low"

[interaction.speak]
danmaku = false

[runtime]
log_level = "debug"
"""

NORMAL_PROFILE = """\
[interaction]
chattiness = "medium"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "bilisama.toml").write_text(BASE, encoding="utf-8")
    (tmp_path / "profiles" / "debug.toml").write_text(DEBUG_PROFILE, encoding="utf-8")
    (tmp_path / "profiles" / "normal.toml").write_text(NORMAL_PROFILE, encoding="utf-8")
    return tmp_path / "bilisama.toml"


def test_profile_named_in_file_overlays_base(config_path: Path) -> None:
    s = load(config_path)
    assert s.active_profile == "normal"
    assert s.interaction.chattiness is Chattiness.MEDIUM
    assert s.runtime.log_level == "info"
    assert s.room.room_id == 12345  # a profile is an overlay, the base survives


def test_override_selects_the_profile(config_path: Path) -> None:
    """Switching profile from the panel has to read the profile file, not just
    relabel the settings object."""
    s = load(config_path, overrides={"active_profile": "debug"})
    assert s.active_profile == "debug"
    assert s.interaction.chattiness is Chattiness.LOW
    assert s.runtime.log_level == "debug"
    assert s.interaction.speak.danmaku is False
    assert s.room.room_id == 12345


def test_override_field_still_beats_the_profile_it_selected(config_path: Path) -> None:
    """The other half: overrides are the last layer, above the profile they picked."""
    s = load(
        config_path,
        overrides={"active_profile": "debug", "runtime": {"log_level": "error"}},
    )
    assert s.active_profile == "debug"
    assert s.runtime.log_level == "error"
    assert s.interaction.chattiness is Chattiness.LOW  # rest of the profile still applies


def test_override_without_profile_keeps_the_file_profile(config_path: Path) -> None:
    s = load(config_path, overrides={"runtime": {"log_level": "warning"}})
    assert s.active_profile == "normal"
    assert s.runtime.log_level == "warning"
    assert s.interaction.chattiness is Chattiness.MEDIUM


def test_unknown_profile_name_leaves_base_untouched(config_path: Path) -> None:
    """Pins today's behaviour: a profile that does not exist overlays nothing.

    Silently applying no overlay sits badly with plan §7.6, which wants every
    surprise reported. Changing that needs a Chinese message and a fatal-or-not
    decision, so it is a separate call — this test makes it a visible one.
    """
    s = load(config_path, overrides={"active_profile": "nope"})
    assert s.active_profile == "nope"
    assert s.runtime.log_level == "info"
    assert s.interaction.speak.danmaku is True


def test_no_file_falls_back_to_packaged_defaults(tmp_path: Path) -> None:
    """No path and a path that does not exist both mean "defaults only".

    strict=False because the packaged defaults carry no model id, which is a fatal
    problem in its own right — see test_config_validation.py.
    """
    for path in (None, tmp_path / "not-here.toml"):
        s = load(path, strict=False)
        assert s.active_profile == "normal"
        assert s.interaction.chattiness is Chattiness.MEDIUM
        assert s.room.room_id == 0


def test_malformed_toml_is_not_swallowed(tmp_path: Path) -> None:
    """The loader does not paper over a broken file; the CLI is what turns this
    into a sentence."""
    path = tmp_path / "bilisama.toml"
    path.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load(path)
