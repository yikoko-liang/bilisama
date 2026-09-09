"""Behavioural checks for the double-click startup entry point."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from bilisama import cli
from bilisama.config import ProviderName, load

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_check(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    entry: str = "start_bilisama.sh",
) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(REPO_ROOT / "start_bilisama.sh", root / "start_bilisama.sh")
    shutil.copy2(REPO_ROOT / "start_bilisama.command", root / "start_bilisama.command")
    (root / "path.sh").write_text(
        "export volcano_api_key=test-volcano\n"
        "export ali_api_key=test-dashscope\n"
        "export dashscope_url=wss://example.invalid\n",
        encoding="utf-8",
    )
    _make_executable(root / ".venv/bin/python")
    _make_executable(root / ".venv/bin/bilisama")
    _make_executable(
        root / "desktop/preview/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    )

    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    for name in (
        "BILISAMA_PROVIDER",
        "BILISAMA_REALTIME_MODEL",
        "BILISAMA_VOICE",
    ):
        env.pop(name, None)
    if env_overrides is not None:
        env.update(env_overrides)
    completed = subprocess.run(
        [
            "bash",
            str(root / entry),
            "--check",
            "--input-device",
            "1",
            *args,
        ],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_default_start_uses_volcano_sc2_and_the_cloned_voice(tmp_path: Path) -> None:
    output = _run_check(tmp_path)
    assert "后端 volcano" in output
    assert "模型 2.2.0.0" in output
    assert "音色 saturn_zh_female_keainvsheng_tob" in output


def test_dashscope_override_gets_only_dashscope_defaults(tmp_path: Path) -> None:
    output = _run_check(tmp_path, "--provider", "dashscope")
    assert "后端 dashscope" in output
    assert "模型 qwen-audio-3.0-realtime-flash" in output
    assert "saturn_zh_female_keainvsheng_tob" not in output


def test_flags_override_environment_model_and_voice(tmp_path: Path) -> None:
    output = _run_check(
        tmp_path,
        "--provider",
        "volcano",
        "--model",
        "1.2.1.1",
        "--voice",
        "zh_female_vv_jupiter_bigtts",
        env_overrides={
            "BILISAMA_PROVIDER": "dashscope",
            "BILISAMA_REALTIME_MODEL": "from-env",
            "BILISAMA_VOICE": "from-env",
        },
    )
    assert "后端 volcano" in output
    assert "模型 1.2.1.1" in output
    assert "音色 zh_female_vv_jupiter_bigtts" in output
    assert "from-env" not in output


def test_local_provider_does_not_inherit_cloud_defaults(tmp_path: Path) -> None:
    output = _run_check(tmp_path, "--provider", "s2s")
    assert "后端 s2s" in output
    assert "模型 从 [speech.s2s] 读" in output
    assert "saturn_zh_female_keainvsheng_tob" not in output


def test_direct_cli_config_defaults_to_the_same_doubao_model_and_voice(tmp_path: Path) -> None:
    assert cli.DEFAULT_CONFIG == REPO_ROOT / "config/bilisama.toml"
    settings = load(cli.DEFAULT_CONFIG, user_profiles_root=tmp_path / "profiles")
    assert settings.speech.provider is ProviderName.VOLCANO
    assert settings.speech.volcano.model == "2.2.0.0"
    assert settings.speech.volcano.speaker == "saturn_zh_female_keainvsheng_tob"
    assert settings.avatar.expression_source == "lexicon"


def test_finder_entry_delegates_to_the_same_doubao_defaults(tmp_path: Path) -> None:
    output = _run_check(tmp_path, entry="start_bilisama.command")
    assert "后端 volcano" in output
    assert "模型 2.2.0.0" in output
    assert "音色 saturn_zh_female_keainvsheng_tob" in output


def test_finder_entry_preserves_explicit_other_provider(tmp_path: Path) -> None:
    output = _run_check(
        tmp_path,
        "--provider=dashscope",
        "--voice=longanlingxin",
        entry="start_bilisama.command",
    )
    assert "后端 dashscope" in output
    assert "模型 qwen-audio-3.0-realtime-flash" in output
    assert "音色 longanlingxin" in output
    assert "2.2.0.0" not in output
    assert "saturn_zh_female_keainvsheng_tob" not in output


def test_provider_environment_override_keeps_provider_specific_defaults(tmp_path: Path) -> None:
    output = _run_check(tmp_path, env_overrides={"BILISAMA_PROVIDER": "s2s"})
    assert "后端 s2s" in output
    assert "2.2.0.0" not in output
    assert "saturn_zh_female_keainvsheng_tob" not in output
