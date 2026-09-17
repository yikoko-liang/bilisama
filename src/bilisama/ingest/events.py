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
    VIP_ENTER = "vip_enter"  # current guard or level-5+ current-room medal walking in
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
    # Mapped from DANMU_MSG only (source.py's _medal call for danmaku passes
    # runame; every other kind passes ""), so it is empty on most events. The
    # panel feed shows it (ui/events.py); nothing else should lean on it.
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

    Some fields here are marked NO CONSUMER YET. They are filled by the mapper
    and read by nobody, and the marker exists because a populated field in a
    public model reads as a working feature: reach for `face_url` expecting the
    avatar pipeline behind it and there is none. The marker is the contract —
    wire a reader and delete the marker in the same change.
    """

    uid: int = 0
    uid_hash: str = ""  # stable per-room id, the only handle we get when uid is masked
    name: str = ""  # may literally be "***" when masked
    # NO CONSUMER YET — see the note on hollow fields in the class docstring.
    # This one is the easiest to misread: every mapped event fills it.
    face_url: str = ""
    user_level: int = 0  # scoring.py:74 reads this one
    wealth_level: int = 0  # DANMU_MSG fills it; only the panel feed reads it (ui/events.py)
    guard_level: GuardLevel = GuardLevel.NONE
    is_admin: bool = False
    # Set by the bilibili source when the sender is the room's own streamer
    # (uid matches room_owner_uid from the handshake). The assembly reads it to
    # turn the streamer's own danmaku into shared context instead of a reply.
    is_anchor: bool = False
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
        """The fallback wording a greeting needs, kept here so the "一位观众"
        string has one home: the entry welcome (director/intents.py) and the
        panel feed (ui/events.py) both read it."""
        return self.name or "一位观众"


@dataclass(frozen=True, slots=True)
class Gift:
    gift_id: int = 0
    name: str = ""
    num: int = 1
    coin_type: str = ""  # gold | silver | ""; only gold is real money
    total_coin: int = 0  # 1000 gold == CNY 1
    # The per-unit value a viewer actually sees in the gift panel: wire price
    # 100 == 1 battery == CNY 0.1. Product tiers speak battery, not gold.
    unit_battery: int = 0
    combo_id: str = ""
    # NO WIRE WRITER; the panel feed reads combo_count (ui/events.py), nothing
    # else does. The platform's own combo signals: only the replay fixtures
    # fill them (tests/fakes/replay.py), because a fixture
    # records what arrived. No mapper in source.py sets either one, and the
    # aggregator settles a combo on its 1.0s idle timer rather than on
    # combo_end (safety.py) — deliberately, since the last hit's combo_end can
    # go missing and an unsettled combo is an unthanked gift.
    combo_count: int = 0
    combo_end: bool | None = None
    # Written by GiftComboAggregator._aggregate and shown in the panel feed
    # (ui/events.py); the spoken thank-you that would say "连击 50 次" is not
    # built yet.
    aggregated_count: int = 1  # >1 once several small gifts were merged into one

    @property
    def is_paid(self) -> bool:
        """NO CONSUMER YET — mind the near-namesake. What routes an event into
        the paid lane is LiveEvent.is_paid (source.py's offer), which reads
        value_cny. This one answers the same question from the gift block."""
        return self.total_battery > 0 or (self.coin_type == "gold" and self.total_coin > 0)

    @property
    def total_battery(self) -> int:
        """The whole gift's frontend value, the number tiering compares against
        gift_battery_high/medium. Combos multiply it via num."""
        return self.unit_battery * self.num


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
    # NO CONSUMER YET: stamped on every mapped event by the source, read by
    # nothing. Restart-crossing duplicates are the dedup ring's job, not this
    # field's — it is here for the day a log or the panel needs to say which
    # connection an event came in on.
    session_generation: int = 0
    raw: dict[str, Any] | None = None
    # Explicit platform reply target, never inferred from a typed @name.
    reply_to_uid: int = 0
    reply_to_name: str = ""
    # None means the target or room owner's UID is unknown, not "a viewer".
    reply_to_anchor: bool | None = None
    # Stamped by the assembly, read by the model's event line only: a short
    # fact about the thread this danmaku sits in (「6 秒前被观众 小路 @过」).
    # Derived from the danmaku stream, never from the platform.
    thread_note: str = ""

    @property
    def is_anonymous(self) -> bool:
        return self.viewer.is_anonymous

    @property
    def dedup_key(self) -> str:
        """Dedup key.

        Falls back to identity plus content plus a one-second bucket when the
        platform gives us no id, which is what stops a reconnect from replaying
        the same reaction.

        Gifts carry their own discriminator in that fallback. Their text is
        always empty, so identity plus a one-second bucket collapsed a whole
        blind-box batch — SEND_GIFT_V2 can arrive with neither tid nor rnd
        (tests/unit/test_bili_translate.py:202) — into one key, and every gift
        after the first was dropped as a duplicate before the aggregator ever
        saw it. Paid events must not be deduplicated by a coincidence of
        timing.
        """
        if self.event_id:
            return f"{self.kind}:{self.event_id}"
        mark = ""
        if self.gift is not None:
            mark = f":{self.gift.gift_id}:{self.gift.num}:{self.value_cny:.4f}"
        return f"{self.kind}:{self.viewer.identity}:{self.text[:32]}{mark}:{self.ts_ms // 1000}"

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


def sc_dedup_key(sc_id: int) -> str:
    """The dedup key a super chat's LiveEvent will carry, from its platform id.

    SUPER_CHAT_MESSAGE_DELETE only gives us the id, and the revoke path must
    produce byte-identical keys to the ones the mapping layer minted — one
    builder, not two f-strings that must stay in sync by luck.
    """
    return f"{EventKind.SUPER_CHAT}:sc:{sc_id}"


def cny_from_gold(total_coin: int) -> float:
    """Convert gold coins to CNY. 1000 gold == CNY 1."""
    return total_coin / 1000.0


def is_vip_entry(viewer: Viewer, *, room_id: int = 0, lifetime_gift_cny: float = 0.0) -> bool:
    """Whether this arrival deserves a greeting by name.

    Current guards and viewers wearing a level-5-or-higher medal FOR THIS ROOM
    go into the named welcome lane — both read straight off the wire, so a
    first-time captain is greeted this stream, not next. lifetime_gift_cny is
    the legacy store-backed lane; production stopped paying a store read per
    arrival for it, but the condition stays so a caller that already holds the
    number can still use it.

    Args:
        viewer: The person who just walked in.
        room_id: The active room, used to reject medals worn for another room.
        lifetime_gift_cny: What they have spent across all past streams, if the
            caller looked it up; 0 skips the lane.

    Returns:
        True when they are worth greeting individually.
    """
    medal = viewer.medal
    high_local_medal = medal is not None and medal.level >= 5 and medal.is_this_room(room_id)
    return viewer.guard_level.is_patron or high_local_medal or lifetime_gift_cny > 0
