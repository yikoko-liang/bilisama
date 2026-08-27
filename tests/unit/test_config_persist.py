"""Targeted TOML persistence used by the live control centre."""

from __future__ import annotations

from pathlib import Path

from bilisama.config.persist import TomlConfigWriter
from bilisama.config.schema import Settings


def test_profile_write_preserves_comments_and_unrelated_lines(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")
    profile = tmp_path / "profiles" / "normal.toml"
    profile.parent.mkdir()
    profile.write_text(
        '[interaction]\nchattiness = "low"  # hand tuned\nreply_length = "medium"\n',
        encoding="utf-8",
    )

    target = TomlConfigWriter(base, Settings()).write("interaction.chattiness", "high")

    assert target == profile
    assert profile.read_text(encoding="utf-8") == (
        '[interaction]\nchattiness = "high"  # hand tuned\nreply_length = "medium"\n'
    )


def test_missing_nested_section_is_appended_to_active_profile(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n', encoding="utf-8")

    target = TomlConfigWriter(base, Settings()).write("audio.noise_sensitivity", 72)

    assert target == tmp_path / "profiles" / "normal.toml"
    assert target.read_text(encoding="utf-8") == "[audio]\nnoise_sensitivity = 72\n"


def test_active_profile_itself_is_written_to_the_base_file(tmp_path: Path) -> None:
    base = tmp_path / "bilisama.toml"
    base.write_text('active_profile = "normal"\n[room]\nroom_id = 0\n', encoding="utf-8")

    TomlConfigWriter(base, Settings()).write("active_profile", "hype")

    assert base.read_text(encoding="utf-8").startswith('active_profile = "hype"\n')
