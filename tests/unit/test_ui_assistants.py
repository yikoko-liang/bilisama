"""Fixed Mia assistant and its two editable persona profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from bilisama.config import Settings
from bilisama.ui.assistants import (
    assistant_snapshot,
    persona_profile,
    profile_config_changes,
    save_anchor,
)
from bilisama.ui.config_edit import apply_runtime_config_edit


def _settings(*, profile: str = "default") -> Settings:
    return Settings(
        persona={
            "id": "mia",
            "display_name": "mia",
            "profile": profile,
        }
    )


def _templates(root: Path) -> None:
    folder = root / "personas" / "mia"
    modified = folder / "profiles" / "modified"
    modified.mkdir(parents=True)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "identity.md").write_text("# mia identity\n", encoding="utf-8")
    (folder / "personality.md").write_text("# mia personality\n", encoding="utf-8")
    (modified / "identity.md").write_text("# mia modified identity\n", encoding="utf-8")
    (modified / "personality.md").write_text("# mia modified personality\n", encoding="utf-8")


def test_snapshot_contains_only_fixed_mia_and_two_persona_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _templates(tmp_path)

    items = assistant_snapshot(_settings(), tmp_path)

    assert len(items) == 1
    assert items[0]["id"] == "mia"
    assert items[0]["current"] is True
    assert items[0]["avatar"] == {"renderer": "tofu", "model_id": ""}
    profiles = items[0]["profiles"]
    assert isinstance(profiles, list)
    assert [profile["id"] for profile in profiles] == ["default", "modified"]
    assert [profile["current"] for profile in profiles] == [True, False]
    assert profiles[1]["identity"] == "# mia modified identity\n"


def test_modified_profile_is_marked_current_without_changing_mia_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _templates(tmp_path)

    item = assistant_snapshot(_settings(profile="modified"), tmp_path)[0]

    assert item["id"] == "mia"
    profiles = item["profiles"]
    assert isinstance(profiles, list)
    assert [profile["current"] for profile in profiles] == [False, True]


def test_profile_selection_can_only_change_prompt_fields() -> None:
    assert profile_config_changes("modified") == (("persona.profile", "modified"),)

    settings = Settings(
        persona={"id": "mia", "profile": "default"},
        avatar={"renderer": "tofu", "model_id": ""},
        speech={"dashscope": {"voice": "longanlingxin"}},
    )
    for path, value in profile_config_changes("modified"):
        apply_runtime_config_edit(settings, path, value)

    assert settings.persona.id == "mia"
    assert settings.persona.profile == "modified"
    assert settings.avatar.renderer == "tofu"
    assert settings.avatar.model_id == ""
    assert settings.speech.dashscope.voice == "longanlingxin"


def test_profile_edit_is_atomic_and_visible_in_the_next_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _templates(tmp_path)
    settings = _settings()

    saved = save_anchor(settings, tmp_path, "modified", "identity", "# 新身份")

    assert saved == (
        tmp_path
        / "data"
        / "bilisama"
        / "personas"
        / "mia"
        / "profiles"
        / "modified"
        / "identity.md"
    )
    item = assistant_snapshot(settings, tmp_path)[0]
    profiles = item["profiles"]
    assert isinstance(profiles, list)
    assert profiles[1]["identity"] == "# 新身份\n"
    assert not saved.with_name("identity.md.tmp").exists()


def test_unknown_profile_and_blank_anchor_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _templates(tmp_path)
    settings = _settings()

    with pytest.raises(ValueError, match="没有"):
        persona_profile("unknown")
    with pytest.raises(ValueError, match="不能保存为空"):
        save_anchor(settings, tmp_path, "modified", "identity", "  \n")
