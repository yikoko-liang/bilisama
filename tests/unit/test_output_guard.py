"""How the wordlist gets from disk into the guard.

The matching itself is pinned in test_director.py, where the guard is driven
through the scheduler the way a reply drives it. What had no cover at all was the
step before that: load_guard, resolve_safety_path and _read_list — the only route
from [safety] in the TOML to an OutputGuard, walked on every dev-talk start
(dev_talk.py:1238) and by nothing else.

Ledger #43 is about this path specifically: it says "auto" goes wrong once the
config no longer sits next to the shipped lists, i.e. after an install. The
entry says it will break; until now nothing asked it to.

Plan section 7.6 makes a missing wordlist a refuse-to-start condition, so the
error path here is as much the contract as the happy one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilisama.config.schema import SafetyConfig
from bilisama.director.output_guard import (
    OutputGuard,
    _read_list,
    load_guard,
    resolve_safety_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _lists(config_dir: Path, *, words: str, allow: str | None = None) -> None:
    """Write a shipped-layout pair of lists under config_dir/safety/."""
    safety = config_dir / "safety"
    safety.mkdir(parents=True, exist_ok=True)
    (safety / "wordlist.txt").write_text(words, encoding="utf-8")
    if allow is not None:
        (safety / "allowlist.txt").write_text(allow, encoding="utf-8")


# ------------------------------------------------------------ resolve_safety_path


def test_auto_means_the_list_shipped_beside_the_config(tmp_path: Path) -> None:
    """The default both fields carry, and the one ledger #43 is about.

    "auto" is resolved against the directory the config file was loaded from —
    not the working directory and not the package — so a config that moves away
    from the shipped lists takes the lists with it or finds nothing.
    """
    resolved = resolve_safety_path("auto", config_dir=tmp_path, default_name="wordlist.txt")

    assert resolved == tmp_path / "safety" / "wordlist.txt"


def test_an_explicit_path_ignores_the_config_directory(tmp_path: Path) -> None:
    """The streamer's own list lives wherever they put it."""
    resolved = resolve_safety_path(
        "/srv/lists/mine.txt", config_dir=tmp_path, default_name="wordlist.txt"
    )

    assert resolved == Path("/srv/lists/mine.txt")


def test_a_tilde_path_is_expanded() -> None:
    """`~/lists/mine.txt` is what somebody will actually type into the TOML, and
    an unexpanded one becomes a relative directory literally named `~`."""
    resolved = resolve_safety_path("~/mine.txt", config_dir=Path("/tmp"), default_name="w.txt")

    assert resolved == Path.home() / "mine.txt"
    assert "~" not in str(resolved)


def test_the_shipped_lists_are_where_auto_looks_for_them() -> None:
    """The repo's own config directory has to satisfy the default.

    This is the half of ledger #43 that can be checked without an installer: if
    it ever goes red in the source tree, "auto" is broken for everybody, not just
    for an installed copy.
    """
    config_dir = _REPO_ROOT / "config"
    for name in ("wordlist.txt", "allowlist.txt"):
        path = resolve_safety_path("auto", config_dir=config_dir, default_name=name)
        assert path.is_file(), f"「auto」指向的 {path} 不存在"


# ------------------------------------------------------------ _read_list


def test_blank_lines_and_comments_are_not_banned_words(tmp_path: Path) -> None:
    """A blank entry would match every delta ever sent.

    OutputGuard.__init__ drops empty strings as a second line of defence, but a
    reader that returned them would still put "#" and any comment text into the
    list — and a banned word of "#" mutes the stream on a hashtag.
    """
    path = tmp_path / "wordlist.txt"
    path.write_text("# 注释\n\n  违禁词  \n\n#另一条注释\n第二个\n", encoding="utf-8")

    assert _read_list(path) == ["违禁词", "第二个"]


def test_an_empty_file_reads_as_an_empty_list(tmp_path: Path) -> None:
    """Boundary: a file with nothing in it is not an error, it is no words."""
    path = tmp_path / "wordlist.txt"
    path.write_text("", encoding="utf-8")

    assert _read_list(path) == []


# ------------------------------------------------------------ load_guard


def test_load_guard_wires_both_lists_through(tmp_path: Path) -> None:
    """The happy path, end to end: TOML defaults in, working guard out."""
    _lists(tmp_path, words="违禁词\n河\n", allow="河北\n")

    guard = load_guard(SafetyConfig(), config_dir=tmp_path)

    assert guard.hit("说了违禁词") == "违禁词"
    guard.reset()
    # The allowlist arrived too, or 「河北」 would trip the ban on 「河」.
    assert guard.hit("河北的朋友") is None


def test_a_missing_allowlist_is_not_fatal(tmp_path: Path) -> None:
    """Boundary: only the wordlist is a start-up condition.

    An allowlist is a false-positive tuning file. Refusing to start without one
    would make the safer configuration the harder one to run.
    """
    _lists(tmp_path, words="违禁词\n")

    guard = load_guard(SafetyConfig(), config_dir=tmp_path)

    assert guard.hit("说了违禁词") == "违禁词"


def test_a_missing_wordlist_refuses_by_name(tmp_path: Path) -> None:
    """Error path: plan section 7.6's refuse-to-start, and the shape it takes.

    The path is in the message because that is the whole fix — the caller turns
    this into a SystemExit the streamer reads (dev_talk.py:1239-1240), and a
    message without the path leaves them guessing which of the two "auto"s
    resolved wrong.
    """
    with pytest.raises(FileNotFoundError, match=r"敏感词表不存在") as caught:
        load_guard(SafetyConfig(), config_dir=tmp_path)

    assert str(tmp_path / "safety" / "wordlist.txt") in str(caught.value)


def test_a_wordlist_path_that_points_at_a_directory_still_refuses(tmp_path: Path) -> None:
    """Error path: is_file(), not exists().

    A path that exists but is not a file would sail past an existence check and
    then blow up in read_text with an IsADirectoryError traceback — the shape of
    failure this layer is supposed to convert into a sentence.
    """
    (tmp_path / "safety" / "wordlist.txt").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match=r"敏感词表不存在"):
        load_guard(SafetyConfig(), config_dir=tmp_path)


def test_an_explicit_wordlist_path_beats_auto(tmp_path: Path) -> None:
    """The streamer's own list, resolved without reference to config_dir."""
    mine = tmp_path / "elsewhere" / "mine.txt"
    mine.parent.mkdir()
    mine.write_text("自定义词\n", encoding="utf-8")
    _lists(tmp_path, words="不该用到的词\n")

    guard = load_guard(
        SafetyConfig(wordlist_path=str(mine)),
        config_dir=tmp_path,
    )

    assert guard.hit("说了自定义词") == "自定义词"
    guard.reset()
    assert guard.hit("说了不该用到的词") is None, "the shipped list was loaded instead"


# ------------------------------------------------------------ text_blocked


def test_text_blocked_answers_for_a_whole_string() -> None:
    """The distiller's entry point: no streaming, one verdict.

    It is handed to Distiller as `guard.text_blocked` (dev_talk.py:1250), which
    is the only reason a summary written by the side model cannot smuggle a
    banned word into memory.
    """
    guard = OutputGuard(["违禁词"], ["河北"])

    assert guard.text_blocked("这里有违禁词") is True
    assert guard.text_blocked("这里没有") is False


def test_text_blocked_honours_the_allowlist() -> None:
    """Same lists as the streaming path, or the two layers disagree about 「河北」."""
    guard = OutputGuard(["河"], ["河北"])

    assert guard.text_blocked("河北的朋友") is False
    assert guard.text_blocked("这条河很宽") is True


def test_text_blocked_leaves_the_streaming_state_alone() -> None:
    """The reason it builds a throwaway guard instead of calling hit().

    The distiller runs beside a live reply, and both share one OutputGuard
    instance. If a whole-text check consumed the streaming tail, a banned word
    split across two deltas would stop being caught the moment a summary
    happened to run in between.
    """
    guard = OutputGuard(["违禁词"], [])
    assert guard.hit("说了违禁") is None  # half a banned word parked in the tail

    assert guard.text_blocked("完全无关的一句") is False

    assert guard.hit("词") == "违禁词", "the whole-text check ate the streaming tail"
