"""事件模型与回放源。

重点是那条会静默毁掉整个产品的规则：**匿名观众不能被丢弃**。
"""

from __future__ import annotations

import pytest

from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Viewer,
    cny_from_gold,
    is_vip_entry,
)
from bilisama.ingest.sources import QueueSource, collect
from tests.fakes.replay import ReplaySource, fixture, read_fixture

# ------------------------------------------------------------ 身份


def test_masked_uid_still_has_a_usable_identity() -> None:
    """B 站隐私掩码下 uid 就是 0。这时 uid_hash 才是稳定标识。

    N.E.K.O 那句 `if not uid or uid == "0": return` 会把整条弹幕流静音掉。
    """
    masked = Viewer(uid=0, uid_hash="ab12", name="***")
    assert masked.is_anonymous
    assert masked.identity == "hash:ab12"
    assert masked.identity, "身份 key 永远不能为空，否则去重和记忆全废"


def test_identity_prefers_uid_when_present() -> None:
    assert Viewer(uid=42, uid_hash="ab12").identity == "uid:42"


def test_identity_falls_back_to_anon_only_when_nothing_available() -> None:
    assert Viewer().identity == "anon"


def test_display_name_never_empty() -> None:
    assert Viewer(uid=1).display_name == "一位观众"
    assert Viewer(uid=1, name="阿强").display_name == "阿强"


# ------------------------------------------------------------ 货币口径


@pytest.mark.parametrize(("coin", "cny"), [(1000, 1.0), (20000, 20.0), (198000, 198.0), (0, 0.0)])
def test_gold_to_cny(coin: int, cny: float) -> None:
    assert cny_from_gold(coin) == cny


def test_silver_gift_is_not_paid() -> None:
    assert not Gift(coin_type="silver", total_coin=500).is_paid
    assert Gift(coin_type="gold", total_coin=1000).is_paid


# ------------------------------------------------------------ 去重与脱敏


def test_dedup_key_uses_event_id_when_available() -> None:
    e = LiveEvent(kind=EventKind.DANMAKU, event_id="x1", text="666")
    assert e.dedup_key == "danmaku:x1"


def test_dedup_key_falls_back_without_event_id() -> None:
    """没有 event_id 时也要能去重，否则重连后会重复回应。"""
    v = Viewer(uid=7)
    a = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=1500)
    b = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=1900)
    assert a.dedup_key == b.dedup_key  # 同一秒内同一个人说同样的话
    c = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=2500)
    assert a.dedup_key != c.dedup_key


def test_redacted_drops_raw() -> None:
    """raw 是未经清洗的平台负载，绝对不能进 prompt。"""
    e = LiveEvent(kind=EventKind.DANMAKU, text="嗨", raw={"cmd": "DANMU_MSG", "info": [1, 2]})
    assert e.raw is not None
    assert e.redacted().raw is None
    assert e.redacted().text == "嗨"  # 别的字段不能丢


def test_redacted_is_cheap_when_already_clean() -> None:
    e = LiveEvent(kind=EventKind.DANMAKU, text="嗨")
    assert e.redacted() is e


# ------------------------------------------------------------ VIP 判定


def test_guard_makes_a_vip_entry() -> None:
    assert is_vip_entry(Viewer(uid=1, guard_level=GuardLevel.CAPTAIN))
    assert not is_vip_entry(Viewer(uid=1))


def test_past_spending_makes_a_vip_entry() -> None:
    """送过钱的观众进房也该点名。需求第 5 条要的是这个，不只是舰长。"""
    assert is_vip_entry(Viewer(uid=1), lifetime_gift_cny=30.0)


def test_guard_level_from_wire() -> None:
    assert GuardLevel.from_wire(3) is GuardLevel.CAPTAIN
    assert GuardLevel.from_wire(1) is GuardLevel.GOVERNOR
    assert GuardLevel.from_wire(0) is GuardLevel.NONE
    assert GuardLevel.from_wire(99) is GuardLevel.NONE  # 未知等级不该炸


# ------------------------------------------------------------ 回放


def test_every_fixture_parses() -> None:
    """七个 fixture 全部能解析。fixture 坏了会让一整批测试假绿。"""
    names = [
        "quiet_stream.jsonl",
        "superchat_during_speech.jsonl",
        "gift_combo.jsonl",
        "event_flood.jsonl",
        "presence.jsonl",
        "anonymous_masked.jsonl",
        "returning_viewer.jsonl",
        "injection_attempt.jsonl",
    ]
    for name in names:
        events = list(read_fixture(fixture(name)))
        assert events, f"{name} 是空的"
        assert all(isinstance(e, LiveEvent) for _, e in events)


def test_anonymous_fixture_yields_usable_identities() -> None:
    """全 uid=0 的那份 fixture，每条都要有可用身份，而且能区分出不同的人。"""
    events = [e for _, e in read_fixture(fixture("anonymous_masked.jsonl"))]
    assert all(e.is_anonymous for e in events)
    assert all(e.viewer.identity != "anon" for e in events)
    assert len({e.viewer.identity for e in events}) > 1, "掩码后也要能区分出不同的人"


def test_gift_value_derived_from_gold() -> None:
    events = [e for _, e in read_fixture(fixture("gift_combo.jsonl"))]
    assert all(e.value_cny == 1.0 for e in events)
    assert events[-1].gift is not None and events[-1].gift.combo_end is True


def test_flood_fixture_has_paid_events_mixed_in() -> None:
    """洪水里要混着付费事件,验的就是付费不会输给窗口竞争。"""
    events = [e for _, e in read_fixture(fixture("event_flood.jsonl"))]
    assert len(events) > 150
    assert any(e.is_paid for e in events)


async def test_replay_source_emits_in_order() -> None:
    source = ReplaySource(path=fixture("gift_combo.jsonl"), speed=0)
    events = await collect(source, limit=4)
    assert [e.event_id for e in events] == ["g1", "g2", "g3", "g4"]


async def test_replay_source_keeps_relative_order_under_real_clock() -> None:
    """speed 是倍速不是"忽略时间"。验窗口逻辑的测试要保留相对间隔。"""
    source = ReplaySource(path=fixture("superchat_during_speech.jsonl"), speed=2000.0)
    events = await collect(source, limit=3)
    assert [e.kind for e in events] == [
        EventKind.DANMAKU,
        EventKind.SUPER_CHAT,
        EventKind.DANMAKU,
    ]


async def test_queue_source_round_trip() -> None:
    source = QueueSource()
    await source.push(LiveEvent(kind=EventKind.DANMAKU, text="嗨", event_id="q1"))
    events = await collect(source, limit=1)
    assert events[0].event_id == "q1"


def test_event_kind_covers_the_speak_switches() -> None:
    """事件枚举和 speak 开关必须一一对上。两边各写各的就是 bug 的温床。"""
    from bilisama.config import SpeakSwitches

    switches = set(SpeakSwitches.model_fields)
    # proactive 和 background_result 不是直播事件，是内部源
    switches -= {"proactive", "background_result"}
    kinds = {k.value for k in EventKind} - {"room_state"}
    assert (
        switches == kinds
    ), f"开关和事件枚举对不上：只在开关里 {switches - kinds}，只在枚举里 {kinds - switches}"
