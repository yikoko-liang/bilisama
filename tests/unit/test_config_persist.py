"""Targeted TOML persistence used by the live control centre."""

from __future__ import annotations

from pathlib import Path

import pytest

from bilisama.config.persist import TomlConfigWriter
from bilisama.config.schema import Settings


def test_profile_write_preserves_comments_and_unrelated_lines(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")
    user_root = tmp_path / "user-profiles"
    user_root.mkdir()
    profile = user_root / "normal.toml"
    profile.write_text(
        '[interaction]\nchattiness = "low"  # hand tuned\nreply_length = "medium"\n',
        encoding="utf-8",
    )

    writer = TomlConfigWriter(base, Settings(), user_profiles_root=user_root)
    target = writer.write("interaction.chattiness", "high")

    assert target == profile
    assert profile.read_text(encoding="utf-8") == (
        '[interaction]\nchattiness = "high"  # hand tuned\nreply_length = "medium"\n'
    )


def test_missing_nested_section_is_appended_to_active_profile(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")
    user_root = tmp_path / "user-profiles"

    writer = TomlConfigWriter(base, Settings(), user_profiles_root=user_root)
    target = writer.write("audio.noise_sensitivity", 72)

    assert target == user_root / "normal.toml"
    assert target.read_text(encoding="utf-8") == "[audio]\nnoise_sensitivity = 72\n"


def test_profile_writes_never_touch_the_checkout_tree(tmp_path: Path) -> None:
    """The repo's config/profiles/ is code; a skin picked mid-stream is
    runtime state. The shipped profile next to the base file must stay
    byte-identical however many panel edits land."""
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")
    shipped = tmp_path / "profiles" / "normal.toml"
    shipped.parent.mkdir()
    shipped_text = '[interaction]\nchattiness = "medium"\n'
    shipped.write_text(shipped_text, encoding="utf-8")

    writer = TomlConfigWriter(base, Settings(), user_profiles_root=tmp_path / "user-profiles")
    writer.write("interaction.chattiness", "high")
    writer.write("avatar.model_id", "candy")

    assert shipped.read_text(encoding="utf-8") == shipped_text


def test_the_default_user_root_is_the_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dev-talk builds the writer without the parameter; the default must be
    the same place the loader reads its user layer from."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")

    target = TomlConfigWriter(base, Settings()).write("interaction.chattiness", "high")

    assert target == tmp_path / "xdg" / "bilisama" / "profiles" / "normal.toml"


def test_active_profile_itself_is_written_to_the_base_file(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n[room]\nroom_id = 0\n', encoding="utf-8")

    TomlConfigWriter(base, Settings()).write("active_profile", "hype")

    assert base.read_text(encoding="utf-8").startswith('active_profile = "hype"\n')
