"""Dynamic pacing policy for live-room events only.

Microphone turns never enter this module. The director's speaking floor owns
streamer priority; this policy only decides how eagerly ordinary platform
events may ask for that floor once it is free.

Chattiness used to derive fixed timers (a 20s danmaku window and a 12s global
cooldown at medium) — a quiet room waited half a minute for its first answered
danmaku while a busy one still spammed. Here chattiness is a relative factor
over the room's measured activity instead: the pacer bands the last 60s of
live events into quiet/sparse/active/busy and hands every consumer its knobs
(danmaku window, entry coalescing wait, proactive idle threshold, score
threshold) plus one shared token bucket for ordinary speech. Paid and VIP
lanes always bypass the bucket. Numbers are deliberately not config keys: they
are one policy table, and the only tuning surface is chattiness itself.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from bilisama.config.enums import Chattiness
from bilisama.ingest.events import EventKind, LiveEvent

if TYPE_CHECKING:
    from bilisama.clock import Clock

__all__ = ["EventPacer", "EventPacingSnapshot", "RoomActivity"]

_ACTIVITY_WINDOW_S = 60.0
_BURST_WINDOW_S = 10.0
# Upshifts apply instantly (a flood must not wait); downshifts hold so one
# quiet gap between waves does not whipsaw every window length.
_DOWNSHIFT_HOLD_S = 30.0
_ORDINARY_LANES = frozenset({"danmaku", "entry", "proactive"})


class RoomActivity(StrEnum):
    """Human-readable live-event load bands; voice activity is excluded."""

    QUIET = "quiet"
    SPARSE = "sparse"
    ACTIVE = "active"
    BUSY = "busy"


_RANK = {
    RoomActivity.QUIET: 0,
    RoomActivity.SPARSE: 1,
    RoomActivity.ACTIVE: 2,
    RoomActivity.BUSY: 3,
}


@dataclass(frozen=True, slots=True)
class EventPacingSnapshot:
    """Effective event-lane knobs at one point in time."""

    activity: RoomActivity
    danmaku_window_s: float
    score_threshold: float
    budget_capacity: float
    budget_refill_s: float
    entry_coalesce_s: float
    entry_enabled: bool
    proactive_idle_s: float
    proactive_enabled: bool


class EventPacer:
    """Observe room events and meter only ordinary event-triggered replies."""

    def __init__(self, clock: Clock, *, chattiness: Callable[[], Chattiness]) -> None:
        self._clock = clock
        self._chattiness = chattiness
        self._danmaku: deque[tuple[float, str]] = deque()
        self._entries: deque[tuple[float, str]] = deque()
        self._paid: deque[tuple[float, str]] = deque()
        self._activity = RoomActivity.QUIET
        self._downshift_since: float | None = None
        self._last_refill = clock.monotonic()
        self._tokens = self._capacity(chattiness())
        self._consumed: dict[str, int] = {}
        self._denied: dict[str, int] = {}

    def note_event(self, event: LiveEvent) -> None:
        """Add one platform event. Console events and microphone turns stay out."""
        if event.room_id <= 0:
            return
        now = self._clock.monotonic()
        if event.kind is EventKind.DANMAKU:
            self._danmaku.append((now, event.viewer.identity))
        elif event.kind in {EventKind.ENTRY, EventKind.VIP_ENTER}:
            # Keyed by identity later: one viewer's duplicate packets must not
            # manufacture a room surge.
            self._entries.append((now, event.viewer.identity))
        elif event.kind in {EventKind.GIFT, EventKind.SUPER_CHAT, EventKind.GUARD_BUY}:
            # One combo is one paid interaction, not fifty. combo_id first,
            # then whatever id the platform gave.
            combo_id = event.gift.combo_id if event.gift is not None else ""
            paid_key = combo_id or event.event_id or event.dedup_key
            self._paid.append((now, f"{event.kind.value}:{paid_key}"))
        self._update_activity(now)

    def snapshot(self) -> EventPacingSnapshot:
        now = self._clock.monotonic()
        self._update_activity(now)
        level = self._chattiness()
        factor = {
            Chattiness.LOW: 1.35,
            Chattiness.MEDIUM: 1.0,
            Chattiness.HIGH: 0.7,
        }[level]
        state = self._activity
        window = {
            RoomActivity.QUIET: 1.0,
            RoomActivity.SPARSE: 2.0,
            RoomActivity.ACTIVE: 4.0,
            RoomActivity.BUSY: 8.0,
        }[state]
        refill = {
            RoomActivity.QUIET: 8.0,
            RoomActivity.SPARSE: 12.0,
            RoomActivity.ACTIVE: 20.0,
            RoomActivity.BUSY: 60.0,
        }[state]
        entry_wait = {
            RoomActivity.QUIET: 1.0,
            RoomActivity.SPARSE: 2.0,
            RoomActivity.ACTIVE: 4.0,
            RoomActivity.BUSY: 8.0,
        }[state]
        proactive_idle = {
            RoomActivity.QUIET: 30.0,
            RoomActivity.SPARSE: 60.0,
            RoomActivity.ACTIVE: 120.0,
            RoomActivity.BUSY: 300.0,
        }[state]
        base_score = {
            Chattiness.LOW: 0.5,
            Chattiness.MEDIUM: 0.3,
            Chattiness.HIGH: 0.15,
        }[level]
        load_score = {
            RoomActivity.QUIET: -0.05,
            RoomActivity.SPARSE: 0.0,
            RoomActivity.ACTIVE: 0.05,
            RoomActivity.BUSY: 0.1,
        }[state]
        return EventPacingSnapshot(
            activity=state,
            danmaku_window_s=max(0.5, min(12.0, window * factor)),
            score_threshold=max(0.1, min(0.8, base_score + load_score)),
            budget_capacity=self._capacity(level),
            budget_refill_s=refill * factor,
            entry_coalesce_s=entry_wait,
            entry_enabled=state is not RoomActivity.BUSY,
            proactive_idle_s=proactive_idle * factor,
            proactive_enabled=state is not RoomActivity.BUSY,
        )

    def try_consume(
        self,
        lane: Literal[
            "danmaku", "entry", "proactive", "gift", "super_chat", "guard_buy", "vip_enter"
        ],
    ) -> bool:
        """Spend one ordinary opportunity; paid and VIP lanes always bypass it."""
        if lane not in _ORDINARY_LANES:
            return True
        self._refill()
        if self._tokens < 1.0:
            self._denied[lane] = self._denied.get(lane, 0) + 1
            return False
        self._tokens -= 1.0
        self._consumed[lane] = self._consumed.get(lane, 0) + 1
        return True

    def can_consume(
        self,
        lane: Literal[
            "danmaku", "entry", "proactive", "gift", "super_chat", "guard_buy", "vip_enter"
        ],
    ) -> bool:
        """Check current capacity without spending it."""
        if lane not in _ORDINARY_LANES:
            return True
        self._refill()
        return self._tokens >= 1.0

    def status(self) -> dict[str, object]:
        self._refill()
        policy = self.snapshot()
        return {
            "activity": policy.activity.value,
            "danmaku_window_s": round(policy.danmaku_window_s, 2),
            "score_threshold": round(policy.score_threshold, 2),
            "ordinary_tokens": round(self._tokens, 2),
            "ordinary_capacity": policy.budget_capacity,
            "budget_refill_s": round(policy.budget_refill_s, 2),
            "entry_coalesce_s": round(policy.entry_coalesce_s, 2),
            "entry_enabled": policy.entry_enabled,
            "proactive_idle_s": round(policy.proactive_idle_s, 2),
            "proactive_enabled": policy.proactive_enabled,
            "consumed": dict(self._consumed),
            "denied": dict(self._denied),
        }

    def _refill(self) -> None:
        now = self._clock.monotonic()
        policy = self.snapshot()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(
            policy.budget_capacity,
            self._tokens + elapsed / max(policy.budget_refill_s, 0.1),
        )

    def _update_activity(self, now: float) -> None:
        self._prune(now)
        target = self._target_activity(now)
        if _RANK[target] > _RANK[self._activity]:
            self._activity = target
            self._downshift_since = None
            return
        if _RANK[target] == _RANK[self._activity]:
            self._downshift_since = None
            return
        if self._downshift_since is None:
            self._downshift_since = now
            return
        if now - self._downshift_since >= _DOWNSHIFT_HOLD_S:
            self._activity = target
            self._downshift_since = None

    def _prune(self, now: float) -> None:
        while self._danmaku and now - self._danmaku[0][0] >= _ACTIVITY_WINDOW_S:
            self._danmaku.popleft()
        while self._entries and now - self._entries[0][0] >= _ACTIVITY_WINDOW_S:
            self._entries.popleft()
        while self._paid and now - self._paid[0][0] >= _ACTIVITY_WINDOW_S:
            self._paid.popleft()

    def _target_activity(self, now: float) -> RoomActivity:
        danmaku_count = len(self._danmaku)
        unique_chatters = len({identity for _stamp, identity in self._danmaku})
        entry_count = len({identity for _stamp, identity in self._entries})
        danmaku_burst = sum(
            1 for stamp, _identity in self._danmaku if now - stamp <= _BURST_WINDOW_S
        )
        entry_burst = len(
            {identity for stamp, identity in self._entries if now - stamp <= _BURST_WINDOW_S}
        )
        paid_count = len({identity for _stamp, identity in self._paid})
        if (
            danmaku_count > 30
            or unique_chatters > 30
            or danmaku_burst >= 8
            or entry_burst >= 15
            or paid_count >= 8
        ):
            return RoomActivity.BUSY
        if danmaku_count >= 9 or unique_chatters >= 9 or entry_count >= 16 or paid_count >= 4:
            return RoomActivity.ACTIVE
        if danmaku_count >= 3 or unique_chatters >= 3 or entry_count >= 6 or paid_count >= 2:
            return RoomActivity.SPARSE
        return RoomActivity.QUIET

    @staticmethod
    def _capacity(level: Chattiness) -> float:
        return {
            Chattiness.LOW: 1.0,
            Chattiness.MEDIUM: 2.0,
            Chattiness.HIGH: 3.0,
        }[level]
