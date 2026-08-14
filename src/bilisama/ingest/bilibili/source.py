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
own bugs and catastrophic init failures. An outer restart bumps
session_generation (stamped on events for observability), drains the stale
non-paid queue, and keeps the paid pocket; replayed paid events are absorbed
by the assembly-level dedup ring, not by anything here.

The first commandment of events.py applies here: a masked uid (0) NEVER drops
an event — for danmaku, uid_crc32 becomes the identity (info[0][7], verified
in VENDOR.md). The other message kinds carry no crc on the wire, so under an
anonymous connection they all collapse to the "anon" identity; that is the
masking cost the runbook spells out, not something this module can repair.
"""

from __future__ import annotations

import asyncio
import http.cookies
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp

from bilisama.ingest.bilibili._vendor.blivedm import handlers as bili_handlers
from bilisama.ingest.bilibili._vendor.blivedm.clients import web as bili_web
from bilisama.ingest.bilibili.safety import CircuitBreaker, DedupRing
from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Medal,
    Viewer,
    cny_from_gold,
    sc_dedup_key,
)
from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.ingest.sources import EventSink

__all__ = ["BilibiliEventSource"]

log = get_logger(__name__)

_QUEUE_SIZE = 1024
# Matches the timeout upstream applies when it builds its own session
# (ws_base.py:93, ClientTimeout(total=10)); a session we hand in must bring
# its own, or init_room inherits aiohttp's implicit ~300s and a stalled API
# response hangs start() for minutes with nothing for the supervisor to see.
_HTTP_TIMEOUT_S = 10.0
# Parse-budget sampling (plan section 16.8 item 27): commands are classified
# BEFORE full parsing. Paid commands always parse; danmaku and presence get a
# per-second budget each and shed the excess, counted per lane. The stated
# cost: in a ten-thousand-viewer room, regular-viewer counts become sampled
# figures rather than a census.
_DANMAKU_PARSE_BUDGET_PER_S = 80
_PRESENCE_PARSE_BUDGET_PER_S = 40
_PARSE_LANES: dict[str, str] = {
    "DANMU_MSG": "danmaku",
    # Cross-room mirrors ride the danmaku budget too: a linked-room flood is
    # exactly the case a parse budget exists for.
    "DANMU_MSG_MIRROR": "danmaku",
    "INTERACT_WORD_V2": "presence",
}
_LANE_BUDGETS = {
    "danmaku": _DANMAKU_PARSE_BUDGET_PER_S,
    "presence": _PRESENCE_PARSE_BUDGET_PER_S,
}
# One purchase arrives as USER_TOAST_MSG_V2 and, sometimes, a legacy GUARD_BUY
# too; the 0.35s dedup ring downstream is far too short for that pair, so the
# source keeps its own window (VENDOR.md verification 4). Keyed on identity
# PLUS the purchase timestamp: under a masked connection every buyer shares
# the "anon" identity, and identity alone would merge two different people's
# purchases away.
_GUARD_MERGE_WINDOW_S = 30.0

# The drain queue carries None as a wake token: a paid arrival, stop() and a
# dying client each push one so _next_event never has to poll on a timer.
_WAKE = None

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
    # An empty id must stay empty: "gift:" would be one constant, truthy key
    # shared by every viewer, and dedup_key's identity+content fallback — the
    # thing built for exactly this — would never engage.
    transaction = message.tid or message.rnd
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=room_id,
        viewer=viewer,
        gift=gift,
        value_cny=value,
        event_id=f"gift:{transaction}" if transaction else "",
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


def _guard_event(
    viewer: Viewer,
    *,
    room_id: int,
    text: str,
    price: int,
    num: int,
    uid: int,
    start_time: int,
    recv_at: float,
    generation: int,
) -> LiveEvent:
    """The one shape both guard-purchase wires map onto.

    The toast and the legacy GUARD_BUY must mint byte-identical event ids and
    values — the 30s merge window only recognises the double-send because
    they do. One builder keeps them from drifting apart.

    A masked buyer has uid 0 and start_time is only second-accurate, so
    `guard:0:<second>` made every anonymous purchase in the same second one
    event: the second ¥198 buyer was merged away unthanked. The tail folds in
    what still differs between two such buyers, and it stays identical across
    both wires because both read it from the same fields.
    """
    tail = "" if uid else f":{viewer.name}:{viewer.guard_level}:{price}:{num}"
    return LiveEvent(
        kind=EventKind.GUARD_BUY,
        room_id=room_id,
        viewer=viewer,
        text=text,
        value_cny=cny_from_gold(price * num),
        event_id=f"guard:{uid}:{start_time}{tail}",
        ts_ms=int(start_time) * 1000,
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
    return _guard_event(
        viewer,
        room_id=room_id,
        text=message.toast_msg,
        price=message.price,
        num=message.num,
        uid=message.uid,
        start_time=message.start_time,
        recv_at=recv_at,
        generation=generation,
    )


def event_from_guard_buy(
    message: Any, *, room_id: int, recv_at: float, generation: int
) -> LiveEvent:
    """Legacy GUARD_BUY — the fallback when no toast arrives; merged by the
    source's window."""
    viewer = Viewer(
        uid=message.uid,
        name=message.username,
        guard_level=GuardLevel.from_wire(message.guard_level),
    )
    return _guard_event(
        viewer,
        room_id=room_id,
        text="",
        price=message.price,
        num=message.num,
        uid=message.uid,
        start_time=message.start_time,
        recv_at=recv_at,
        generation=generation,
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
    safe; every mapping call is guarded so a mapping bug cannot kill the
    library's network task. Upstream's OWN parse step (from_command inside
    handle()) is swallowed by the vendored client and logged on 'blivedm' —
    those failures never reach note_error, which is why the quarterly
    re-vendor tripwire is the unit tests, not this counter."""

    def __init__(self, owner: BilibiliEventSource) -> None:
        super().__init__()
        self._owner = owner

    def handle(self, client: Any, command: dict[str, Any]) -> None:
        """Budget gate ahead of upstream's dispatch-and-parse.

        LIVE / PREPARING sit in upstream's ignore list, so they are lifted
        here into ROOM_STATE events before that list swallows them.
        """
        cmd = str(command.get("cmd", "")).split(":")[0]
        if cmd in ("LIVE", "PREPARING"):
            self._owner.on_room_state("live" if cmd == "LIVE" else "preparing")
            return
        lane = _PARSE_LANES.get(cmd)
        if lane is not None and not self._owner.parse_allowed(lane):
            return
        super().handle(client, command)

    def _guarded(self, fn: Any, message: Any) -> None:
        owner = self._owner
        try:
            owner.offer(
                fn(
                    message,
                    room_id=owner.mapped_room_id(),
                    recv_at=owner.clock_now(),
                    generation=owner.generation(),
                )
            )
        except Exception as exc:
            owner.note_error(exc)

    def _on_danmaku(self, client: Any, message: Any) -> None:
        if getattr(message, "is_mirror", False):
            # Cross-room danmaku during a link/PK session: those viewers are
            # watching another room and cannot hear a reply from this one.
            self._owner.note_shed("mirror")
            return
        self._guarded(event_from_danmaku, message)

    def _on_gift(self, client: Any, message: Any) -> None:
        self._guarded(event_from_gift, message)

    def _on_super_chat(self, client: Any, message: Any) -> None:
        self._guarded(event_from_super_chat, message)

    def _on_user_toast_v2(self, client: Any, message: Any) -> None:
        self._guarded(event_from_user_toast, message)

    def _on_buy_guard(self, client: Any, message: Any) -> None:
        self._guarded(event_from_guard_buy, message)

    def _on_interact_word_v2(self, client: Any, message: Any) -> None:
        self._guarded(event_from_interact, message)

    def _on_heartbeat(self, client: Any, message: Any) -> None:
        self._owner.note_popularity(int(getattr(message, "popularity", 0) or 0))

    def _on_super_chat_delete(self, client: Any, message: Any) -> None:
        try:
            self._owner.on_sc_delete(list(getattr(message, "ids", []) or []))
        except Exception as exc:
            self._owner.note_error(exc)

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
        on_sc_delete: Callable[[str], None] | None = None,
    ) -> None:
        self.name = "bilibili"
        self._room_id_arg = room_id
        self._clock = clock
        self._sessdata = sessdata
        # None entries are wake tokens (_WAKE): see _nudge().
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue(maxsize=queue_size)
        # Paid events never drop: their volume is single digits per minute, so
        # an unbounded side pocket is safe, and it drains first.
        self._paid: deque[LiveEvent] = deque()
        self._stop_requested = False
        self._client_dead = False
        self._client_error: BaseException | None = None
        self._guard_merge = DedupRing(window_s=_GUARD_MERGE_WINDOW_S, capacity=1024)
        # Caught-failure book (mapping bugs); escaped crashes are the
        # supervisor's book. Tripping raises out of the drain loop so the
        # supervisor's backoff-and-give-up policy applies to both.
        self._breaker = CircuitBreaker()
        self._generation = 0
        self._real_room_id = 0
        self._connected = False
        self._popularity = 0
        self._counts: dict[str, int] = {}
        self._dropped = 0
        self._errors = 0
        self._danmaku_anonymous = 0
        self._on_sc_delete = on_sc_delete
        self._parse_windows: dict[str, list[float]] = {}
        self._shed: dict[str, int] = {}
        self._room_state = ""

    # ---- Source protocol ----

    def _reset_run_state(self) -> None:
        # Kept out of start()'s body: an inline `self._client_error = None`
        # makes mypy narrow the attribute to None for the whole function, and
        # the callback assignments it can't see turn the final error check
        # into "unreachable".
        self._generation += 1
        self._stop_requested = False
        self._client_dead = False
        self._client_error = None
        # Fresh failure book per supervised run: without this, a tripped
        # breaker would re-raise instantly on every restart and burn the
        # supervisor's budget without giving the new connection a chance.
        self._breaker.reset()
        # Stale chatter from the dead connection is worthless; paid events
        # survive the restart and the assembly dedup ring absorbs replays.
        while not self._queue.empty():
            self._queue.get_nowait()

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
            if not await client.init_room():
                # Upstream returns False and DEGRADES: room_id stays the short
                # vanity id and the default danmaku servers are used. Refuse
                # instead — a wrong room id poisons medal matching all run.
                raise RuntimeError(f"房间 {self._room_id_arg} 初始化失败（房号不对，或接口被风控）")
            # init_room succeeded, so keep client.start() from running the
            # whole thing again inside the network coroutine — upstream only
            # clears this flag on its own start path (ws_base.py:305-310).
            client._need_init_room = False
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
            try:
                await client.stop_and_close()
            except Exception as exc:
                log.debug("bilibili.close_failed", error_text=str(exc)[:200])
            await session.close()
        if self._breaker.is_open:
            raise RuntimeError(f"弹幕映射连续失败，熔断：{self._breaker.reason}")
        if self._client_error is not None and not self._stop_requested:
            # Escaped blivedm's own reconnection: hand it to SupervisedSource.
            raise RuntimeError(f"弹幕连接挂了：{self._client_error}") from self._client_error

    async def stop(self) -> None:
        self._stop_requested = True
        self._nudge()

    # ---- callback side (same loop, synchronous) ----

    def mapped_room_id(self) -> int:
        return self._real_room_id or self._room_id_arg

    def clock_now(self) -> float:
        return self._clock.monotonic()

    def generation(self) -> int:
        return self._generation

    def offer(self, event: LiveEvent | None) -> None:
        if event is None:
            return
        if event.kind is EventKind.GUARD_BUY and self._merged_guard(event):
            return
        self._counts[event.kind.value] = self._counts.get(event.kind.value, 0) + 1
        if event.kind is EventKind.DANMAKU and event.is_anonymous:
            self._danmaku_anonymous += 1
        if event.is_paid:
            self._paid.append(event)
            self._nudge()
            return
        if self._queue.full():
            # Live streams stay live: shed the oldest, keep the account.
            self._queue.get_nowait()
            self._dropped += 1
        self._queue.put_nowait(event)

    def parse_allowed(self, lane: str) -> bool:
        """Spend one unit of this second's parse budget; False means shed."""
        now = self._clock.monotonic()
        window = self._parse_windows.get(lane)
        if window is None or now - window[0] >= 1.0:
            window = [now, 0.0]
            self._parse_windows[lane] = window
        if window[1] >= _LANE_BUDGETS[lane]:
            self.note_shed(lane)
            return False
        window[1] += 1
        return True

    def note_shed(self, lane: str) -> None:
        self._shed[lane] = self._shed.get(lane, 0) + 1

    def on_room_state(self, state: str) -> None:
        self._room_state = state
        self.offer(
            LiveEvent(
                kind=EventKind.ROOM_STATE,
                room_id=self.mapped_room_id(),
                text=state,
                event_id=f"state:{state}:{int(self._clock.monotonic() * 1000)}",
                recv_at=self._clock.monotonic(),
                session_generation=self._generation,
            )
        )

    def on_sc_delete(self, ids: list[int]) -> None:
        """A withdrawn super chat: pull it out of our own pocket first, then
        revoke whatever already reached the scheduler.

        Both halves matter. The delete can arrive in the SAME decompressed
        bundle as the SC (upstream dispatches a bundle synchronously), when
        the SC still sits in the paid deque and the scheduler has never seen
        its key — revoking alone would be a no-op and the withdrawn SC would
        still be thanked moments later.
        """
        withdrawn = {f"sc:{mid}" for mid in ids}
        before = len(self._paid)
        if withdrawn:
            self._paid = deque(e for e in self._paid if e.event_id not in withdrawn)
        if len(self._paid) != before:
            log.info("bilibili.sc_withdrawn_before_emit", count=before - len(self._paid))
        if self._on_sc_delete is None:
            return
        for mid in ids:
            self._on_sc_delete(sc_dedup_key(mid))

    def note_error(self, exc: Exception) -> None:
        self._errors += 1
        log.warning("bilibili.map_failed", error_text=str(exc)[:200])
        if self._breaker.record_failure(self._clock.monotonic(), str(exc)[:200]):
            # Wake the drain loop so start() can raise and hand the systematic
            # failure to the supervisor (backoff, then give up visibly).
            self._client_dead = True
            self._nudge()

    def note_popularity(self, value: int) -> None:
        self._popularity = value

    def on_client_stopped(self, exception: BaseException | None) -> None:
        self._client_error = exception
        self._client_dead = True
        self._nudge()

    # ---- internals ----

    def _nudge(self) -> None:
        """Push a wake token so _next_event never needs a poll timer."""
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped += 1
        self._queue.put_nowait(_WAKE)

    async def _next_event(self) -> LiveEvent | None:
        while True:
            if self._paid:
                return self._paid.popleft()
            if self._stop_requested or self._client_dead:
                return None
            item = await self._queue.get()
            if item is None:
                continue  # wake token: re-check the paid pocket and the flags
            return item

    def _merged_guard(self, event: LiveEvent) -> bool:
        """True when this GUARD_BUY duplicates a recent one — the toast/legacy
        double-send.

        Keyed on the purchase id both wires mint identically (see _guard_event),
        which is also what keeps two masked buyers in the same second — all
        sharing the "anon" identity — from merging each other away.
        """
        return self._guard_merge.seen(event.event_id, self._clock.monotonic())

    def _build_session(self) -> aiohttp.ClientSession:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S))
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
        danmaku_total = self._counts.get(EventKind.DANMAKU.value, 0)
        return {
            "connected": self._connected,
            "room_id": self._real_room_id,
            "popularity": self._popularity,
            "counts": dict(self._counts),
            "dropped": self._dropped,
            "map_errors": self._errors,
            "breaker_open": self._breaker.is_open,
            "shed": dict(self._shed),
            "room_state": self._room_state,
            "anonymous_ratio": (
                round(self._danmaku_anonymous / danmaku_total, 2) if danmaku_total else 0.0
            ),
        }
