"""blivedm → LiveEvent mapping, driven through upstream's own parsers.

Every payload here goes through `from_command` (or the protobuf round trip)
on purpose: a quarterly re-vendor that changes upstream's parsing behaviour
must trip these before it reaches a live room (VENDOR.md's promise).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

from bilisama.clock import FakeClock
from bilisama.ingest.bilibili._vendor.blivedm.models import pb
from bilisama.ingest.bilibili._vendor.blivedm.models import web as web_models
from bilisama.ingest.bilibili.source import (
    BilibiliEventSource,
    event_from_danmaku,
    event_from_gift,
    event_from_guard_buy,
    event_from_interact,
    event_from_super_chat,
    event_from_user_toast,
)
from bilisama.ingest.events import EventKind, GuardLevel

_KW: dict[str, Any] = {"room_id": 777, "recv_at": 1.0, "generation": 2}


def _danmu_info(
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


def test_danmaku_maps_identity_medal_and_millisecond_timestamp() -> None:
    message = web_models.DanmakuMessage.from_command(_danmu_info())
    event = event_from_danmaku(message, **_KW)
    assert event.kind is EventKind.DANMAKU
    assert event.text == "主播今天玩什么"
    assert event.room_id == 777
    assert event.session_generation == 2
    assert event.ts_ms == 1755000000000, "DANMU_MSG timestamps are already milliseconds"
    assert event.event_id == "dm:12345"
    v = event.viewer
    assert v.uid == 42 and v.uid_hash == "abc123ef"
    assert v.guard_level is GuardLevel.CAPTAIN and v.is_admin
    assert v.user_level == 50 and v.wealth_level == 22
    assert v.medal is not None and v.medal.is_this_room(777)


def test_masked_danmaku_keeps_the_event_and_falls_back_to_the_hash() -> None:
    """The first commandment: uid=0 never drops an event (plan section 5.2)."""
    message = web_models.DanmakuMessage.from_command(
        _danmu_info(uid=0, uname="***", crc="deadbeef", medal=False, privilege=0, admin=0)
    )
    event = event_from_danmaku(message, **_KW)
    assert event.is_anonymous
    assert event.viewer.identity == "hash:deadbeef"
    assert event.viewer.medal is None


def _gift_data(**overrides: object) -> dict[str, Any]:
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


def test_gold_gift_derives_cny_and_merges_on_tid() -> None:
    message = web_models.GiftMessage.from_command(_gift_data())
    event = event_from_gift(message, **_KW)
    assert event.kind is EventKind.GIFT
    assert event.value_cny == 26.0, "1000 gold == 1 CNY, from total_coin"
    assert event.event_id == "gift:tid-777", "tid is the V1/V2 merge key"
    assert event.ts_ms == 1755000123000, "gift timestamps are seconds, scaled to ms"
    assert event.gift is not None
    assert event.gift.combo_id == "uid:9:31036", "synthesised: same viewer, same gift"
    assert event.is_paid


def test_silver_gift_is_worth_nothing_and_not_paid() -> None:
    message = web_models.GiftMessage.from_command(_gift_data(coin_type="silver", total_coin=990))
    event = event_from_gift(message, **_KW)
    assert event.value_cny == 0.0
    assert not event.is_paid


def _sc_data() -> dict[str, Any]:
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


def test_super_chat_price_is_already_cny() -> None:
    message = web_models.SuperChatMessage.from_command(_sc_data())
    event = event_from_super_chat(message, **_KW)
    assert event.kind is EventKind.SUPER_CHAT
    assert event.value_cny == 30.0, "SC price must NOT be divided by 1000"
    assert event.text == "能表演个节目吗"
    assert event.event_id == "sc:888001", "the id is the deletion handle"
    assert event.is_paid


def _toast_data(source: int = 0) -> dict[str, Any]:
    return {
        "sender_uinfo": {"uid": 55, "base": {"name": "新舰长"}},
        "guard_info": {"guard_level": 3, "start_time": 1755000300, "end_time": 1755000300},
        "pay_info": {"num": 1, "price": 198000, "unit": "月"},
        "gift_info": {"gift_id": 10003},
        "option": {"source": source},
        "toast_msg": "新舰长 在主播的直播间开通了舰长",
    }


def test_guard_toast_converts_gold_seeds_and_keeps_source_zero() -> None:
    message = web_models.UserToastV2Message.from_command(_toast_data(source=0))
    event = event_from_user_toast(message, **_KW)
    assert event is not None
    assert event.kind is EventKind.GUARD_BUY
    assert event.value_cny == 198.0, "toast price is gold seeds per unit"
    assert event.viewer.guard_level is GuardLevel.CAPTAIN


def test_guard_toast_source_two_is_the_hidden_duplicate() -> None:
    message = web_models.UserToastV2Message.from_command(_toast_data(source=2))
    assert event_from_user_toast(message, **_KW) is None


def test_legacy_guard_buy_maps_the_same_shape() -> None:
    message = web_models.GuardBuyMessage.from_command(
        {
            "uid": 55,
            "username": "新舰长",
            "guard_level": 3,
            "num": 1,
            "price": 198000,
            "gift_id": 10003,
            "gift_name": "舰长",
            "start_time": 1755000300,
            "end_time": 1755000300,
        }
    )
    event = event_from_guard_buy(message, **_KW)
    assert event.kind is EventKind.GUARD_BUY
    assert event.value_cny == 198.0
    assert event.event_id == "guard:55:1755000300", "same id shape as the toast, for merging"


def _interact_message(msg_type: int) -> object:
    raw = pb.InteractWordV2(uid=66, uname="路过", msg_type=msg_type, timestamp=1755000400).dumps()
    return web_models.InteractWordV2Message.from_command({"pb": base64.b64encode(raw).decode()})


def test_interact_word_v2_splits_by_msg_type_through_real_protobuf() -> None:
    cases = {
        1: EventKind.ENTRY,
        2: EventKind.FOLLOW,
        3: EventKind.SHARE,
        4: EventKind.FOLLOW,
        5: EventKind.FOLLOW,
        6: EventKind.LIKE,
    }
    for msg_type, kind in cases.items():
        event = event_from_interact(_interact_message(msg_type), **_KW)
        assert event is not None and event.kind is kind, f"msg_type={msg_type}"
    assert event_from_interact(_interact_message(99), **_KW) is None, "unknown types stay silent"


# ------------------------------------------------------------------ source queues


def _source() -> tuple[BilibiliEventSource, FakeClock]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    return BilibiliEventSource(777, clock, queue_size=4), clock


def test_paid_events_take_the_side_pocket_and_drain_first() -> None:
    source, _clock = _source()
    danmaku = event_from_danmaku(web_models.DanmakuMessage.from_command(_danmu_info()), **_KW)
    sc = event_from_super_chat(web_models.SuperChatMessage.from_command(_sc_data()), **_KW)
    source.offer(danmaku)
    source.offer(sc)
    assert list(source._paid) == [sc]
    assert source._queue.qsize() == 1


def test_full_queue_sheds_the_oldest_and_keeps_the_account() -> None:
    source, _clock = _source()
    for i in range(6):
        info = _danmu_info(uid=100 + i, msg=f"弹幕{i}", medal=False, privilege=0, admin=0)
        source.offer(event_from_danmaku(web_models.DanmakuMessage.from_command(info), **_KW))
    assert source._queue.qsize() == 4
    assert source.status()["dropped"] == 2
    oldest_left = source._queue.get_nowait()
    assert oldest_left.viewer.uid == 102, "the two oldest were shed"


def test_guard_double_send_merges_within_the_window() -> None:
    source, clock = _source()
    toast = event_from_user_toast(web_models.UserToastV2Message.from_command(_toast_data()), **_KW)
    assert toast is not None
    legacy = event_from_guard_buy(
        web_models.GuardBuyMessage.from_command(
            {
                "uid": 55,
                "username": "新舰长",
                "guard_level": 3,
                "num": 1,
                "price": 198000,
                "gift_id": 10003,
                "gift_name": "舰长",
                "start_time": 1755000300,
                "end_time": 1755000300,
            }
        ),
        **_KW,
    )
    source.offer(toast)
    source.offer(legacy)  # inside the 30s window: the legacy duplicate is merged away
    assert len(source._paid) == 1
    clock._now += 31.0
    source.offer(legacy)  # a genuinely new purchase later goes through
    assert len(source._paid) == 2
