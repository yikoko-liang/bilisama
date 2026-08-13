"""Shared builders for the Bilibili ingest suites.

Two families live here so the suites stop keeping private near-copies:

- Wire payloads (`danmu_info`, `gift_data`, `sc_data`, `toast_data`) in the
  exact shapes upstream's `from_command` parsers eat — the re-vendor
  tripwires drive real parsers, so these must stay faithful to the wire.
- LiveEvent factories (`gift_event`, `danmaku_event`, `entry_event`) built
  through the public constructors, with `cny_from_gold` doing the currency
  math so a rate change in product code fails these suites instead of being
  quietly re-hardcoded in three test files.
"""

from __future__ import annotations

from typing import Any

from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Medal,
    Viewer,
    cny_from_gold,
)

# ------------------------------------------------------------------ wire payloads


def danmu_info(
    *,
    uid: int = 42,
    uname: str = "阿强",
    msg: str = "主播今天玩什么",
    crc: str = "abc123ef",
    medal: bool = True,
    privilege: int = 3,
    admin: int = 1,
    wealth: int = 22,
) -> list[Any]:
    # Index layout verified against DanmakuMessage.from_command: info[0][1..15],
    # info[1] text, info[2][0..7], info[3] medal, info[4] level, info[5] titles,
    # info[7] privilege, info[16][0] wealth.
    info0 = [
        0,
        1,
        25,
        16777215,
        1755000000000,
        12345,
        0,
        crc,
        0,
        0,
        0,
        "",
        0,
        "{}",
        "{}",
        {"user": {"base": {"face": "http://face"}}},
    ]
    medal_block = [21, "小牌子", "某主播", 777, 0, ""] if medal else []
    return [
        info0,
        msg,
        [uid, uname, admin, 0, 0, 10000, 1, ""],
        medal_block,
        [50, 0, 9868950, ">50000"],
        ["", ""],
        0,
        privilege,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        [wealth],
    ]


def gift_data(**overrides: object) -> dict[str, Any]:
    data: dict[str, Any] = {
        "giftName": "小心心",
        "num": 5,
        "uname": "老板",
        "face": "",
        "guard_level": 0,
        "uid": 9,
        "timestamp": 1755000123,
        "giftId": 31036,
        "giftType": 0,
        "gift_info": {"img_basic": ""},
        "action": "赠送",
        "price": 5200,
        "rnd": "uuid-1",
        "coin_type": "gold",
        "total_coin": 26000,
        "tid": "tid-777",
    }
    data.update(overrides)
    return data


def sc_data() -> dict[str, Any]:
    return {
        "price": 30,
        "message": "能表演个节目吗",
        "message_trans": "",
        "start_time": 1755000200,
        "end_time": 1755000260,
        "time": 60,
        "id": 888001,
        "gift": {"gift_id": 12000, "gift_name": "醒目留言"},
        "uid": 77,
        "user_info": {"uname": "yiang", "face": "", "guard_level": 0, "user_level": 12},
        "background_bottom_color": "",
        "background_color": "",
        "background_icon": "",
        "background_image": "",
        "background_price_color": "",
        "medal_info": None,
    }


def toast_data(source: int = 0) -> dict[str, Any]:
    return {
        "sender_uinfo": {"uid": 55, "base": {"name": "新舰长"}},
        "guard_info": {"guard_level": 3, "start_time": 1755000300, "end_time": 1755000300},
        "pay_info": {"num": 1, "price": 198000, "unit": "月"},
        "gift_info": {"gift_id": 10003},
        "option": {"source": source},
        "toast_msg": "新舰长 在主播的直播间开通了舰长",
    }


# ------------------------------------------------------------------ event factories


def gift_event(
    *,
    uid: int = 9,
    gift_id: int = 31036,
    num: int = 1,
    coin: int = 100,
    coin_type: str = "gold",
    room_id: int = 777,
    event_id: str = "",
) -> LiveEvent:
    viewer = Viewer(uid=uid, name=f"老板{uid}")
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=room_id,
        viewer=viewer,
        gift=Gift(
            gift_id=gift_id,
            name="小心心",
            num=num,
            coin_type=coin_type,
            total_coin=coin,
            combo_id=f"{viewer.identity}:{gift_id}",
        ),
        value_cny=cny_from_gold(coin) if coin_type == "gold" else 0.0,
        event_id=event_id or f"gift:{uid}:{gift_id}:{coin}",
        ts_ms=1_755_000_000_000,
    )


def danmaku_event(
    text: str,
    uid: int = 1,
    *,
    room_id: int = 777,
    guard: GuardLevel = GuardLevel.NONE,
    medal_level: int = 0,
    medal_room: int = 777,
    admin: bool = False,
    user_level: int = 0,
    event_id: str = "",
) -> LiveEvent:
    viewer = Viewer(
        uid=uid,
        name=f"观众{uid}",
        guard_level=guard,
        is_admin=admin,
        user_level=user_level,
        medal=(
            Medal(name="牌子", level=medal_level, anchor_room_id=medal_room)
            if medal_level
            else None
        ),
    )
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=room_id,
        viewer=viewer,
        text=text,
        event_id=event_id or f"{uid}:{text}",
    )


def entry_event(uid: int, *, room_id: int = 777) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.ENTRY,
        room_id=room_id,
        viewer=Viewer(uid=uid, name=f"观众{uid}"),
        event_id=f"iw:{uid}",
    )
