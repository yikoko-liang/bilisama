"""The output-side safety net, in front of anything the audience hears.

Someone will try to make the co-host say a career-ending thing on stream —
plan section 4.5 treats that as a certainty, not a possibility. Prompt-side
wrapping mitigates; this is the backstop on the way OUT: a hit means the
sentence does not go, and when the hit lands mid-stream the scheduler claws
back what already played.

Matching is deliberately dumb — substring against a word list, an allowlist to
spare false positives. Clever matching belongs to a moderation model someday;
a backstop must be predictable.

One known limit, recorded rather than hidden: a banned word whose allowlist
verdict is still pending when the reply ends is dropped unjudged — by then the
audio is out anyway, and stage 4's sentence chunker is the layer that can hold
text back before synthesis.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bilisama.config.schema import SafetyConfig

__all__ = ["OutputGuard", "load_guard"]


class OutputGuard:
    """Stateful across one reply: deltas arrive in pieces, and a banned word
    split across two chunks must still hit. Call reset() between replies —
    the scheduler does it on dispatch — or last reply's tail haunts this one."""

    def __init__(self, wordlist: Iterable[str], allowlist: Iterable[str] = ()) -> None:
        self._words = [w for w in wordlist if w]
        self._allow = [w for w in allowlist if w]
        # The tail must be able to hold a banned word OR an allowlist phrase
        # still completing across the boundary — whichever is longer.
        self._keep = max(
            (len(w) for w in (*self._words, *self._allow)),
            default=0,
        )
        self._tail = ""

    def reset(self) -> None:
        """Call between replies; leftovers from the last one must not haunt."""
        self._tail = ""

    def hit(self, delta: str) -> str | None:
        """Feed one delta; return the banned word it completed, if any.

        A hit that might still be inside an allowlisted phrase extending past
        the window's end is HELD: judgement waits for the next delta instead of
        killing 「河北」 because 「北」 has not arrived yet (A7).
        """
        window = self._tail + delta
        for word in self._words:
            at = window.find(word)
            while at != -1:
                if self._allowed_at(window, at, word):
                    at = window.find(word, at + 1)
                    continue
                if self._could_still_allow(window, at, word):
                    # Verdict pending: remember from just before the hit so the
                    # next delta re-adjudicates with more context.
                    self._tail = window[max(0, at - self._keep) :]
                    return None
                self._tail = ""
                return word
        self._tail = window[-self._keep :] if self._keep else ""
        return None

    def text_blocked(self, text: str) -> bool:
        """Whole-text check for non-streaming callers (the distiller): same
        lists, throwaway state."""
        return OutputGuard(self._words, self._allow).hit(text) is not None

    def _allowed_at(self, window: str, at: int, word: str) -> bool:
        """A hit inside an allowlisted phrase is not a hit — 「河北」 must not
        trip on a ban of 「河」-something."""
        return any(
            window.find(phrase, max(0, at - len(phrase)), at + len(word) + len(phrase)) != -1
            for phrase in self._allow
            if word in phrase
        )

    def _could_still_allow(self, window: str, at: int, word: str) -> bool:
        """Could an allowlisted phrase containing this hit still complete once
        more text arrives? True defers the verdict to the next delta."""
        for phrase in self._allow:
            offset = phrase.find(word)
            while offset != -1:
                start = at - offset
                if start >= 0:
                    visible = window[start:]
                    if len(visible) < len(phrase) and phrase.startswith(visible):
                        return True
                offset = phrase.find(word, offset + 1)
        return False


def _read_list(path: Path) -> list[str]:
    """One entry per line; blanks and `#` comments skipped."""
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def resolve_safety_path(raw: str, *, config_dir: Path, default_name: str) -> Path:
    """ "auto" means the shipped list under config/safety/."""
    if raw == "auto":
        return config_dir / "safety" / default_name
    return Path(raw).expanduser()


def load_guard(cfg: SafetyConfig, *, config_dir: Path) -> OutputGuard:
    """Build the guard from [safety] — the wiring that was missing (D1).

    Raises:
        FileNotFoundError: wordlist file missing. Plan section 7.6 makes a
            missing wordlist a refuse-to-start condition; validate reports it
            in Chinese before anything gets this far.
    """
    wordlist_path = resolve_safety_path(
        cfg.wordlist_path, config_dir=config_dir, default_name="wordlist.txt"
    )
    allowlist_path = resolve_safety_path(
        cfg.allowlist_path, config_dir=config_dir, default_name="allowlist.txt"
    )
    if not wordlist_path.is_file():
        raise FileNotFoundError(f"敏感词表不存在：{wordlist_path}")
    allow = _read_list(allowlist_path) if allowlist_path.is_file() else []
    return OutputGuard(_read_list(wordlist_path), allow)
