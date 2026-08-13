"""Bilibili live events through vendored blivedm, behind the Source protocol.

Two halves, split on purpose. The mapping half is module-level pure functions
(upstream message → LiveEvent) so unit tests can drive them through upstream's
own `from_command` parsers — the quarterly re-vendor tripwire VENDOR.md
promises. The transport half is BilibiliEventSource: it owns the aiohttp
session and the BLiveClient, funnels callbacks through a bounded queue, and
raises out of start() when the client dies so SupervisedSource can restart it.

Reconnection is split between two layers (plan section 15.11): blivedm's own
reconnect handles transport drops — token refresh and backoff are its existing
behaviour — while SupervisedSource only catches what escapes it, meaning our
own bugs and catastrophic init failures. session_generation bumps only on the
outer restart; the few events an inner reconnect replays are absorbed by the
dedup window downstream.

The first commandment of events.py applies here: a masked uid (0) NEVER drops
an event — uid_crc32 becomes the identity (info[0][7], verified in VENDOR.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookies
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp

from bilisama.ingest.bilibili._vendor.blivedm import handlers as bili_handlers
from bilisama.ingest.bilibili._vendor.blivedm.clients import web as bili_web
from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Medal,
    Viewer,
    cny_from_gold,
)
from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.ingest.sources import EventSink

__all__ = ["BilibiliEventSource"]

log = get_logger(__name__)

_QUEUE_SIZE = 1024
# One purchase arrives as USER_TOAST_MSG_V2 and, sometimes, a legacy GUARD_BUY
# too; the 0.35s dedup ring downstream is far too short for that pair, so the
# source keeps its own per-viewer window (VENDOR.md verification 4).
_GUARD_MERGE_WINDOW_S = 30.0

# InteractWordV2 msg_type → our kind. 4 (special-follow) and 5 (mutual-follow)
# are follow-shaped; 6 is the like path the web dialect exposes (upstream has
# no dedicated web LIKE callback — verified against the dispatch table).
_INTERACT_KIND: dict[int, EventKind] = {
    1: EventKind.ENTRY,
    2: EventKind.FOLLOW,
    3: EventKind.SHARE,
    4: EventKind.FOLLOW,
    5: EventKind.FOLLOW,
    6: EventKind.LIKE,
}


# ------------------------------------------------------------------ mapping


def _medal(name: str, level: int, up_name: str, anchor_room_id: int) -> Medal | None:
    if not name:
        return None
    return Medal(name=name, level=level, up_name=up_name, anchor_room_id=anchor_room_id)


def event_from_danmaku(message: Any, *, room_id: int, recv_at: float, generation: int) -> LiveEvent:
    """DANMU_MSG → DANMAKU. Timestamp is already milliseconds upstream."""
    viewer = Viewer(
        uid=message.uid,
        uid_hash=message.uid_crc32,
        name=message.uname,
        face_url=message.face,
        user_level=message.user_level,
        wealth_level=message.wealth_level,
        guard_level=GuardLevel.from_wire(message.privilege_type),
        is_admin=bool(message.admin),
        medal=_medal(
            message.medal_name, message.medal_level, message.runame, message.medal_room_id
        ),
    )
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=room_id,
        viewer=viewer,
        text=message.msg,
        event_id=f"dm:{message.rnd}" if message.rnd else "",
        ts_ms=int(message.timestamp),
        recv_at=recv_at,
        session_generation=generation,
    )


def event_from_gift(message: Any, *, room_id: int, recv_at: float, generation: int) -> LiveEvent:
    """SEND_GIFT (and V2, which upstream normalises onto the same callback).

    Upstream exposes no combo id on the model, so the aggregation key is
    synthesised from viewer and gift — the combo aggregator only needs "same
    person, same gift, close together".
    """
    viewer = Viewer(
        uid=message.uid,
        name=message.uname,
        face_url=message.face,
        guard_level=GuardLevel.from_wire(message.guard_level),
        medal=_medal(message.medal_name, message.medal_level, "", message.medal_room_id),
    )
    gift = Gift(
        gift_id=message.gift_id,
        name=message.gift_name,
        num=message.num,
        coin_type=message.coin_type,
        total_coin=message.total_coin,
        combo_id=f"{viewer.identity}:{message.gift_id}",
    )
    value = cny_from_gold(message.total_coin) if message.coin_type == "gold" else 0.0
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=room_id,
        viewer=viewer,
        gift=gift,
        value_cny=value,
        event_id=f"gift:{message.tid or message.rnd}",
        ts_ms=int(message.timestamp) * 1000,
        recv_at=recv_at,
        session_generation=generation,
    )


def event_from_super_chat(
    message: Any, *, room_id: int, recv_at: float, generation: int
) -> LiveEvent:
    """SUPER_CHAT_MESSAGE → SUPER_CHAT. price is already CNY (VENDOR.md 3)."""
    viewer = Viewer(
        uid=message.uid,
        name=message.uname,
        face_url=message.face,
        user_level=message.user_level,
        guard_level=GuardLevel.from_wire(message.guard_level),
        medal=_medal(message.medal_name, message.medal_level, "", message.medal_room_id),
    )
    return LiveEvent(
        kind=EventKind.SUPER_CHAT,
        room_id=room_id,
        viewer=viewer,
        text=message.message,
        value_cny=float(message.price),
        event_id=f"sc:{message.id}",
        ts_ms=int(message.start_time) * 1000,
        recv_at=recv_at,
        session_generation=generation,
    )


def event_from_user_toast(
    message: Any, *, room_id: int, recv_at: float, generation: int
) -> LiveEvent | None:
    """USER_TOAST_MSG_V2 → GUARD_BUY, the primary path for guard purchases.

    One purchase emits source=0 then source=2 (the official comment feed hides
    the 2) — keep the first, drop the second. price is gold seeds per unit.
    """
    if message.source == 2:
        return None
    viewer = Viewer(
        uid=message.uid,
        name=message.username,
        guard_level=GuardLevel.from_wire(message.guard_level),
    )
    return LiveEvent(
        kind=EventKind.GUARD_BUY,
        room_id=room_id,
        viewer=viewer,
        text=message.toast_msg,
        value_cny=cny_from_gold(message.price * message.num),
        event_id=f"guard:{message.uid}:{message.start_time}",
        ts_ms=int(message.start_time) * 1000,
        recv_at=recv_at,
        session_generation=generation,
    )


def event_from_guard_buy(
    message: Any, *, room_id: int, recv_at: float, generation: int
) -> LiveEvent:
    """Legacy GUARD_BUY — the fallback when no toast arrives; merged by the
    source's per-viewer window."""
    viewer = Viewer(
        uid=message.uid,
        name=message.username,
        guard_level=GuardLevel.from_wire(message.guard_level),
    )
    return LiveEvent(
        kind=EventKind.GUARD_BUY,
        room_id=room_id,
        viewer=viewer,
        value_cny=cny_from_gold(message.price * message.num),
        event_id=f"guard:{message.uid}:{message.start_time}",
        ts_ms=int(message.start_time) * 1000,
        recv_at=recv_at,
        session_generation=generation,
    )


def event_from_interact(
    message: Any, *, room_id: int, recv_at: float, generation: int
) -> LiveEvent | None:
    """INTERACT_WORD_V2 → ENTRY / FOLLOW / SHARE / LIKE by msg_type.

    The simplified upstream model carries no guard level, so promoting a
    captain's arrival to VIP_ENTER is not decidable here — that upgrade reads
    the store and lives at assembly level (stage-6 periphery batch).
    """
    kind = _INTERACT_KIND.get(message.msg_type)
    if kind is None:
        return None
    viewer = Viewer(uid=message.uid, name=message.username, face_url=message.face)
    return LiveEvent(
        kind=kind,
        room_id=room_id,
        viewer=viewer,
        event_id=f"iw:{message.uid}:{message.msg_type}:{message.timestamp}",
        ts_ms=int(message.timestamp) * 1000,
        recv_at=recv_at,
        session_generation=generation,
    )


# ------------------------------------------------------------------ transport


class _Forwarder(bili_handlers.BaseHandler):  # type: ignore[misc]
    """blivedm callbacks → owner's queues. Callbacks run on OUR event loop
    (blivedm schedules its network coroutine there), so plain put_nowait is
    safe; every callback body is guarded so a mapping bug cannot kill the
    library's network task."""

    def __init__(self, owner: BilibiliEventSource) -> None:
        super().__init__()
        self._owner = owner

    def _guarded(self, build: Any) -> None:
        try:
            self._owner.offer(build())
        except Exception as exc:
            self._owner.note_error(exc)

    def _on_danmaku(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_danmaku(message, **self._owner.map_kwargs()))

    def _on_gift(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_gift(message, **self._owner.map_kwargs()))

    def _on_super_chat(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_super_chat(message, **self._owner.map_kwargs()))

    def _on_user_toast_v2(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_user_toast(message, **self._owner.map_kwargs()))

    def _on_buy_guard(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_guard_buy(message, **self._owner.map_kwargs()))

    def _on_interact_word_v2(self, client: Any, message: Any) -> None:
        self._guarded(lambda: event_from_interact(message, **self._owner.map_kwargs()))

    def _on_heartbeat(self, client: Any, message: Any) -> None:
        self._owner.note_popularity(int(getattr(message, "popularity", 0) or 0))

    def on_client_stopped(self, client: Any, exception: BaseException | None) -> None:
        self._owner.on_client_stopped(exception)


class BilibiliEventSource:
    """One room, one connection, one bounded queue. Implements Source."""

    def __init__(
        self,
        room_id: int,
        clock: Clock,
        *,
        sessdata: str = "",
        queue_size: int = _QUEUE_SIZE,
    ) -> None:
        self.name = "bilibili"
        self._room_id_arg = room_id
        self._clock = clock
        self._sessdata = sessdata
        self._queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=queue_size)
        # Paid events never drop: their volume is single digits per minute, so
        # an unbounded side pocket is safe, and it drains first.
        self._paid: deque[LiveEvent] = deque()
        self._stopped = asyncio.Event()
        self._client_dead = asyncio.Event()
        self._client_error: BaseException | None = None
        self._recent_guard: dict[str, float] = {}
        self._generation = 0
        self._real_room_id = 0
        self._connected = False
        self._popularity = 0
        self._counts: dict[str, int] = {}
        self._dropped = 0
        self._errors = 0
        self._danmaku_total = 0
        self._danmaku_anonymous = 0

    # ---- Source protocol ----

    def _reset_run_state(self) -> None:
        # Kept out of start()'s body: an inline `self._client_error = None`
        # makes mypy narrow the attribute to None for the whole function, and
        # the callback assignments it can't see turn the final error check
        # into "unreachable".
        self._generation += 1
        self._stopped.clear()
        self._client_dead.clear()
        self._client_error = None

    async def start(self, emit: EventSink) -> None:
        self._reset_run_state()
        session = self._build_session()
        # uid=None asks blivedm to fetch the logged-in uid (needs SESSDATA);
        # 0 skips that round trip for the anonymous path.
        client = bili_web.BLiveClient(
            self._room_id_arg, uid=None if self._sessdata else 0, session=session
        )
        client.set_handler(_Forwarder(self))
        try:
            await client.init_room()
            self._real_room_id = int(getattr(client, "room_id", 0) or 0)
            self._connected = True
            log.info(
                "bilibili.connected",
                room_id=self._real_room_id,
                logged_in=bool(self._sessdata),
            )
            client.start()
            while True:
                event = await self._next_event()
                if event is None:
                    break
                await emit(event)
        finally:
            self._connected = False
            with contextlib.suppress(Exception):
                await client.stop_and_close()
            await session.close()
        if self._client_error is not None and not self._stopped.is_set():
            # Escaped blivedm's own reconnection: hand it to SupervisedSource.
            raise RuntimeError(f"弹幕连接挂了：{self._client_error}") from self._client_error

    async def stop(self) -> None:
        self._stopped.set()

    # ---- callback side (same loop, synchronous) ----

    def map_kwargs(self) -> dict[str, Any]:
        return {
            "room_id": self._real_room_id or self._room_id_arg,
            "recv_at": self._clock.monotonic(),
            "generation": self._generation,
        }

    def offer(self, event: LiveEvent | None) -> None:
        if event is None:
            return
        if event.kind is EventKind.GUARD_BUY and self._merged_guard(event):
            return
        self._counts[event.kind.value] = self._counts.get(event.kind.value, 0) + 1
        if event.kind is EventKind.DANMAKU:
            self._danmaku_total += 1
            if event.is_anonymous:
                self._danmaku_anonymous += 1
        if event.is_paid:
            self._paid.append(event)
            return
        if self._queue.full():
            # Live streams stay live: shed the oldest, keep the account.
            self._queue.get_nowait()
            self._dropped += 1
        self._queue.put_nowait(event)

    def note_error(self, exc: Exception) -> None:
        self._errors += 1
        log.warning("bilibili.map_failed", error_text=str(exc)[:200])

    def note_popularity(self, value: int) -> None:
        self._popularity = value

    def on_client_stopped(self, exception: BaseException | None) -> None:
        self._client_error = exception
        self._client_dead.set()

    # ---- internals ----

    async def _next_event(self) -> LiveEvent | None:
        while True:
            if self._paid:
                return self._paid.popleft()
            if self._stopped.is_set():
                return None
            if self._client_dead.is_set() and self._queue.empty():
                return None
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except TimeoutError:
                continue

    def _merged_guard(self, event: LiveEvent) -> bool:
        """True when this GUARD_BUY duplicates a recent one for the same
        viewer — the toast/legacy double-send window."""
        now = self._clock.monotonic()
        key = event.viewer.identity
        last = self._recent_guard.get(key)
        self._recent_guard[key] = now
        return last is not None and now - last < _GUARD_MERGE_WINDOW_S

    def _build_session(self) -> aiohttp.ClientSession:
        session = aiohttp.ClientSession()
        if self._sessdata:
            # Domain-scoped exactly like upstream's sample.py: the cookie must
            # match api.bilibili.com and api.live.bilibili.com, not just www.
            cookies = http.cookies.SimpleCookie()
            cookies["SESSDATA"] = self._sessdata
            cookies["SESSDATA"]["domain"] = "bilibili.com"
            session.cookie_jar.update_cookies(cookies)
        return session

    # ---- health ----

    def status(self) -> dict[str, Any]:
        anonymous_ratio = (
            round(self._danmaku_anonymous / self._danmaku_total, 2) if self._danmaku_total else 0.0
        )
        return {
            "connected": self._connected,
            "room_id": self._real_room_id,
            "popularity": self._popularity,
            "counts": dict(self._counts),
            "dropped": self._dropped,
            "map_errors": self._errors,
            "anonymous_ratio": anonymous_ratio,
        }
