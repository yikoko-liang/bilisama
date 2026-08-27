"""Layering rules for `config.load` (plan §7.4).

packaged defaults < bilisama.toml global < active profile < runtime panel override

The order matters twice over: an override that names a profile has to select that
profile, and an override that names an ordinary field still has to beat the profile
it just selected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bilisama.config import Chattiness, ConfigError, load
from bilisama.config.migrate import CURRENT_VERSION, MIGRATIONS, Step, migrate

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
    # BASE sets a room_id, and a config that is going live must carry a
    # wordlist (plan section 7.6 row 7) — strict load refuses otherwise.
    (tmp_path / "safety").mkdir()
    (tmp_path / "safety" / "wordlist.txt").write_text("测试敏感词\n", encoding="utf-8")
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
    """The loader does not paper over a broken file, and the refusal names WHICH
    file — with profile overlays the broken line is as likely in
    profiles/<name>.toml as in the base file (D7)."""
    path = tmp_path / "bilisama.toml"
    path.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load(path)
    assert exc_info.value.problems[0].field == str(path)


def test_broken_profile_toml_names_the_profile_file(tmp_path: Path) -> None:
    """The half that motivated D7: the base parses fine, the overlay does not,
    and the error must point at the overlay."""
    path = tmp_path / "bilisama.toml"
    path.write_text(BASE, encoding="utf-8")
    (tmp_path / "safety").mkdir()
    (tmp_path / "safety" / "wordlist.txt").write_text("测试词\n", encoding="utf-8")
    profile = tmp_path / "profiles" / "normal.toml"
    profile.parent.mkdir()
    profile.write_text("this is not = = toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load(path)
    assert exc_info.value.problems[0].field == str(profile)


# ------------------------------------------------------------ config_version


def test_a_file_from_the_future_is_refused_in_plain_language(config_path: Path) -> None:
    """The case that has no good silent answer.

    A newer BiliSama can change what a value MEANS, and this one would read it
    with today's meaning and behave differently from what the file says. Loud is
    the only honest option, and the message has to name both numbers.
    """
    config_path.write_text(f"config_version = {CURRENT_VERSION + 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load(config_path)
    problem = exc_info.value.problems[0]
    assert problem.field == "config_version"
    assert str(CURRENT_VERSION + 1) in problem.message
    assert problem.fix


def test_a_file_from_the_future_is_still_reported_without_strict(config_path: Path) -> None:
    """dev-talk loads with strict=False so a half-configured box stays usable
    (dev_talk.py:898). That must not turn "cannot read this file" into silence."""
    config_path.write_text(f"config_version = {CURRENT_VERSION + 9}\n", encoding="utf-8")
    settings = load(config_path, strict=False)
    from bilisama.config import check

    assert "config_version" in [p.field for p in check(settings) if p.fatal]


def test_todays_file_needs_no_migration() -> None:
    raw, notes = migrate({"config_version": CURRENT_VERSION, "room": {"room_id": 7}})
    assert notes == ()
    assert raw["room"]["room_id"] == 7


def test_a_file_with_no_version_is_read_as_the_oldest_shape() -> None:
    """Absent is not "current": the key was optional before it meant anything, so
    a file without it is the oldest shape we know, not the newest."""
    raw, _ = migrate({"room": {"room_id": 7}}, steps={}, current=1)
    assert raw["config_version"] == 1


def _rename_room(raw: dict[str, Any]) -> dict[str, Any]:
    moved = dict(raw)
    moved["room"] = {"room_id": moved.pop("legacy_room_id", 0)}
    return moved


def test_the_machinery_runs_a_migration_and_chains_them() -> None:
    """Today's table is empty, so the steps here are planted ones.

    Same reasoning as the planted violations in test_dependency_direction.py: a
    migration path nobody has ever walked is not a migration path, and the first
    real schema change is the worst moment to find that out.
    """
    steps: dict[int, Step] = {
        1: _rename_room,
        2: lambda raw: {**raw, "active_profile": "chat"},
    }
    raw, notes = migrate({"config_version": 1, "legacy_room_id": 12345}, steps=steps, current=3)
    assert raw["room"] == {"room_id": 12345}
    assert "legacy_room_id" not in raw
    assert raw["active_profile"] == "chat"
    assert raw["config_version"] == 3
    assert len(notes) == 2  # one per step, for the line the CLI prints


def test_a_gap_in_the_table_refuses_rather_than_guessing() -> None:
    """A version with no step out of it cannot be upgraded, and pretending it can
    hands the schema a shape it will misread."""
    with pytest.raises(ConfigError) as exc_info:
        migrate({"config_version": 1}, steps={}, current=2)
    assert exc_info.value.problems[0].field == "config_version"


def test_every_shipped_version_has_a_way_forward() -> None:
    """The gate that keeps the case above unreachable in production."""
    missing = [v for v in range(1, CURRENT_VERSION) if v not in MIGRATIONS]
    assert not missing, f"这些 config_version 没有升级步骤：{missing}"
    ahead = sorted(v for v in MIGRATIONS if v >= CURRENT_VERSION)
    assert not ahead, f"迁移表里有还没到的版本：{ahead}"


def test_a_non_integer_version_is_left_for_the_schema_to_report() -> None:
    """Type errors have one reporter, and it is not this module — `config_version
    = "2"` comes back as a field error with a path in front (cli.py:50-59)."""
    raw, notes = migrate({"config_version": "2"})
    assert raw["config_version"] == "2"
    assert notes == ()


def test_the_renamed_persona_still_loads(tmp_path: Path) -> None:
    """The shipped persona was renamed mia→tofu and the old directory went with
    it, so an existing config still naming mia died at startup on
    `FileNotFoundError: 人设文件缺失` — a rename presented as a missing file.

    This is the migration table's first real entry, and it is exactly the case
    plan §7.7 wanted the machinery in place for before the first format change
    rather than after it.
    """
    path = tmp_path / "bilisama.toml"
    path.write_text(
        'config_version = 1\n[persona]\nid = "mia"\n[speech.s2s]\nllm_model = "m"\n',
        encoding="utf-8",
    )

    settings = load(path, strict=False)

    assert settings.persona.id == "tofu"
    assert settings.config_version == CURRENT_VERSION


def test_a_persona_nobody_renamed_is_left_alone(tmp_path: Path) -> None:
    """A rename table that rewrites more than it was given is worse than none."""
    path = tmp_path / "bilisama.toml"
    path.write_text(
        'config_version = 1\n[persona]\nid = "hanako"\n[speech.s2s]\nllm_model = "m"\n',
        encoding="utf-8",
    )

    assert load(path, strict=False).persona.id == "hanako"


def test_the_rename_says_where_the_grown_files_went(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The half a config migration cannot do for anyone: the relationship and
    voice files live under the data home by the OLD name, and would just stop
    being read. Silence there reads as "the AI forgot everything"."""
    path = tmp_path / "bilisama.toml"
    path.write_text(
        'config_version = 1\n[persona]\nid = "mia"\n[speech.s2s]\nllm_model = "m"\n',
        encoding="utf-8",
    )

    with caplog.at_level("INFO"):
        load(path, strict=False)

    said = "\n".join(record.getMessage() for record in caplog.records)
    assert "personas/mia" in said
    assert "personas/tofu" in said
