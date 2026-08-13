"""Danmaku scoring: which message in the window is worth answering.

A port of N.E.K.O's get_score ordering (livedanmaku.py:477 — guard > admin >
medal > user level > text length), renormalised to 0..1 because derive.py's
score_threshold lives on that scale (0.55 / 0.35 / 0.2 by chattiness).
Two adaptations beyond the port, both stated here on purpose:

- Text substance discounts repetition: "哈哈哈哈哈哈哈哈" is length 8 and
  substance 2, so spam cannot buy its way over the threshold by length —
  N.E.K.O used raw length and its thresholds absorbed the difference.
- Questions get a flat bonus. A question is the most answerable danmaku a
  co-host can pick, and nothing else in the port distinguishes "asked us
  something" from "said something".

Calibration against the thresholds (worked examples, pinned by tests):
a bare "666" scores ~0.07 and never speaks at any chattiness; a plain
viewer asking a real question lands ~0.4 — over MEDIUM, under LOW; a
captain saying almost anything clears MEDIUM, and with a real sentence
clears LOW.
"""

from __future__ import annotations

from bilisama.ingest.events import GuardLevel, LiveEvent

__all__ = ["danmaku_score"]

_GUARD_BONUS: dict[GuardLevel, float] = {
    GuardLevel.GOVERNOR: 0.4,
    GuardLevel.ADMIRAL: 0.35,
    GuardLevel.CAPTAIN: 0.3,
}
_ADMIN_BONUS = 0.15
# Platform medal levels stop at 40; cap under the admin bonus to keep
# N.E.K.O's ordering (admin 500 > medal max 400) intact after scaling.
_MEDAL_SCALE = 0.14 / 40
_USER_LEVEL_SCALE = 0.1 / 50
_TEXT_WEIGHT = 0.4
_TEXT_FULL_AT = 12  # chars of substance for the full text weight
_QUESTION_BONUS = 0.15
_QUESTION_MARKS = ("?", "？")
_QUESTION_WORDS = ("吗", "呢", "什么", "怎么", "为什么", "为啥", "咋", "多少", "哪", "谁", "几点")


def _substance(text: str) -> int:
    """Length with repetition discounted: at most twice the distinct chars."""
    stripped = "".join(text.split())
    return min(len(stripped), 2 * len(set(stripped)))


def _looks_like_question(text: str) -> bool:
    return any(m in text for m in _QUESTION_MARKS) or any(w in text for w in _QUESTION_WORDS)


def danmaku_score(event: LiveEvent) -> float:
    """Score one danmaku on 0..1 against derive.py's score_threshold scale.

    Args:
        event: A DANMAKU event. Other kinds are not scored — paid kinds
            bypass the window entirely and free gifts carry no text.

    Returns:
        0.0..1.0; bigger means more worth answering.
    """
    score = min(_substance(event.text), _TEXT_FULL_AT) / _TEXT_FULL_AT * _TEXT_WEIGHT
    if _looks_like_question(event.text):
        score += _QUESTION_BONUS
    viewer = event.viewer
    score += _GUARD_BONUS.get(viewer.guard_level, 0.0)
    if viewer.is_admin:
        score += _ADMIN_BONUS
    if viewer.medal is not None and viewer.medal.is_this_room(event.room_id):
        score += min(viewer.medal.level, 40) * _MEDAL_SCALE
    score += min(viewer.user_level, 50) * _USER_LEVEL_SCALE
    return min(score, 1.0)
