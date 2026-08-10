"""Live event model.

This module owns the event taxonomy. Config keys, the speak switches in the UI
protocol and the fixture filenames all reference `EventKind` — nobody gets to
keep a private copy.

One rule matters more than the rest: **never drop an event because uid is 0.**
Bilibili masks uid for privacy, and a masked viewer still has a stable per-room
identity in uid_hash. N.E.K.O drops those events outright
(neko_live/modules/live_events/module.py:238, `if not uid or uid == "0": return`),
which silences the entire danmaku stream the moment masking kicks in.

That is also why a logged-in path matters from day one. Anonymous connections
work, but every viewer arrives as uid 0 named `***`, and per-viewer memory,
name-checking and per-uid cooldowns are most of what makes a co-host feel present.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """The event taxonomy. Adding one means updating the UI protocol and the
    config schema too — there is a test that fails if they drift apart."""

    DANMAKU = "danmaku"
    GIFT = "gift"
    SUPER_CHAT = "super_chat"
    GUARD_BUY = "guard_buy"
    VIP_ENTER = "vip_enter"  # member, top-spender or past gifter walking in
    ENTRY = "entry"  # ordinary arrival, high volume
    FOLLOW = "follow"
    LIKE = "like"
    SHARE = "share"
    ROOM_STATE = "room_state"


class GuardLevel(StrEnum):
    """Membership tier.

    Deliberately not an IntEnum: on the wire, smaller means more expensive, and
    an integer sitting next to a bunch of scores invites someone to compare them.
    """

    NONE = "none"
    GOVERNOR = "governor"  # 总督, the most expensive tier
    ADMIRAL = "admiral"  # 提督
    CAPTAIN = "captain"  # 舰长, the entry tier and by far the most common

    @classmethod
    def from_wire(cls, value: int) -> GuardLevel:
        return {1: cls.GOVERNOR, 2: cls.ADMIRAL, 3: cls.CAPTAIN}.get(value, cls.NONE)

    @property
    def is_patron(self) -> bool:
        return self is not GuardLevel.NONE


@dataclass(frozen=True, slots=True)
class Medal:
    name: str = ""
    level: int = 0
    up_name: str = ""
    anchor_room_id: int = 0

    def is_this_room(self, room_id: int) -> bool:
        """Whether the medal belongs to THIS room — it must be told which.

        The old property answered "has any medal at all" (D13): a viewer
        wearing another streamer's badge counted as a local fan.
        """
        return bool(self.name) and room_id > 0 and self.anchor_room_id == room_id


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who sent an event.

    uid == 0 does not mean "no identity", it means "the platform masked it". Use
    `identity`, which falls back to uid_hash and never returns an empty key, so
    callers never have to special-case masking.
    """

    uid: int = 0
    uid_hash: str = ""  # stable per-room id, the only handle we get when uid is masked
    name: str = ""  # may literally be "***" when masked
    face_url: str = ""
    user_level: int = 0
    wealth_level: int = 0
    guard_level: GuardLevel = GuardLevel.NONE
    is_admin: bool = False
    medal: Medal | None = None

    @property
    def is_anonymous(self) -> bool:
        return self.uid == 0

    @property
    def identity(self) -> str:
        """Key used for dedup and memory. Never empty."""
        if self.uid:
            return f"uid:{self.uid}"
        if self.uid_hash:
            return f"hash:{self.uid_hash}"
        return "anon"

    @property
    def display_name(self) -> str:
        return self.name or "一位观众"


@dataclass(frozen=True, slots=True)
class Gift:
    gift_id: int = 0
    name: str = ""
    num: int = 1
    coin_type: str = ""  # gold | silver | ""; only gold is real money
    total_coin: int = 0  # 1000 gold == CNY 1
    combo_id: str = ""
    combo_count: int = 0
    combo_end: bool | None = None
    aggregated_count: int = 1  # >1 once several small gifts were merged into one

    @property
    def is_paid(self) -> bool:
        return self.coin_type == "gold" and self.total_coin > 0


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """One shape for every kind of live event.

    `raw` is for debugging only and must never reach an LLM prompt — it is the
    unsanitised platform payload. Call `redacted()` before anything that flows
    toward the model.
    """

    kind: EventKind
    room_id: int = 0  # the real room id, not the short vanity one
    viewer: Viewer = field(default_factory=Viewer)
    text: str = ""  # danmaku or super chat body; empty for gifts
    gift: Gift | None = None
    value_cny: float = 0.0  # one currency for every paid event, so ranking is easy
    event_id: str = ""  # primary dedup key when the platform gives us one
    ts_ms: int = 0  # platform timestamp
    recv_at: float = 0.0  # our monotonic clock
    session_generation: int = 0  # bumped on reconnect so late events can be dropped
    raw: dict[str, Any] | None = None

    @property
    def is_anonymous(self) -> bool:
        return self.viewer.is_anonymous

    @property
    def dedup_key(self) -> str:
        """Dedup key.

        Falls back to identity plus content plus a one-second bucket when the
        platform gives us no id, which is what stops a reconnect from replaying
        the same reaction.
        """
        if self.event_id:
            return f"{self.kind}:{self.event_id}"
        return f"{self.kind}:{self.viewer.identity}:{self.text[:32]}:{self.ts_ms // 1000}"

    @property
    def is_paid(self) -> bool:
        return self.value_cny > 0

    def redacted(self) -> LiveEvent:
        """A copy with `raw` stripped. Run it before anything model-facing.

        Uses `replace` rather than listing fields by hand: forget one after adding
        a field and it silently reverts to its default, with no error, on the exact
        path that feeds the prompt.
        """
        if self.raw is None:
            return self
        return dataclasses.replace(self, raw=None)


def cny_from_gold(total_coin: int) -> float:
    """Convert gold coins to CNY. 1000 gold == CNY 1."""
    return total_coin / 1000.0


def is_vip_entry(viewer: Viewer, *, lifetime_gift_cny: float = 0.0) -> bool:
    """Whether this arrival deserves a greeting by name.

    Members and anyone who has spent money before go into the paid lane; ordinary
    arrivals are high-volume and stay silent by default, surfacing only in the
    batched welcome.

    Args:
        viewer: The person who just walked in.
        lifetime_gift_cny: What they have spent across all past streams, looked up
            from memory.

    Returns:
        True when they are worth greeting individually.
    """
    return viewer.guard_level.is_patron or lifetime_gift_cny > 0
