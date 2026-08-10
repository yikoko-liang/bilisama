"""The output-side safety net, in front of anything the audience hears.

Someone will try to make the co-host say a career-ending thing on stream —
plan section 4.5 treats that as a certainty, not a possibility. Prompt-side
wrapping mitigates; this is the backstop on the way OUT: a hit means the
sentence does not go, and when the hit lands mid-stream the scheduler claws
back what already played.

Matching is deliberately dumb — substring against a word list, an allowlist to
spare false positives. Clever matching belongs to a moderation model someday;
a backstop must be predictable.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["OutputGuard"]


class OutputGuard:
    """Stateful across one reply: deltas arrive in pieces, and a banned word
    split across two chunks must still hit."""

    def __init__(self, wordlist: Iterable[str], allowlist: Iterable[str] = ()) -> None:
        self._words = [w for w in wordlist if w]
        self._allow = [w for w in allowlist if w]
        # Longest banned word bounds how much tail we must remember.
        self._keep = max((len(w) for w in self._words), default=0)
        self._tail = ""

    def reset(self) -> None:
        """Call between replies; leftovers from the last one must not haunt."""
        self._tail = ""

    def hit(self, delta: str) -> str | None:
        """Feed one delta; return the banned word it completed, if any."""
        window = self._tail + delta
        for word in self._words:
            at = window.find(word)
            while at != -1:
                if not self._allowed_at(window, at, word):
                    return word
                at = window.find(word, at + 1)
        self._tail = window[-self._keep :] if self._keep else ""
        return None

    def _allowed_at(self, window: str, at: int, word: str) -> bool:
        """A hit inside an allowlisted phrase is not a hit — 「河北」 must not
        trip on a ban of 「河」-something."""
        return any(
            window.find(phrase, max(0, at - len(phrase)), at + len(word) + len(phrase)) != -1
            for phrase in self._allow
            if word in phrase
        )
