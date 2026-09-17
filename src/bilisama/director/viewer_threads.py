"""Viewer-to-viewer addressing facts for the danmaku lane.

Two facts and no semantics. On 2026-09-15 she answered 「@白团 你那个按钮是不是
也越修越歪？」 and then 白团's 「哈哈哈哈哈哈笑死」 that followed it (hard-11
s1/s2): the selector admits every danmaku by identity only (selector.py
docstring), so the model was the sole judge and it missed twice. This
module gives the harness the two facts a person uses to see a thread:

- ``viewer_chat_target``: the danmaku opens with @someone who is neither the
  host nor her, and its body never turns back to the host, her or the room.
  A typed @ is a convention, not platform metadata, so it is a WEAK fact and
  only the unambiguous shape counts; ``LiveEvent.reply_to_*`` (the platform's
  reply target) is the HARD fact and wins when present. The assembly keeps a
  matching danmaku out of the reply lane; memory and the shared observations
  still see it.
- ``ViewerThreads``: who @'d whom, so a viewer who writes back a moment after
  being @'d can be annotated for the model (「6 秒前被观众 小路 @过」). This
  one only annotates — a reply to a mention is likelier to be chat, not
  certain, and the model reads the note with the line.
"""

from __future__ import annotations

from collections import OrderedDict

from bilisama.clock import Clock
from bilisama.ingest.events import EventKind, LiveEvent

__all__ = ["ViewerThreads", "leading_mention", "turns_to_room", "viewer_chat_target"]

_AT = "@＠"
# What ends a typed nickname: whitespace, or the punctuation people put after
# an @ when they do not leave a space.
_NAME_STOPS = " \t\r\n　，,。：:！!？?、；;（）()「」[]"
# bilibili nicknames are at most 16 characters.
_NAME_MAX = 16
# Generic words that address the host or her whatever the configured names.
_HOST_WORDS = ("主播", "up主", "up")
_HER_WORDS = ("助手", "伴播")
# Words that turn a danmaku back to the host, her, or the whole room. Matched
# as substrings of the body: 「主播你看」 and 「大家觉得」 are what they look like.
_ROOM_TERMS = ("主播", "大家", "各位", "全场", "直播间", "家人们", "老铁们", "朋友们")
_MENTIONS_KEPT = 256
_DEFAULT_WINDOW_S = 90.0


def leading_mention(text: str) -> tuple[str, str] | None:
    """The @name a danmaku opens with, and the body after it.

    None when the text does not start with @, the name is empty or longer
    than a nickname can be, or nothing separates the name from what follows
    (「@白团你那个按钮呢」: where the name ends is anyone's guess, so it is
    left to the model).
    """
    stripped = text.lstrip(" \t\r\n　")
    if not stripped or stripped[0] not in _AT:
        return None
    rest = stripped[1:]
    end = 0
    while end < len(rest) and rest[end] not in _NAME_STOPS:
        end += 1
    name = rest[:end]
    if not name or len(name) > _NAME_MAX or end == len(rest):
        return None
    body = rest[end:].lstrip(_NAME_STOPS)
    return name, body


def _names_match(name: str, candidates: tuple[str, ...]) -> bool:
    """Case-insensitive, and either side may be the other's substring so that
    「@土豆」 matches the host 「AI代码侠土豆」 — one character is too little to
    match on."""
    key = name.casefold()
    if not key:
        return False
    for raw in candidates:
        candidate = raw.strip().casefold()
        if not candidate:
            continue
        if key == candidate:
            return True
        if len(key) >= 2 and len(candidate) >= 2 and (key in candidate or candidate in key):
            return True
    return False


def _turns_to_room(body: str, *, host_names: tuple[str, ...], her_names: tuple[str, ...]) -> bool:
    lowered = body.casefold()
    if any(term in lowered for term in _ROOM_TERMS):
        return True
    return any(
        name.strip() and len(name.strip()) >= 2 and name.strip().casefold() in lowered
        for name in (*host_names, *her_names)
    )


def viewer_chat_target(
    event: LiveEvent, *, host_names: tuple[str, ...], assistant_names: tuple[str, ...]
) -> str | None:
    """The other viewer this danmaku is addressed to, or None.

    None also for every ambiguous shape — the host or her as the target, a
    body that turns back to the host, her or the room, no leading @ — so
    that what this returns is safe to act on without a model.
    """
    if event.kind is not EventKind.DANMAKU or event.viewer.is_anchor:
        return None
    body = event.text
    target = ""
    if event.reply_to_anchor is False and event.reply_to_name.strip():
        target = event.reply_to_name.strip()
        mention = leading_mention(event.text)
        if mention is not None:
            body = mention[1]
    elif event.reply_to_anchor is True:
        return None
    else:
        mention = leading_mention(event.text)
        if mention is None:
            return None
        target, body = mention
        if _names_match(target, (*host_names, *_HOST_WORDS)) or _names_match(
            target, (*assistant_names, *_HER_WORDS)
        ):
            return None
    if _turns_to_room(body, host_names=host_names, her_names=assistant_names):
        return None
    return target


def turns_to_room(
    text: str, *, host_names: tuple[str, ...], assistant_names: tuple[str, ...]
) -> bool:
    """Whether a danmaku's words reach past one viewer: the host, her or the
    whole room is named. A viewer written back to after being @'d is chat
    unless this says otherwise (2026-09-17)."""
    return _turns_to_room(text, host_names=host_names, her_names=assistant_names)


class ViewerThreads:
    """Who @'d whom lately, by nickname, on the injected clock."""

    def __init__(self, clock: Clock, *, window_s: float = _DEFAULT_WINDOW_S) -> None:
        self._clock = clock
        self._window_s = window_s
        # nickname (casefolded) → (who @'d them, their identity, when)
        self._mentions: OrderedDict[str, tuple[str, str, float]] = OrderedDict()

    def reset(self) -> None:
        self._mentions.clear()

    def note(self, event: LiveEvent, *, target: str | None) -> None:
        """Record that ``event``'s sender @'d ``target`` (a viewer). Anchor
        messages and mentions of the host or her (target None) open nothing."""
        if target is None or event.viewer.is_anchor:
            return
        key = target.casefold()
        self._mentions.pop(key, None)
        self._mentions[key] = (
            event.viewer.name or event.viewer.identity,
            event.viewer.identity,
            self._clock.monotonic(),
        )
        while len(self._mentions) > _MENTIONS_KEPT:
            self._mentions.popitem(last=False)

    def reply_context(self, event: LiveEvent) -> tuple[str, int] | None:
        """(who @'d this sender, seconds ago) when that happened inside the
        window and it was somebody else; None otherwise."""
        if event.kind is not EventKind.DANMAKU or event.viewer.is_anchor:
            return None
        key = (event.viewer.name or "").strip().casefold()
        if not key:
            return None
        found = self._mentions.get(key)
        if found is None:
            return None
        by_name, by_identity, at = found
        if by_identity == event.viewer.identity:
            return None
        ago = self._clock.monotonic() - at
        if ago < 0 or ago > self._window_s:
            return None
        return by_name, int(ago)
