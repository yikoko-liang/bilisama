"""`bilisama persona review`: the human hand on the promotion gate."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from bilisama.cli import main
from bilisama.persona.loader import PersonaStore


@pytest.fixture()
def config_tree(tmp_path: Path) -> Path:
    """A minimal but strictly loadable config with its own persona templates."""
    (tmp_path / "personas" / "mia").mkdir(parents=True)
    (tmp_path / "personas" / "mia" / "identity.md").write_text(
        "# 我是谁\n测试人设", encoding="utf-8"
    )
    (tmp_path / "personas" / "mia" / "personality.md").write_text(
        "# 性格\n- 爱接梗", encoding="utf-8"
    )
    config = tmp_path / "bilisama.toml"
    config.write_text(
        "\n".join(
            [
                "[speech.s2s]",
                'llm_model = "test-model"',
                "[persona]",
                'id = "mia"',
                f'data_dir = "{tmp_path / "live"}"',
            ]
        ),
        encoding="utf-8",
    )
    return config


def _store(config: Path) -> PersonaStore:
    return PersonaStore(config.parent / "live", config.parent / "personas" / "mia")


def _run(*argv: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(list(argv)) == 0
    return out.getvalue()


def test_review_lists_growth_entries_with_refs(config_tree: Path) -> None:
    _store(config_tree).write_growth("voice", ["这把稳了", "蚌埠住了"])

    out = _run("persona", "review", "--config", str(config_tree))
    assert "[v1] 这把稳了" in out
    assert "[v2] 蚌埠住了" in out
    assert "共同经历" in out and "（空）" in out


def test_promote_moves_the_entry_and_says_so(config_tree: Path) -> None:
    store = _store(config_tree)
    store.write_growth("voice", ["这把稳了", "蚌埠住了"])

    out = _run("persona", "review", "--config", str(config_tree), "--promote", "v2")
    assert "已合并" in out

    live = (config_tree.parent / "live" / "personality.md").read_text(encoding="utf-8")
    assert "- 蚌埠住了" in live
    assert store.growth_entries("voice") == ["这把稳了"]


def test_drop_removes_an_entry_without_touching_personality(config_tree: Path) -> None:
    store = _store(config_tree)
    store.write_growth("relationship", ["2026-08-12 起了外号"])

    out = _run("persona", "review", "--config", str(config_tree), "--drop", "r1")
    assert "已删掉" in out
    assert store.growth_entries("relationship") == []
    assert not (config_tree.parent / "live" / "personality.md").exists()


def test_a_bad_ref_exits_with_a_pointer(config_tree: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["persona", "review", "--config", str(config_tree), "--promote", "x9"])
    assert excinfo.value.code == 2
