"""The scene markers she writes at the head of a microphone turn.

The microphone hears the whole room and the provider answers every VAD turn
on its own. The one judgement only the model can make — who the streamer was
talking to — is reported here: a half-width bracket tag at the very start of
the reply, nothing before it, and a note of a few words after it. A turn
that IS for her carries no marker at all. The audio is the provider's, so
whatever she writes gets spoken; a marked turn is therefore one that never
plays, and the absence of a marker is the whole "speak" signal.

The tags are the English category names on purpose. They survive the s2s
official pipeline's speakable-character filter (only half-width brackets and
``\\w`` do; upstream ``LLM/utils.py:18-21``), they map onto SceneCategory with
no translation table, and one that leaks into a speaker sounds like a glitch
rather than a word. The Chinese labels are for the prompt that teaches the
tags and for the panel that shows the rulings.

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
    """Who the streamer was talking to, as she heard it.

    TO_ME has no marker — it is what a plain head means. DECLINED is the odd
    one out: not a scene at all, but her answer to a turn WE asked for (the
    phase-two probes), meaning she found nothing worth saying.
    """

    TO_ME = "to_me"
    AUDIENCE = "audience"
    SELF_TALK = "self_talk"
    READING = "reading"
    GUEST = "guest"
    UNSURE = "unsure"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class Marker:
    """One tag she may write, and the words the prompt and panel use for it."""

    tag: str
    category: SceneCategory
    label: str
    meaning: str


MARKERS: tuple[Marker, ...] = (
    Marker("AUDIENCE", SceneCategory.AUDIENCE, "对观众", "主播在对观众讲话"),
    Marker("SELF_TALK", SceneCategory.SELF_TALK, "自语", "主播在自言自语"),
    Marker("READING", SceneCategory.READING, "念弹幕", "主播在念弹幕、念礼物或谢礼物"),
    Marker("GUEST", SceneCategory.GUEST, "连麦", "主播在跟连麦的人或旁边的人说话"),
    Marker("UNSURE", SceneCategory.UNSURE, "不确定", "听不出主播在跟谁说"),
    Marker("DECLINED", SceneCategory.DECLINED, "不说", "被邀请开口，但没有值得说的"),
)

# Spellings a model invents for "not for me". Folded to UNSURE rather than
# taught, and only recognised inside brackets, where the intent is unambiguous.
ALIASES: dict[str, SceneCategory] = {
    "略": SceneCategory.UNSURE,
    "SKIP": SceneCategory.UNSURE,
    "PASS": SceneCategory.UNSURE,
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
