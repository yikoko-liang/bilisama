"""Decoding the head of a microphone turn, and the policy that acts on it.

Two halves, both pure. MarkerHead is a small state machine fed the reply's
text as it streams: it settles the moment the head can be read — a plain
first character, a closed bracket, a fragment no tag could grow out of — and
never later than a fixed number of characters, so a turn is held for its
first few tokens and no longer. TurnPolicy maps a ruling to SPEAK or SKIP and
is the only place product policy lives: which scenes she answers is a table
here, not a sentence in the prompt.

No asyncio and no link types: the gate (voice_turn.py) owns time and frames,
the scheduler owns the kill, and this module can be tested with strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bilisama.director.intents import neutralize_tags
from bilisama.scene_markers import MARKERS as _MARKERS
from bilisama.scene_markers import (
    SceneCategory,
    is_tag_prefix,
    label_for,
    lookup,
    tag_for,
)

__all__ = [
    "NOTE_MAX_CHARS",
    "HeadState",
    "MarkerHead",
    "Ruling",
    "TurnAction",
    "TurnPolicy",
    "scene_note",
]

# Longer than the longest spelling we still recognise with its brackets — the
# retired ``[SELF_TALK]``, 11, kept as an alias — and no longer: a head still
# undecided past this is prose that happens to start with a bracket, and
# holding it any further only delays her.
_HEAD_MAX_CHARS = 12
# What the note keeps: one line, this many characters. It is material for the
# dialogue ring and the panel, not a transcript. Public because the gate
# stops waiting for more note once it has this much.
NOTE_MAX_CHARS = 20
# Leading characters the decoder looks past before reading the head.
_LEAD = "﻿ \t\r\n　"
# What may follow a bare (unbracketed) tag for it to count as one.
_BARE_FOLLOW = " \t\r\n，：:,、"
_QUOTES = "\"'“”‘’「」『』"
_NOTE_LEAD = "，。：:,、 \t"


class TurnAction(StrEnum):
    SPEAK = "speak"
    SKIP = "skip"


class HeadState(StrEnum):
    """What the decoder knows so far."""

    PENDING = "pending"  # keep feeding
    PLAIN = "plain"  # no marker: this turn is for her
    MARKED = "marked"  # ruling available
    DANGLING = "dangling"  # the reply ended inside an unfinished marker


@dataclass(frozen=True, slots=True)
class Ruling:
    """What she reported about the turn, decoded."""

    category: SceneCategory
    note: str = ""
    # She wrote the tag without brackets. Accepted, but worth counting: a
    # model that drops the brackets often is one the prompt is not reaching.
    unbracketed: bool = False

    @property
    def tag(self) -> str:
        return tag_for(self.category)

    @property
    def label(self) -> str:
        return label_for(self.category)

    def detail(self) -> str:
        """The panel's one-liner: ``AUDIENCE · 在聊天气``."""
        return f"{self.tag} · {self.note}" if self.note else self.tag


def scene_note(text: str, *, limit: int = NOTE_MAX_CHARS) -> str:
    """Clean the words after a tag into a short note.

    First line only, whitespace folded, quotes and the punctuation that
    follows a tag stripped, the live-events wrapper neutralised the same way
    danmaku text is, and cut to `limit` characters.
    """
    stripped = text.strip()
    first = stripped.splitlines()[0] if stripped else ""
    first = " ".join(first.split())
    first = first.strip(_QUOTES).lstrip(_NOTE_LEAD).strip(_QUOTES)
    return neutralize_tags(first)[:limit]


class MarkerHead:
    """Reads a reply's head as it streams and settles as early as it can.

    The decision and the note settle at different times, on purpose. The
    decision lands on the closing bracket, because that is when the gate must
    mute the turn. The note is the words AFTER the bracket, and a provider
    that streams character by character has not sent them yet — so `ruling`
    is recomputed from the buffer on every read and its note grows until the
    caller stops feeding. Freezing the note at decision time is what left
    production with 298 empty notes out of 302 skips (2026-09-09).
    """

    __slots__ = ("_buf", "_category", "_max_chars", "_note_from", "_state", "_unbracketed")

    def __init__(self, *, max_chars: int = _HEAD_MAX_CHARS) -> None:
        self._buf = ""
        self._max_chars = max_chars
        self._state = HeadState.PENDING
        self._category: SceneCategory | None = None
        # Where the note starts, as an index into the lead-stripped buffer.
        self._note_from = 0
        self._unbracketed = False

    @property
    def state(self) -> HeadState:
        return self._state

    @property
    def ruling(self) -> Ruling | None:
        """Valid once the state is MARKED; None otherwise.

        Rebuilt per read so the note reflects everything fed so far.
        """
        if self._category is None:
            return None
        note = scene_note(self._buf.lstrip(_LEAD)[self._note_from :])
        return Ruling(self._category, note, unbracketed=self._unbracketed)

    @property
    def text(self) -> str:
        return self._buf

    def feed(self, chunk: str) -> HeadState:
        """Add streamed text. Once settled, further text only accumulates."""
        self._buf += chunk
        if self._state is HeadState.PENDING:
            self._state = self._decide(final=False)
        return self._state

    def finish(self) -> HeadState:
        """The reply ended: settle with what there is."""
        if self._state is HeadState.PENDING:
            self._state = self._decide(final=True)
        return self._state

    def _decide(self, *, final: bool) -> HeadState:
        buf = self._buf.lstrip(_LEAD)
        if not buf:
            return HeadState.PLAIN if final else HeadState.PENDING
        state = self._bracketed(buf) if buf[0] == "[" else self._bare(buf, final=final)
        if state is HeadState.PENDING:
            if final:
                # Only an open bracket is worth calling dangling: bare text
                # that merely looked like the start of a tag is prose.
                return HeadState.DANGLING if buf[0] == "[" else HeadState.PLAIN
            if len(buf) > self._max_chars:
                return HeadState.PLAIN
        return state

    def _bracketed(self, buf: str) -> HeadState:
        close = buf.find("]")
        if close == -1:
            return HeadState.PENDING if is_tag_prefix(buf[1:]) else HeadState.PLAIN
        category = lookup(buf[1:close])
        if category is None:
            return HeadState.PLAIN
        self._category = category
        self._note_from = close + 1
        return HeadState.MARKED

    def _bare(self, buf: str, *, final: bool) -> HeadState:
        # Without brackets the spelling has to be exact: the tag as taught,
        # upper case, then a separator — or nothing at all, when the reply
        # ended there. ``Audience 是谁`` is a sentence.
        for marker in _MARKERS:
            tag = marker.tag
            if buf.startswith(tag):
                rest = buf[len(tag) :]
                if not rest and not final:
                    return HeadState.PENDING
                if rest and rest[0] not in _BARE_FOLLOW:
                    return HeadState.PLAIN
                self._category = marker.category
                self._note_from = len(tag)
                self._unbracketed = True
                return HeadState.MARKED
            if tag.startswith(buf):
                return HeadState.PENDING
        return HeadState.PLAIN


@dataclass(frozen=True, slots=True)
class TurnPolicy:
    """Which scenes she answers. Product policy, and nothing but a table."""

    speak: frozenset[SceneCategory] = frozenset({SceneCategory.TO_ME})

    def action(self, ruling: Ruling | None) -> TurnAction:
        """A plain head is TO_ME — no marker is the whole speak signal."""
        category = SceneCategory.TO_ME if ruling is None else ruling.category
        return TurnAction.SPEAK if category in self.speak else TurnAction.SKIP
