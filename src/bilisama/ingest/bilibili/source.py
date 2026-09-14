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

Which makes "are we actually logged in" a question worth answering honestly,
and this module is the only place that can: an expired SESSDATA still yields
a successful init_room with uid 0, so every caller upstream of here sees a
configured credential and assumes it worked. `credential_stale` and the
matching status() keys are that answer — read them once the connection is up
rather than trusting the presence of a credential string.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    "INTERACT_WORD_V2": "presence",
}
# Cross-room danmaku during a link/PK. Those viewers are watching the OTHER
# room and cannot hear a reply from this one, so the event is discarded either
# way (_Forwarder._on_danmaku). Dropping it at the command — upstream sets
# is_mirror only from this one command (_vendor/blivedm/handlers.py:83-86) —
# is what keeps the opponent's flood from spending the home room's parse
# budget and pushing our own viewers into the sampled remainder.
_MIRROR_CMDS = frozenset({"DANMU_MSG_MIRROR"})
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

# InteractWordV2 msg_type → our kind.
#
# 1/2/3 are named by upstream's own enum (_vendor/blivedm/models/pb.py:16-20:
# EnterRoom / Follow / ShareRoom) and are the three the plan's B2 acceptance
# lists.
#
# 4, 5 and 6 are UNVERIFIED (待验证). Upstream names no value above 3, so
# "special-follow / mutual-follow / like" is our reading, not a checked fact —
# what the dispatch table actually shows is only that upstream exposes no
# dedicated web LIKE callback, which is a different claim. Plan section 5.1
# settled likes as "second batch, parse ourselves or drop", so the safe way to
# close this is a real room: log the msg_type distribution for a session and
# confirm or correct these three. Kept rather than dropped meanwhile because
# all four kinds are feed-only — none of them reaches intent_for
# (director/intents.py:118), so a wrong guess mislabels a feed row and costs
# nothing audible, while dropping them would lose arrivals we do get right.
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


def _danmaku_reply_target(message: Any) -> tuple[int, str]:
    """Read Web extra.reply_mid/reply_uname via the existing vendor parser."""
    extra = getattr(message, "extra_dict", {})
    if not isinstance(extra, dict):
        log.debug("source.invalid_reply_metadata", reason="extra_not_object")
        return 0, ""
    uid = extra.get("reply_mid", 0)
    if isinstance(uid, str) and uid.isascii() and uid.isdecimal() and len(uid) <= 20:
        uid = int(uid)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        uid = 0
    name = extra.get("reply_uname", "")
    return uid, name if isinstance(name, str) else ""


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
    reply_uid, reply_name = _danmaku_reply_target(message)
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=room_id,
        viewer=viewer,
        text=message.msg,
        event_id=f"dm:{message.rnd}" if message.rnd else "",
        ts_ms=int(message.timestamp),
        recv_at=recv_at,
        session_generation=generation,
        reply_to_uid=reply_uid,
        reply_to_name=reply_name,
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
        # Wire price 100 == 1 battery. blind_price first: a blind-box gift's
        # `price` is the box, but the viewer (and the thank-you tier) sees what
        # came out of it. Free gifts are silver/empty coin_type and stay 0.
        unit_battery=(
            max(1, int(message.blind_price or message.price) // 100)
            if message.coin_type == "gold"
            else 0
        ),
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

    The locally extended vendor model (VENDOR.md "Local protocol extension")
    carries the arrival's current guard and fan-medal identity, so the
    assembly can classify a VIP arrival off the wire instead of consulting
    spend history.
    """
    kind = _INTERACT_KIND.get(message.msg_type)
    if kind is None:
        return None
    viewer = Viewer(
        uid=message.uid,
        name=message.username,
        face_url=message.face,
        guard_level=GuardLevel.from_wire(message.guard_level),
        medal=_medal(message.medal_name, message.medal_level, "", message.medal_room_id),
    )
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


def _suppress_second_init(client: Any) -> bool:
    """Clear upstream's "still needs init_room" flag; False when it is gone.

    Upstream only clears the flag on its own start path
    (_vendor/blivedm/clients/ws_base.py:301-310), so a start() that already
    ran init_room itself has to clear it or the room-init round trip happens
    twice. A plain assignment would make a re-vendor that renames or drops the
    attribute invisible: we would mint a fresh one nobody reads, every start
    would pay the second round trip, and every unit test would stay green,
    because the fake client defines the attribute itself. Reporting the miss
    is what puts the drift somewhere VENDOR.md's tripwire promise can reach.
    """
    if not hasattr(client, "_need_init_room"):
        return False
    client._need_init_room = False
    return True


def _resolved_uid(client: Any) -> int:
    """The uid upstream actually ended up with, 0 meaning anonymous.

    Read AFTER init_room, never inferred from whether a SESSDATA string
    existed: an expired cookie comes back code -101, and upstream sets uid 0
    and returns True (_vendor/blivedm/clients/web.py:257-262), so init_room
    succeeds and the connection is anonymous with nothing on the wire saying
    so.
    """
    return int(getattr(client, "uid", 0) or 0)


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
        if cmd in _MIRROR_CMDS:
            self._owner.note_shed("mirror")
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
            # Belt and braces behind the command-level drop in handle(): if a
            # re-vendor ever routes mirrors through plain DANMU_MSG, they are
            # still discarded here rather than answered.
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
        # Credential reality, filled once init_room has spoken. Kept apart
        # from `_sessdata` on purpose: that only says a string was configured,
        # these say what the platform did with it.
        self._connection_uid = 0
        self._credential_checked = False
        self._vendor_drift = ""
        # The room owner's uid from the same handshake. 0 means unknown, and
        # unknown means no danmaku gets the anchor mark — misattributing a
        # viewer's question to the streamer is worse than missing the mark.
        self._room_owner_uid = 0
        self._popularity = 0
        self._counts: dict[str, int] = {}
        self._dropped = 0
        self._errors = 0
        self._danmaku_anonymous = 0
        self._anchor_danmaku_marked = 0
        self._on_sc_delete = on_sc_delete
        self._parse_windows: dict[str, list[float]] = {}
        self._shed: dict[str, int] = {}
        self._room_state = ""

    # ---- Source protocol ----

    def _reset_run_state(self) -> int:
        # Kept out of start()'s body: an inline `self._client_error = None`
        # makes mypy narrow the attribute to None for the whole function, and
        # the callback assignments it can't see turn the final error check
        # into "unreachable".
        self._generation += 1
        self._stop_requested = False
        self._client_dead = False
        self._client_error = None
        # A restart re-asks the platform, so last run's answer stops counting
        # until the new init_room replies.
        self._connection_uid = 0
        self._credential_checked = False
        self._vendor_drift = ""
        self._room_owner_uid = 0
        # Fresh failure book per supervised run: without this, a tripped
        # breaker would re-raise instantly on every restart and burn the
        # supervisor's budget without giving the new connection a chance.
        self._breaker.reset()
        # Stale chatter from the dead connection is worthless; paid events
        # survive the restart and the assembly dedup ring absorbs replays.
        # Counted on the way out: those danmaku are gone, and a restart that
        # ate a viewer's message is exactly what the log has to be able to say.
        discarded = 0
        while not self._queue.empty():
            if self._queue.get_nowait() is not None:
                discarded += 1
        return discarded

    async def start(self, emit: EventSink) -> None:
        discarded = self._reset_run_state()
        # Paired with bilibili.connected below: an attempt that never gets
        # there is a hung init_room, which otherwise looks like silence.
        log.info(
            "bilibili.connecting",
            room_id=self._room_id_arg,
            generation=self._generation,
            discarded_count=discarded,
        )
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
            # whole thing again inside the network coroutine.
            if not _suppress_second_init(client):
                self._vendor_drift = "_need_init_room"
                log.warning(
                    "bilibili.vendor_drift",
                    field="_need_init_room",
                    advice="上游改名或删了这个私有属性，每次连接会多打一轮房间初始化接口；"
                    "按 blivedm/VENDOR.md 的 re-vendor 步骤重新核对",
                )
            self._connection_uid = _resolved_uid(client)
            self._credential_checked = True
            self._real_room_id = int(getattr(client, "room_id", 0) or 0)
            self._room_owner_uid = int(getattr(client, "room_owner_uid", 0) or 0)
            self._connected = True
            log.info(
                "bilibili.connected",
                room_id=self._real_room_id,
                logged_in=self._connection_uid > 0,
                owner_uid_known=self._room_owner_uid > 0,
            )
            if self.credential_stale:
                # The one moment the truth is knowable. Nothing downstream can
                # tell this apart from a working login on its own — the banner
                # only ever saw a non-empty string — so say it here, loudly.
                log.warning(
                    "bilibili.credential_stale",
                    room_id=self._real_room_id,
                    advice="SESSDATA 已失效，这一场连的其实是匿名：观众全部打码，"
                    "认不出常客、点不了名。重新登录 B 站取一份新的 SESSDATA，"
                    "写进 path.sh 的 BILI_SESSDATA 后重开",
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

    # ---- credential reality ----

    @property
    def logged_in(self) -> bool:
        """Whether the platform recognised us. False until init_room replies."""
        return self._connection_uid > 0

    @property
    def credential_stale(self) -> bool:
        """A SESSDATA was configured and the platform still handed back uid 0.

        The startup banner is printed before the connection exists and can only
        see whether a credential STRING was resolved, so it says "登录态" for an
        expired cookie too. This is the fact it is missing; read it once
        `status()["connected"]` turns true (or on the first event) and correct
        the banner. It stays false while the answer is unknown — an unconnected
        source accuses nobody.
        """
        return self._credential_checked and bool(self._sessdata) and self._connection_uid == 0

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
        if event.kind is EventKind.DANMAKU:
            event = dataclasses.replace(
                event,
                reply_to_anchor=(
                    event.reply_to_uid == self._room_owner_uid
                    if event.reply_to_uid > 0 and self._room_owner_uid > 0
                    else None
                ),
            )
        if (
            event.kind is EventKind.DANMAKU
            and self._room_owner_uid > 0
            and event.viewer.uid == self._room_owner_uid
        ):
            # The streamer typing in their own room. Marked, not dropped: the
            # assembly turns it into shared context instead of a reply, and
            # dropping it here would also hide it from memory and the panel.
            self._anchor_danmaku_marked += 1
            event = dataclasses.replace(
                event,
                viewer=dataclasses.replace(event.viewer, is_anchor=True),
            )
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
        # blivedm's own reconnection never reaches here — this fires when the
        # wire is down for good, so it is the moment the room went quiet.
        # `requested` separates our shutdown from a drop; an unrequested one
        # goes on to raise out of start() for the supervisor to log.
        log.info(
            "bilibili.client_stopped",
            requested=self._stop_requested,
            generation=self._generation,
            error_text=str(exception)[:200] if exception is not None else "",
        )
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
            # Not `bool(self._sessdata)`: that is the claim the startup banner
            # already makes wrongly. This one is the platform's answer.
            "logged_in": self.logged_in,
            "credential_stale": self.credential_stale,
            "owner_uid_known": self._room_owner_uid > 0,
            "anchor_danmaku_marked": self._anchor_danmaku_marked,
            "vendor_drift": self._vendor_drift,
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
