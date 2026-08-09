"""The command line, which is the only part of this project a streamer touches.

Two things are under test everywhere below: the exit code, because scripts/gate.sh
and CI branch on it, and the text, because plan §7.6 makes the wording a
requirement — a streamer who gets a traceback files a ticket instead of fixing the
config.

Every config lives in tmp_path. Pointing these at config/bilisama.toml would tie
the suite to a file the streamer is expected to edit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from bilisama import __version__, cli
from bilisama.bootstrap import s2s_launch
from bilisama.config import Chattiness, S2SConfig, derive

_CLEAN = """
config_version = 1
[speech.s2s]
llm_model = "our-s2t-v1"
"""

# Only advisory problems: the echo guard is off while audio goes straight out.
_ADVISORY = """
config_version = 1
[speech.s2s]
llm_model = "our-s2t-v1"
[audio]
output_route = "direct"
echo_guard = "off"
"""

# One advisory and one fatal, in that order, so the exit code cannot be read off
# the first problem alone.
_ADVISORY_AND_FATAL = """
config_version = 1
[speech.s2s]
llm_model = ""
[audio]
output_route = "direct"
echo_guard = "off"
"""

# A hosted provider that is otherwise complete, so render-s2s reaches its own
# provider check instead of stopping at validation.
_HOSTED = """
config_version = 1
[speech]
provider = "dashscope"
[speech.dashscope]
endpoint = "wss://dashscope.example/api/v1/realtime"
model = "qwen-omni-realtime"
[avatar]
expression_source = "lexicon"
"""

_DERIVED_KEYS = frozenset(
    ("idle_threshold_s", "danmaku_window_s", "score_threshold", "cooldown_s", "max_output_tokens")
)


def _write(tmp_path: Path, body: str, name: str = "bilisama.toml") -> Path:
    """Put a config in tmp_path and return its path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _key_paths(value: Any) -> set[str]:
    """Every mapping key anywhere in a nested payload."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys |= _key_paths(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _key_paths(child)
    return keys


def _fake_upstream(root: Path, fields: Iterable[str]) -> Path:
    """Write the one file `upstream_field_names` scans, declaring `fields`.

    Args:
        root: Stands in for a speech-to-speech checkout.
        fields: Field names upstream should be seen to accept.

    Returns:
        `root`, ready to pass as --s2s-root.
    """
    arg_dir = root / "src" / "speech_to_speech" / s2s_launch._UPSTREAM_ARG_DIR
    arg_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    {name}: str = ''" for name in sorted(fields))
    (arg_dir / "vad_arguments.py").write_text(
        f"class VADHandlerArguments:\n{body}\n", encoding="utf-8"
    )
    return root


# ------------------------------------------------------------------ config show


def test_show_prints_the_config_that_actually_took_effect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command exists to answer "what is this thing really running with?".

    The chattiness in the file has to reach both the settings body and the derived
    block, otherwise the output is describing the defaults rather than this config.
    """
    body = _CLEAN + '\n[interaction]\nchattiness = "high"\n'
    code = cli.main(["config", "show", "--config", str(_write(tmp_path, body))])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["interaction"]["chattiness"] == "high"
    assert payload["speech"]["s2s"]["llm_model"] == "our-s2t-v1"
    assert payload["_derived"]["source"] == "chattiness"
    derived = derive(Chattiness.HIGH).model_dump()
    for key, expected in derived.items():
        assert payload["_derived"][key] == expected


def test_show_keeps_derived_thresholds_out_of_the_settings_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boundary: the five thresholds appear under _derived and nowhere else.

    They are deliberately absent from the TOML (config/derive.py:3-6). Printing one
    inside the settings body would read as "you can set this", and then two writers
    would own the same number.
    """
    cli.main(["config", "show", "--config", str(_write(tmp_path, _CLEAN))])
    payload = json.loads(capsys.readouterr().out)

    assert set(payload["_derived"]) >= _DERIVED_KEYS
    del payload["_derived"]
    assert _DERIVED_KEYS.isdisjoint(_key_paths(payload))


def test_show_output_survives_a_round_trip_through_a_strict_parser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The settings page reads this (plan §7.5), so it has to be JSON, not repr.

    Lists are the part `_json_safe` walks separately: `patches` is a tuple in the
    schema and has to come out as a JSON array of strings.
    """
    cli.main(["config", "show", "--config", str(_write(tmp_path, _CLEAN))])
    payload = json.loads(capsys.readouterr().out)

    assert payload["speech"]["s2s"]["patches"] == ["text_modality", "raw_instructions"]
    assert isinstance(payload["interaction"]["speak"]["danmaku"], bool)


# ------------------------------------------- the two defences against Infinity
#
# JSON has no infinity and JSON.parse rejects the literal, so `Infinity` reaching
# the settings page (plan §7.5) is a blank panel with no clue why. There are two
# defences and the suite only exercised the first arm of the first one.


def test_json_safe_replaces_non_finite_floats_anywhere_in_the_payload() -> None:
    """The contract is "anywhere", so the walk has to enter lists, not only dicts.

    No settings field is a list of floats today, which is why every other test
    reaches the dict branch and none reach the list one. The branch is not
    decoration: `patches` shows sequence fields already exist in this schema
    (config/schema.py:55), and the day one of them carries a number, an unwalked
    list reopens the hole with the whole suite still green.
    """
    payload: dict[str, Any] = {
        "flat": float("inf"),
        "in_a_list": [1.0, float("-inf"), float("nan")],
        "nested": {"deeper": [[float("inf")], {"k": float("nan")}]},
        "left_alone": ["text", 3, True, None, 2.5],
    }

    assert cli._json_safe(payload) == {
        "flat": None,
        "in_a_list": [1.0, None, None],
        "nested": {"deeper": [[None], {"k": None}]},
        "left_alone": ["text", 3, True, None, 2.5],
    }


class _DerivedWithASequence(BaseModel):
    """A derived block whose field is a sequence — the shape `_json_safe` walks past.

    `cmd_show` builds `_derived` from `model_dump()` in python mode, where a tuple
    field stays a tuple (`patches` at config/schema.py:55 is exactly such a field).
    `_json_safe` handles dicts, lists and floats; a tuple is none of the three, and
    `json.dumps` serialises it as an array regardless.
    """

    cooldown_s: tuple[float, ...] = (float("inf"),)


def test_show_refuses_to_print_infinity_when_the_first_defence_misses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path, and the reason `allow_nan=False` is on the dumps call.

    Stacking `allow_nan=False` behind `_json_safe` only means something if it fires
    when `_json_safe` misses, and nothing reached that state before. This is a
    developer-facing guard rather than a config error — no TOML a streamer can write
    gets here — so failing loudly at the point of the mistake beats emitting a
    document the settings page cannot parse.
    """
    monkeypatch.setattr(cli, "derive", _fake_derive)

    with pytest.raises(ValueError):
        cli.main(["config", "show", "--config", str(_write(tmp_path, _CLEAN))])

    assert "Infinity" not in capsys.readouterr().out


def _fake_derive(chattiness: Chattiness) -> _DerivedWithASequence:
    """Stands in for `derive`, returning a block `_json_safe` cannot clean."""
    return _DerivedWithASequence()


# -------------------------------------------------------------- config validate


def test_validate_says_so_when_there_is_nothing_to_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _CLEAN))])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "配置没问题。"
    assert captured.err == ""


def test_validate_reports_an_advisory_on_stdout_and_still_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An advisory is worth printing but must not fail a build.

    It goes to stdout, not stderr: `config validate` succeeded, and a pipeline that
    treats anything on stderr as a failure would otherwise stop on advice.
    """
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _ADVISORY))])
    captured = capsys.readouterr()

    assert code == 0
    assert "[提醒] audio.output_route" in captured.out
    assert "怎么办：" in captured.out
    assert "[错误]" not in captured.out
    assert captured.err == ""


def test_validate_exits_1_when_any_problem_is_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boundary on the exit code: one fatal problem decides it, whatever precedes it.

    `check` returns the advisory first here, so reading the verdict off problems[0]
    would report success on a config that cannot start.
    """
    code = cli.main(["config", "validate", "--config", str(_write(tmp_path, _ADVISORY_AND_FATAL))])
    out = capsys.readouterr().out

    assert code == 1
    assert "[提醒] audio.output_route" in out
    assert "[错误] speech.s2s.llm_model" in out
    # Every problem carries an action, not just a diagnosis (plan §7.6).
    assert out.count("怎么办：") == 2
    assert "Traceback" not in out


# ------------------------------------------------------------ reading the file


@pytest.mark.parametrize("command", ["show", "validate", "render-s2s"])
def test_a_missing_config_file_is_a_sentence_naming_the_path(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path shared by every command that reads config.

    Each one has its own entry point, so the promise has to hold on all of them: a
    filename the streamer can check, and no traceback.
    """
    missing = tmp_path / "not-here.toml"
    argv = ["config", command, "--config", str(missing)]
    if command == "render-s2s":
        argv += ["--out", str(tmp_path / "s2s.json")]

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "找不到配置文件" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_malformed_toml_is_a_sentence_not_a_parser_dump(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path: a broken file exists, so this is a different arm from the one above."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "show", "--config", str(_write(tmp_path, "= = =\n"))])

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "配置有问题" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_a_directory_given_as_the_config_is_reported_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boundary between the two arms: the path exists, but reading it raises OSError.

    Tab completion on a directory name is how this gets typed in practice.
    """
    directory = tmp_path / "conf.d"
    directory.mkdir()

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "validate", "--config", str(directory)])

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "配置有问题" in err
    assert "Traceback" not in err


# ------------------------------------------------------------ config render-s2s


def test_render_s2s_writes_valid_json_and_creates_the_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Normal path. A live stream launches from this file, so it has to parse.

    The trailing newline matters for the same reason every other config file has
    one: `cat` and `git diff` on a file without it are unreadable.
    """
    out = tmp_path / "generated" / "bilisama-s2s.json"
    code = cli.main(
        [
            "config",
            "render-s2s",
            "--config",
            str(_write(tmp_path, _CLEAN)),
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert f"已写入 {out}" in captured.out
    assert captured.err == ""

    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert payload["stt"] == "none"
    assert payload["llm_backend"] == "chat-completions"
    assert payload["model_name"] == "our-s2t-v1"


def test_render_s2s_refuses_a_provider_that_has_no_launch_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path. A hosted provider has nothing to launch, so there is nothing to render.

    The message names the provider: someone who runs this command usually believes
    the config says s2s, and the useful answer is what it actually says.
    """
    out = tmp_path / "s2s.json"
    code = cli.main(
        [
            "config",
            "render-s2s",
            "--config",
            str(_write(tmp_path, _HOSTED)),
            "--out",
            str(out),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "dashscope" in captured.err
    assert "不需要渲染" in captured.err
    assert "已写入" not in captured.out
    assert not out.exists()


def test_render_s2s_names_the_turn_fields_it_did_not_pass_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: reconciliation succeeded, and still found something worth saying.

    A turn-detection parameter upstream accepts but we never send is not an error —
    upstream falls back to its own default — but it is exactly the surprise that
    sends someone tuning a VAD threshold that was never being applied. Faked by
    dropping a key from the payload, because every field is passed through today.
    """
    dropped = "speech_pad_ms"
    original = s2s_launch.render

    def render_without_the_field(cfg: S2SConfig) -> dict[str, object]:
        payload = original(cfg)
        payload.pop(dropped)
        return payload

    monkeypatch.setattr(s2s_launch, "render", render_without_the_field)
    root = _fake_upstream(tmp_path / "upstream", original(S2SConfig(llm_model="our-s2t-v1")))
    out = tmp_path / "s2s.json"

    code = cli.main(
        [
            "config",
            "render-s2s",
            "--config",
            str(_write(tmp_path, _CLEAN)),
            "--out",
            str(out),
            "--s2s-root",
            str(root),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert f"提醒：这些判停参数没有透传：{dropped}" in captured.out
    assert dropped not in json.loads(out.read_text(encoding="utf-8"))


# ------------------------------------------------------------ config chattiness


def test_chattiness_prints_every_level_and_marks_the_chosen_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normal path. Answers "why is it talking this much?" without a config file."""
    code = cli.main(["config", "chattiness", "--level", "high"])
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert len(lines) == len(Chattiness)
    marked = [line for line in lines if "←" in line]
    assert len(marked) == 1
    assert marked[0].split()[0] == "high"
    # The numbers, not just the names: this is the table people read off.
    assert str(derive(Chattiness.HIGH).cooldown_s) in marked[0]


def test_chattiness_still_prints_the_table_for_an_unknown_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Error path. --level takes a free string, so a typo has to be survivable.

    Printing the table with nothing marked is the answer to a typo: the valid names
    are right there. Raising KeyError would not be.
    """
    code = cli.main(["config", "chattiness", "--level", "bogus"])
    out = capsys.readouterr().out

    assert code == 0
    assert len(out.splitlines()) == len(Chattiness)
    assert "←" not in out
    for level in Chattiness:
        assert level.value in out


def test_chattiness_defaults_to_medium(capsys: pytest.CaptureFixture[str]) -> None:
    """Boundary: with no --level the marker still lands somewhere."""
    assert cli.main(["config", "chattiness"]) == 0
    marked = [line for line in capsys.readouterr().out.splitlines() if "←" in line]
    assert len(marked) == 1
    assert marked[0].split()[0] == Chattiness.MEDIUM.value


# ---------------------------------------------------------------- parser itself


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize("argv", [[], ["config"]])
def test_a_missing_subcommand_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path. Both subparser levels are required, so neither may fall through
    to an AttributeError on args.func."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "usage: bilisama" in err


def test_default_config_path_is_absolute_and_exists() -> None:
    """The default is resolved from the package, not the working directory, so the
    CLI works from anywhere. A package move would silently point it elsewhere."""
    assert cli.DEFAULT_CONFIG.is_absolute()
    assert cli.DEFAULT_CONFIG.exists()
    assert cli.DEFAULT_CONFIG.name == "bilisama.toml"
