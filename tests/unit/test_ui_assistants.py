"""The assistant page's data: shipped personas, switchable and editable."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bilisama.config.schema import Settings
from bilisama.ui.assistants import assistant_snapshot, list_personas, save_anchor

REPO = Path(__file__).resolve().parents[2]


def _config_dir(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    for persona in ("tofu", "hanako"):
        shutil.copytree(REPO / "config" / "personas" / persona, root / "personas" / persona)
    shutil.copytree(REPO / "config" / "personas" / "live", root / "personas" / "live")
    return root


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # data_dir stays "auto" (production shape); the data home is redirected
    # under tmp so live copies never touch the real one.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return Settings.model_validate({"persona": {"id": "tofu"}})


def test_live_rules_directory_is_not_listed_as_a_persona(tmp_path: Path) -> None:
    root = _config_dir(tmp_path)
    assert list_personas(root) == ["hanako", "tofu"]


def test_snapshot_carries_anchors_and_marks_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _config_dir(tmp_path)
    cards = assistant_snapshot(_settings(tmp_path, monkeypatch), root)
    by_id = {card["id"]: card for card in cards}
    assert set(by_id) == {"hanako", "tofu"}
    assert by_id["tofu"]["current"] is True and by_id["hanako"]["current"] is False
    assert by_id["hanako"]["identity"].strip(), "the editor needs the full template text"
    assert by_id["tofu"]["name"], "a card without a name is unclickable"


def test_save_anchor_writes_the_live_copy_and_the_next_snapshot_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _config_dir(tmp_path)
    settings = _settings(tmp_path, monkeypatch)
    target = save_anchor(settings, root, "hanako", "identity", "# 花子\n她今晚换个说法")
    assert target == tmp_path / "xdg" / "bilisama" / "personas" / "hanako" / "identity.md"
    cards = {card["id"]: card for card in assistant_snapshot(settings, root)}
    assert "换个说法" in cards["hanako"]["identity"]
    shipped = (root / "personas" / "hanako" / "identity.md").read_text(encoding="utf-8")
    assert "换个说法" not in shipped, "the shipped template stays untouched"


def test_unknown_persona_or_anchor_refuses_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _config_dir(tmp_path)
    with pytest.raises(ValueError, match="没有「nobody」这个人设"):
        save_anchor(_settings(tmp_path, monkeypatch), root, "nobody", "identity", "x")


def test_an_explicit_data_dir_never_mixes_two_personas_live_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit data_dir names the ACTIVE persona's directory; editing a
    non-current persona must not write into it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root = _config_dir(tmp_path)
    settings = Settings.model_validate(
        {"persona": {"id": "tofu", "data_dir": str(tmp_path / "tofu-live")}}
    )
    save_anchor(settings, root, "hanako", "identity", "# 花子\n改动")
    assert not (tmp_path / "tofu-live" / "identity.md").exists()
