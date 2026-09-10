"""The marker she writes at the head of a microphone turn.

The microphone hears the whole room and the provider answers every VAD turn
on its own. The one judgement only the model can make — is the streamer
talking to ME — is reported here: ``[SKIP]`` at the very start of the reply,
nothing before it, and a note of a few words after it saying what she heard
happening. A turn that IS for her carries no marker at all. The audio is the
provider's, so whatever she writes gets spoken; a marked turn is therefore
one that never plays, and the absence of a marker is the whole "speak"
signal.

The tag is English on purpose. It survives the s2s official pipeline's
speakable-character filter (only half-width brackets and ``\\w`` do; upstream
``LLM/utils.py:18-21``), and one that leaks into a speaker sounds like a
glitch rather than a word. The Chinese label is for the panel.

No dependencies: persona (the prompt) and director (the decoder and the
gate) both import this, and persona must not reach into director.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ALIASES",
    "MARKERS",
    "Marker",
    "SceneCategory",
    "is_tag_prefix",
    "label_for",
    "lookup",
    "tag_for",
]


class SceneCategory(StrEnum):
    """What she reported about a microphone turn.

    Two values, because the gate makes one decision: play this turn or hold
    it. TO_ME has no marker — it is what a plain head means.

    It used to be five scenes (AUDIENCE, SELF_TALK, READING, GUEST, UNSURE)
    plus DECLINED. They all mapped to the same action, production used one of
    them for 91% of skips (275 of 302 on 2026-09-09), and the categories were
    not even orthogonal — reading a danmaku aloud TO the audience is both
    READING and AUDIENCE. Making her choose among them before speaking spent
    attention on the judgement that measurement showed to be the weak one.
    The scene now lives in the note, in her own words.
    """

    TO_ME = "to_me"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Marker:
    """One tag she may write, and the words the prompt and panel use for it."""

    tag: str
    category: SceneCategory
    label: str
    meaning: str


MARKERS: tuple[Marker, ...] = (Marker("SKIP", SceneCategory.SKIP, "先听", "这一轮不接话"),)

# Spellings that also mean "not for me". The five retired scene tags are here
# rather than deleted: a session opened under the old contract may still be
# running, a persona file may carry the old wording, and a model that invents
# SELF_TALK on its own is telling us exactly what SKIP means. Recognised only
# inside brackets, where the intent is unambiguous.
ALIASES: dict[str, SceneCategory] = {
    "AUDIENCE": SceneCategory.SKIP,
    "SELF_TALK": SceneCategory.SKIP,
    "READING": SceneCategory.SKIP,
    "GUEST": SceneCategory.SKIP,
    "UNSURE": SceneCategory.SKIP,
    "DECLINED": SceneCategory.SKIP,
    "略": SceneCategory.SKIP,
    "PASS": SceneCategory.SKIP,
}

_BY_KEY: dict[str, SceneCategory] = {
    **{m.tag.replace("_", ""): m.category for m in MARKERS},
    **{alias.replace("_", ""): category for alias, category in ALIASES.items()},
}
_BY_CATEGORY: dict[SceneCategory, Marker] = {m.category: m for m in MARKERS}


def _key(word: str) -> str:
    """Case-insensitive, underscore-optional: ``[selftalk]`` is ``[SELF_TALK]``."""
    return word.strip().upper().replace("_", "")


def lookup(word: str) -> SceneCategory | None:
    """The category a bracketed word names, or None for anything else."""
    return _BY_KEY.get(_key(word))


def is_tag_prefix(fragment: str) -> bool:
    """Could more characters still turn this bracketed fragment into a tag?

    What a streaming decoder asks of ``[AU`` (yes) and ``[弹`` (no).
    """
    key = _key(fragment)
    return any(candidate.startswith(key) for candidate in _BY_KEY)


def tag_for(category: SceneCategory) -> str:
    """The tag she writes for a category; TO_ME, which has none, reads as ""."""
    marker = _BY_CATEGORY.get(category)
    return marker.tag if marker is not None else ""


def label_for(category: SceneCategory) -> str:
    """The Chinese label the prompt and the panel use; 「对你说的」 for TO_ME."""
    marker = _BY_CATEGORY.get(category)
    return marker.label if marker is not None else "对你说的"
