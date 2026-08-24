"""blivedm → LiveEvent mapping, driven through upstream's own parsers.

Every payload here goes through `from_command` (or the protobuf round trip)
on purpose: a quarterly re-vendor that changes upstream's parsing behaviour
must trip these before it reaches a live room (VENDOR.md's promise).
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
from tests.fakes.bili import danmu_info as _danmu_info
from tests.fakes.bili import gift_data as _gift_data
from tests.fakes.bili import sc_data as _sc_data
from tests.fakes.bili import toast_data as _toast_data

_KW: dict[str, Any] = {"room_id": 777, "recv_at": 1.0, "generation": 2}

# ------------------------------------------------------------------ the re-vendor alarm

_VENDOR_MD = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "bilisama"
    / "ingest"
    / "bilibili"
    / "_vendor"
    / "blivedm"
    / "VENDOR.md"
)
# Plan section 5.1's cadence, in days. One quarter, counted from the date the
# snapshot in VENDOR.md was taken.
_REVENDOR_PERIOD_DAYS = 92


def _vendored_on(text: str) -> date:
    """The snapshot date VENDOR.md records, as a date."""
    match = re.search(r"^- Vendored: (\d{4})-(\d{2})-(\d{2})", text, re.MULTILINE)
    if match is None:
        raise AssertionError("VENDOR.md 里读不到 `- Vendored: YYYY-MM-DD` 那一行，到期提醒失灵了")
    return date(int(match[1]), int(match[2]), int(match[3]))


def test_vendor_md_records_a_readable_snapshot_date() -> None:
    assert _vendored_on(_VENDOR_MD.read_text(encoding="utf-8")) >= date(2026, 8, 13)


def test_an_unreadable_vendor_date_fails_loudly_instead_of_silently_never_firing() -> None:
    """Error path: the line the alarm reads gets reworded. Swallowing that
    would leave a reminder that can never fire again, which is the state this
    whole alarm exists to end."""
    with pytest.raises(AssertionError, match=re.escape("VENDOR.md")):
        _vendored_on("- Vendored: 上个月，大概吧\n")


def test_quarterly_revendor_is_not_overdue() -> None:
    """The trigger for "每季度 re-vendor" (plan section 5.1).

    Before this, the discipline existed only as prose in VENDOR.md: nothing
    would say a word on the day it came due. A dated test is the one alarm
    that needs neither CI nor a calendar entry — it rings inside the gate the
    next person runs. Going red here is not a bug report, it is the reminder;
    clear it by doing the re-vendor (VENDOR.md's four steps) and updating the
    date, not by widening the window.
    """
    vendored = _vendored_on(_VENDOR_MD.read_text(encoding="utf-8"))
    due = vendored + timedelta(days=_REVENDOR_PERIOD_DAYS)
    assert date.today() <= due, (
        f"该 re-vendor blivedm 了：上次快照 {vendored}，{due} 到期。"
        "按 VENDOR.md「How to re-vendor」四步走完，把新 sha 和日期写回 VENDOR.md，"
        "然后跑本文件的映射测试确认上游行为没变。"
    )


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


def test_the_wire_never_fills_the_platform_combo_fields() -> None:
    """Pins the "NO WIRE WRITER" marker on Gift.combo_count / combo_end.

    Both fields exist because the replay fixtures record what arrived, and
    both stay at their defaults on every real mapping — the aggregator settles
    on its idle timer, not on the platform's combo_end. Without this, the next
    reader sees two populated-looking fields on a public model and builds a
    combo counter on data that is never there in production.
    """
    for message in (
        web_models.GiftMessage.from_command(_gift_data()),
        web_models.GiftMessage.from_command(_gift_data(coin_type="silver", total_coin=990)),
    ):
        gift = event_from_gift(message, **_KW).gift
        assert gift is not None
        assert gift.combo_count == 0
        assert gift.combo_end is None


def test_silver_gift_is_worth_nothing_and_not_paid() -> None:
    message = web_models.GiftMessage.from_command(_gift_data(coin_type="silver", total_coin=990))
    event = event_from_gift(message, **_KW)
    assert event.value_cny == 0.0
    assert not event.is_paid


def test_super_chat_price_is_already_cny() -> None:
    message = web_models.SuperChatMessage.from_command(_sc_data())
    event = event_from_super_chat(message, **_KW)
    assert event.kind is EventKind.SUPER_CHAT
    assert event.value_cny == 30.0, "SC price must NOT be divided by 1000"
    assert event.text == "能表演个节目吗"
    assert event.event_id == "sc:888001", "the id is the deletion handle"
    assert event.is_paid


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


def test_upstream_still_names_only_the_three_verified_msg_types() -> None:
    """Guards the 待验证 marker on msg_type 4/5/6 (ledger item 50).

    Upstream names 1/2/3 and stops (pb.py's InteractWordV2MsgType); our
    special-follow / mutual-follow / like readings above that are a guess. The
    day a re-vendor adds a name for 4, 5 or 6, this goes red and somebody gets
    to confirm the guess or fix it — which is cheaper than the alternative on
    offer, sitting in a live room counting msg_types.
    """
    named = {member.value: member.name for member in pb.InteractWordV2MsgType}
    assert named == {0: "Unknown", 1: "EnterRoom", 2: "Follow", 3: "ShareRoom"}


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
    assert source._queue.qsize() == 2, "the danmaku plus the paid wake token"


def test_full_queue_sheds_the_oldest_and_keeps_the_account() -> None:
    source, _clock = _source()
    for i in range(6):
        info = _danmu_info(uid=100 + i, msg=f"弹幕{i}", medal=False, privilege=0, admin=0)
        source.offer(event_from_danmaku(web_models.DanmakuMessage.from_command(info), **_KW))
    assert source._queue.qsize() == 4
    assert source.status()["dropped"] == 2
    oldest_left = source._queue.get_nowait()
    assert oldest_left is not None and oldest_left.viewer.uid == 102, "the two oldest were shed"


def test_two_masked_guard_buyers_in_one_second_both_survive() -> None:
    """Masking sets uid 0 and start_time is only second-accurate, so keying the
    merge on uid+second made the second ¥198 purchase of that second look like
    the toast/legacy double-send and vanish unthanked."""
    source, _ = _source()
    first = event_from_guard_buy(
        web_models.GuardBuyMessage.from_command(
            {
                "uid": 0,
                "username": "阿***",
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
    second = event_from_guard_buy(
        web_models.GuardBuyMessage.from_command(
            {
                "uid": 0,
                "username": "小***",  # a different masked buyer, same second
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
    assert first.event_id != second.event_id
    source.offer(first)
    source.offer(second)
    assert len(source._paid) == 2, "两位打码买家在同一秒各买各的，不能互相吞掉"


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


def test_gift_with_no_transaction_id_falls_back_to_the_identity_key() -> None:
    """An empty tid/rnd must NOT mint the constant key "gift:" — that one
    truthy string would make every viewer's gift a duplicate of every other's
    (the SEND_GIFT_V2 protobuf path can leave both fields unset)."""
    message = web_models.GiftMessage.from_command(_gift_data(tid="", rnd=""))
    event = event_from_gift(message, **_KW)
    assert event.event_id == ""
    assert event.dedup_key.startswith("gift:uid:9:"), "identity+bucket fallback engaged"


def test_masked_guard_buyers_do_not_merge_each_other_away() -> None:
    """Two DIFFERENT masked buyers share the "anon" identity; only the
    purchase timestamp tells them apart, so the merge key must include it."""
    source, _clock = _source()
    first = event_from_guard_buy(
        web_models.GuardBuyMessage.from_command(
            {
                "uid": 0,
                "username": "***",
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
    second = event_from_guard_buy(
        web_models.GuardBuyMessage.from_command(
            {
                "uid": 0,
                "username": "***",
                "guard_level": 3,
                "num": 1,
                "price": 198000,
                "gift_id": 10003,
                "gift_name": "舰长",
                "start_time": 1755000310,  # ten seconds later: a different person
                "end_time": 1755000310,
            }
        ),
        **_KW,
    )
    source.offer(first)
    source.offer(second)
    assert len(source._paid) == 2, "distinct purchases both survive"
