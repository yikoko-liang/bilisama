"""Local danmaku value checks: cheap rules first, numeric score second.

A port of N.E.K.O's get_score ordering (livedanmaku.py:477 — guard > admin >
medal > user level > text length), renormalised to 0..1 because the score
threshold lives on that scale (0.50 / 0.30 / 0.15 by chattiness, nudged by
room load). Two adaptations beyond the port, both stated here on purpose:

- Text substance discounts repetition: "哈哈哈哈哈哈哈哈" is length 8 and
  substance 2, so spam cannot buy its way over the threshold by length —
  N.E.K.O used raw length and its thresholds absorbed the difference.
- Questions get a flat bonus. A question is the most answerable danmaku a
  co-host can pick, and nothing else in the port distinguishes "asked us
  something" from "said something".

Ahead of the score sits a three-state text signal (danmaku_text_signal):
obvious spam is rejected before it can open a window, and the shapes a
co-host must not miss — questions, name-checks, corrections, fault reports,
requests, anything touching what is being talked about — pass the bar
regardless of the sender's badges. The score then only ranks what survived.

Calibration against the thresholds (worked examples, pinned by tests):
a bare "666" scores ~0.07 and never speaks at any chattiness; a plain
viewer asking a real question lands ~0.4 — over MEDIUM, under LOW; a
captain saying almost anything clears MEDIUM, and with a real sentence
clears LOW.
"""

from __future__ import annotations

import re
from enum import StrEnum

from bilisama.ingest.events import GuardLevel, LiveEvent
from bilisama.obs.logging import get_logger

__all__ = [
    "TextSignal",
    "danmaku_content_key",
    "danmaku_score",
    "danmaku_text_signal",
    "is_near_duplicate",
]

log = get_logger(__name__)

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
_CORRECTION_CUES = ("不对", "不是", "应该", "说错", "搞错", "有问题")
_FAULT_CUES = ("没声音", "听不到", "卡了", "卡住", "断了", "黑屏", "没画面", "延迟")
_REQUEST_CUES = ("试试", "能不能", "可以讲", "建议", "讲一下", "看一下", "解释一下", "帮忙")
# Generic role words only. The assistant's and the streamer's actual names are
# per-config and arrive through mention_terms — a shipped persona name here
# would survive a rename and keep matching the wrong assistant.
_DEFAULT_MENTIONS = ("助手", "伴播", "主播", "up")
_LOW_INFORMATION = frozenset(
    {
        "好",
        "好的",
        "来了",
        "哈哈",
        "哈哈哈",
        "呵呵",
        "666",
        "6",
        "支持",
        "路过",
    }
)
_CONTEXT_STOP_TERMS = frozenset(
    {
        "这个",
        "那个",
        "就是",
        "一下",
        "现在",
        "今天",
        "主播",
        "可以",
        "怎么",
        "什么",
        "我们",
        "你们",
        "他们",
    }
)
_CONTENT_CHARS = re.compile(r"[0-9a-z\u4e00-\u9fff]", re.IGNORECASE)
_ASCII_TERM = re.compile(r"[a-z0-9]{3,}", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


class TextSignal(StrEnum):
    """First-stage verdict before identity weighting and score comparison."""

    HARD_ACCEPT = "hard_accept"
    REJECT = "reject"
    SCORE = "score"


def _substance(text: str) -> int:
    """Length with repetition discounted: at most twice the distinct chars."""
    stripped = "".join(text.split())
    return min(len(stripped), 2 * len(set(stripped)))


def _looks_like_question(text: str) -> bool:
    return any(m in text for m in _QUESTION_MARKS) or any(w in text for w in _QUESTION_WORDS)


def danmaku_content_key(text: str) -> str:
    """Normalised content used for cheap low-information and repetition checks."""
    return "".join(_CONTENT_CHARS.findall(text.casefold()))


def _meaningful_terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = set(_ASCII_TERM.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return {term for term in terms if term not in _CONTEXT_STOP_TERMS}


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    for raw in (*_DEFAULT_MENTIONS, *names):
        name = raw.strip().casefold()
        if not name:
            continue
        if name.isascii():
            # Word boundaries for ASCII names: "ming" must not fire inside
            # "gaming". CJK has no such boundary, so substring is right there.
            if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered):
                return True
        elif name in lowered:
            return True
    return False


def _context_related(text: str, lines: tuple[str, ...]) -> bool:
    terms = _meaningful_terms(text)
    if not terms:
        return False
    context_terms: set[str] = set()
    for line in lines:
        context_terms.update(_meaningful_terms(line))
    return bool(terms & context_terms)


def danmaku_text_signal(
    text: str,
    *,
    context_lines: tuple[str, ...] = (),
    mention_terms: tuple[str, ...] = (),
) -> TextSignal:
    """Classify obvious value without any network or model call.

    REJECT is spam no badge can save: empty content, bare digits, the
    low-information set, or heavy character repetition. HARD_ACCEPT bypasses
    the score bar for the shapes a co-host must not miss — questions,
    name-checks, corrections, fault reports, requests, and anything sharing a
    term with the stream's current context. Everything else goes to SCORE.
    """
    key = danmaku_content_key(text)
    if not key or key.isdigit() or key in _LOW_INFORMATION:
        return TextSignal.REJECT
    if len(key) >= 4 and len(set(key)) / len(key) <= 0.34:
        return TextSignal.REJECT
    if (
        _looks_like_question(text)
        or _mentions(text, mention_terms)
        or any(cue in text for cue in _CORRECTION_CUES)
        or any(cue in text for cue in _FAULT_CUES)
        or any(cue in text for cue in _REQUEST_CUES)
        or _context_related(text, context_lines)
    ):
        return TextSignal.HARD_ACCEPT
    return TextSignal.SCORE


def is_near_duplicate(text: str, recent_keys: tuple[str, ...]) -> bool:
    """Catch cross-viewer copy spam while preserving short conversational replies.

    Exact key hits always count; fuzzy matching (bigram Dice >= 0.86) only
    engages from six content chars up, so "为什么" and "为什么啊" stay two
    separate short questions rather than one piece of spam.
    """
    key = danmaku_content_key(text)
    if not key:
        return False
    if key in recent_keys:
        return True
    if len(key) < 6:
        return False
    grams = {key[index : index + 2] for index in range(len(key) - 1)}
    for previous in recent_keys:
        if len(previous) < 6:
            continue
        prior_grams = {previous[index : index + 2] for index in range(len(previous) - 1)}
        similarity = 2 * len(grams & prior_grams) / max(len(grams) + len(prior_grams), 1)
        if similarity >= 0.86:
            return True
    return False


def danmaku_score(event: LiveEvent) -> float:
    """Score one danmaku on 0..1 against the score_threshold scale.

    Args:
        event: A DANMAKU event. Other kinds are not scored — paid kinds
            bypass the window entirely and free gifts carry no text.

    Returns:
        0.0..1.0; bigger means more worth answering.
    """
    viewer = event.viewer
    substance = _substance(event.text)
    # Broken out rather than accumulated in place so the debug line below can
    # say WHICH term carried the score. Same terms in the same order, so the
    # float result is unchanged — the worked examples in the module docstring
    # still hold. `text`-flavoured field names are avoided on purpose: the
    # formatter scrubs by name (obs/logging.py:_VIEWER_CONTENT) and would fold
    # a float called `text_score` into `<float>`.
    substance_score = min(substance, _TEXT_FULL_AT) / _TEXT_FULL_AT * _TEXT_WEIGHT
    question_score = _QUESTION_BONUS if _looks_like_question(event.text) else 0.0
    guard_score = _GUARD_BONUS.get(viewer.guard_level, 0.0)
    admin_score = _ADMIN_BONUS if viewer.is_admin else 0.0
    medal_score = (
        min(viewer.medal.level, 40) * _MEDAL_SCALE
        if viewer.medal is not None and viewer.medal.is_this_room(event.room_id)
        else 0.0
    )
    level_score = min(viewer.user_level, 50) * _USER_LEVEL_SCALE
    score = min(
        substance_score + question_score + guard_score + admin_score + medal_score + level_score,
        1.0,
    )
    log.debug(
        "scoring.danmaku_scored",
        score=round(score, 4),
        identity=viewer.identity,
        substance_chars=substance,
        substance_score=round(substance_score, 4),
        question_score=question_score,
        guard_score=guard_score,
        admin_score=admin_score,
        medal_score=round(medal_score, 4),
        level_score=round(level_score, 4),
    )
    return score
