"""Where a proactive topic may come from, and the memory of what it came from.

Three small things the topic loop leans on (proactive.py), kept apart from
it so each can be tested on strings and a clock:

- ``Layer`` and ``allowed_layers``: the seven places a topic can be drawn
  from, ordered by how close they sit to what the room is doing right now,
  and which of them a room band permits. event_pacing's four bands say how
  OFTEN she may open; this says what she may open WITH. A busy room gets
  nothing (the pacer already closes it), an active room only the layers
  anchored in the room's own traffic, a quiet room everything down to the
  trivia pool. Modelled on N.E.K.O's activity → propensity → allowed
  sources (main_logic/activity/snapshot.py), shrunk to a live room.
- ``TopicLedger``: what she already opened with this stream — the text, the
  layer, the viewers she named — so a candidate that reheats a recent
  opening is dropped before it is spoken, the same layer is not drawn twice
  in a row while another has material, and one viewer is not the topic
  every time. Literal similarity only; the semantic judgement stays with the
  model, which also receives the ledger's recent lines.
- ``TopicPool``: the trivia floor. Light questions written by hand per
  stream type (config/prompts/topics/*.md, one per line), each handed out
  once per stream. The one layer that does not depend on the model finding
  something in the room.
"""

from __future__ import annotations

import difflib
import random
import re
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from bilisama.clock import Clock
from bilisama.event_pacing import RoomActivity
from bilisama.ingest.bilibili.scoring import danmaku_content_key
from bilisama.obs.logging import get_logger

__all__ = [
    "DEFAULT_LEDGER_WINDOW_S",
    "Duplicate",
    "Layer",
    "TopicLedger",
    "TopicPool",
    "allowed_layers",
    "layer_label",
    "topic_similarity",
]

log = get_logger(__name__)

# How long an opening stays in the ledger. An hour covers the stretch in
# which "she said that already" is what the audience remembers.
DEFAULT_LEDGER_WINDOW_S = 3600.0
_LEDGER_CAPACITY = 64
# Below this a candidate is a different topic; at or above it is the last
# one reheated. difflib on 「刚才小满问自动分类为啥砍了，我解释过…」 against the
# opening it repeats scores 0.6–0.7; two different questions score under 0.3.
DUPLICATE_THRESHOLD = 0.6


class Layer(IntEnum):
    """Where a topic is drawn from, nearest to the room first."""

    OWED = 1  # a reply the streamer talked over, never requeued
    UNANSWERED = 2  # audience danmaku nobody answered
    DISCUSSION = 3  # a discussable angle abstracted from recent danmaku
    STREAMER = 4  # picking up what the streamer just said
    STREAM = 5  # hooks in the stream's own progress or intro
    MEMORY = 6  # regulars present, shared history
    POOL = 7  # the hand-written trivia floor


_LAYER_LABELS = {
    Layer.OWED: "欠着的回复",
    Layer.UNANSWERED: "没人答的弹幕",
    Layer.DISCUSSION: "弹幕里的可讨论话题",
    Layer.STREAMER: "接主播的话",
    Layer.STREAM: "本场进展/直播简介",
    Layer.MEMORY: "在场常客与共同经历",
    Layer.POOL: "趣味池",
}


def layer_label(layer: Layer) -> str:
    return _LAYER_LABELS[layer]


_ROOM_LAYERS = frozenset({Layer.OWED, Layer.UNANSWERED, Layer.DISCUSSION, Layer.STREAMER})
_ALLOWED: dict[RoomActivity, frozenset[Layer]] = {
    RoomActivity.BUSY: frozenset(),
    RoomActivity.ACTIVE: _ROOM_LAYERS,
    RoomActivity.SPARSE: _ROOM_LAYERS | {Layer.STREAM},
    RoomActivity.QUIET: frozenset(Layer),
}


def allowed_layers(activity: RoomActivity | None) -> frozenset[Layer]:
    """Which layers a room band permits; no pacer reads as a quiet room."""
    if activity is None:
        return frozenset(Layer)
    return _ALLOWED[activity]


# ------------------------------------------------------------ similarity


def _bigrams(key: str) -> set[str]:
    return {key[i : i + 2] for i in range(len(key) - 1)} if len(key) >= 2 else set()


def topic_similarity(a: str, b: str) -> float:
    """0..1, the larger of character-bigram Dice and difflib's ratio on the
    content characters (punctuation, spaces and case folded away)."""
    ka, kb = danmaku_content_key(a), danmaku_content_key(b)
    if not ka or not kb:
        return 0.0
    ga, gb = _bigrams(ka), _bigrams(kb)
    dice = 2 * len(ga & gb) / (len(ga) + len(gb)) if ga and gb else 0.0
    ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
    return max(dice, ratio)


@dataclass(frozen=True, slots=True)
class Duplicate:
    """The ledger entry a candidate reheats, and how closely."""

    key: str
    text: str
    score: float
    layer: Layer


@dataclass(slots=True)
class _Entry:
    key: str
    text: str
    layer: Layer
    at: float
    named: tuple[str, ...]


class TopicLedger:
    """Openings made this stream, on the injected clock."""

    def __init__(self, clock: Clock, *, window_s: float = DEFAULT_LEDGER_WINDOW_S) -> None:
        self._clock = clock
        self._window_s = window_s
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def reset(self) -> None:
        self._entries.clear()

    def _prune(self) -> None:
        now = self._clock.monotonic()
        for key, entry in tuple(self._entries.items()):
            if now - entry.at > self._window_s:
                self._entries.pop(key)
        while len(self._entries) > _LEDGER_CAPACITY:
            self._entries.popitem(last=False)

    def note(self, key: str, text: str, *, layer: Layer, named: tuple[str, ...] = ()) -> None:
        """Record one opening (the candidate at submit, or what she said)."""
        line = " ".join(text.split())[:300]
        if not line:
            return
        self._entries.pop(key, None)
        self._entries[key] = _Entry(
            key=key,
            text=line,
            layer=layer,
            at=self._clock.monotonic(),
            named=tuple(name for name in named if name),
        )
        self._prune()

    def forget(self, key: str) -> None:
        """An opening that never played is not one the audience heard."""
        self._entries.pop(key, None)

    def duplicate_of(
        self, text: str, *, threshold: float = DUPLICATE_THRESHOLD, exclude_key: str = ""
    ) -> Duplicate | None:
        self._prune()
        best: Duplicate | None = None
        for entry in self._entries.values():
            if exclude_key and entry.key == exclude_key:
                continue
            score = topic_similarity(text, entry.text)
            if score >= threshold and (best is None or score > best.score):
                best = Duplicate(key=entry.key, text=entry.text, score=score, layer=entry.layer)
        return best

    def recent_lines(self, *, limit: int = 10) -> list[str]:
        self._prune()
        return [entry.text for entry in list(self._entries.values())[-limit:]]

    @property
    def last_layer(self) -> Layer | None:
        self._prune()
        if not self._entries:
            return None
        return next(reversed(self._entries.values())).layer

    def layers_used_within(self, seconds: float) -> set[Layer]:
        self._prune()
        now = self._clock.monotonic()
        return {entry.layer for entry in self._entries.values() if now - entry.at <= seconds}

    def last_used_at(self, layer: Layer) -> float | None:
        """When an opening last drew on this layer; None inside the window."""
        self._prune()
        stamps = [entry.at for entry in self._entries.values() if entry.layer is layer]
        return max(stamps) if stamps else None

    def times_named(self, viewer_name: str) -> int:
        self._prune()
        key = viewer_name.strip().casefold()
        if not key:
            return 0
        return sum(
            1
            for entry in self._entries.values()
            if any(name.casefold() == key for name in entry.named)
        )

    def __len__(self) -> int:
        self._prune()
        return len(self._entries)


# ------------------------------------------------------------ trivia pool

_POOL_LINE_MAX = 120
_POOL_COMMENT = re.compile(r"^\s*(#|//|<!--)")


class TopicPool:
    """Hand-written light questions, one per line, each handed out once."""

    def __init__(self, lines: tuple[str, ...], *, rng: random.Random | None = None) -> None:
        self._lines = lines
        self._used: set[str] = set()
        self._rng = rng or random.Random()

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        names: Collection[str] = (),
        rng: random.Random | None = None,
    ) -> TopicPool:
        """Every non-comment, non-empty line of every ``*.md`` under ``directory``,
        or only of the files ``names`` picks (``aigc`` reads ``aigc.md``).

        A missing or unreadable directory or file is an empty pool: the trivia
        floor is a floor, never a reason not to start.
        """
        lines: list[str] = []
        seen: set[str] = set()
        wanted = {name.strip() for name in names if name.strip()}
        try:
            paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
        except OSError as exc:
            log.warning("proactive.topic_pool_unreadable", error_text=str(exc)[:200])
            paths = []
        if wanted:
            paths = [path for path in paths if path.stem in wanted]
            for name in sorted(wanted - {path.stem for path in paths}):
                log.warning("proactive.topic_pool_unreadable", error_text=f"{name}.md not found")
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                log.warning("proactive.topic_pool_unreadable", error_text=str(exc)[:200])
                continue
            for raw in text.splitlines():
                line = " ".join(raw.split())[:_POOL_LINE_MAX]
                if not line or _POOL_COMMENT.match(line) or line in seen:
                    continue
                seen.add(line)
                lines.append(line)
        return cls(tuple(lines), rng=rng)

    @property
    def size(self) -> int:
        return len(self._lines)

    def reset(self) -> None:
        self._used.clear()

    def draw(self, count: int) -> list[str]:
        """Up to ``count`` unused lines, in random order."""
        fresh = [line for line in self._lines if line not in self._used]
        self._rng.shuffle(fresh)
        return fresh[: max(0, count)]

    def mark_used(self, line: str) -> None:
        self._used.add(line)

    def status(self) -> dict[str, int]:
        return {"pool_size": len(self._lines), "pool_used": len(self._used)}
