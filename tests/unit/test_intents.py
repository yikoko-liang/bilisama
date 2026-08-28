"""What intents.py writes down while it rules on an event.

The ruling itself is pinned elsewhere — the wrapper and the TTL in
test_director.py, the gift ladder in test_bili_peripherals.py. What is pinned
here is that the ruling leaves a line, because the panel's log page is where
「这条弹幕后来怎么了」 gets answered and the story has to start at the moment
the intent existed. The scheduler's verdict is the other end of it
(director/scheduler.py:290); these two join on intent_id.
"""

from __future__ import annotations

import logging

import pytest

from bilisama.director.intents import burst_welcome_intent, intent_for
from bilisama.ingest.events import EventKind, LiveEvent, Viewer

_BODY = "主播今天玩什么游戏"


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    """Every record carrying one event name, in order."""
    return [record for record in caplog.records if record.getMessage() == event]


def _fields(record: logging.LogRecord) -> dict[str, object]:
    fields = getattr(record, "fields", {})
    assert isinstance(fields, dict)
    return fields


def _danmaku(text: str = _BODY) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=777,
        viewer=Viewer(uid=42, name="阿强"),
        text=text,
        event_id="dm:12345",
    )


def test_a_danmaku_intent_records_the_whole_ruling(caplog: pytest.LogCaptureFixture) -> None:
    """Rung, trust, requeue and shelf life — the four things this module decides.

    Debug, not info: one line per speaking event is danmaku volume, and info is
    reserved for the handful of decisions a turn is supposed to leave behind.
    """
    caplog.set_level(logging.DEBUG)

    intent = intent_for(_danmaku(), now=10.0)

    assert intent is not None
    (record,) = _lines(caplog, "intents.built")
    assert record.levelno == logging.DEBUG
    fields = _fields(record)
    assert fields["source"] == "danmaku"
    assert fields["priority"] == "DANMAKU"
    assert fields["trusted"] is False
    assert fields["requeue_on_interrupt"] is False
    assert fields["ttl_ms"] == 20_000, "没记下这条还剩多久就不值得说了"


def test_a_paid_intent_says_it_requeues_and_never_goes_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two fields that carry the revenue rule (director/intent.py:7-10).

    A thank-you an interruption swallowed is a revenue bug, so the line has to
    say which of the two shapes this intent got.
    """
    caplog.set_level(logging.DEBUG)

    intent_for(
        LiveEvent(
            kind=EventKind.SUPER_CHAT,
            room_id=777,
            viewer=Viewer(uid=7, name="老板"),
            text=_BODY,
            value_cny=30.0,
            event_id="sc:9",
        ),
        now=0.0,
    )

    fields = _fields(_lines(caplog, "intents.built")[0])
    assert fields["priority"] == "SUPERCHAT"
    assert fields["requeue_on_interrupt"] is True
    assert fields["ttl_ms"] is None, "付费意图不过期，日志里要看得出来"


def test_the_danmaku_body_never_reaches_the_line(caplog: pytest.LogCaptureFixture) -> None:
    """The audience wrote that sentence; it is not ours to file away.

    Checked on the raw fields rather than on the formatted line, because
    obs/logging.py's folding is a second line of defence — the body should not
    reach the log call in the first place.
    """
    caplog.set_level(logging.DEBUG)

    intent_for(_danmaku(), now=0.0)

    records = _lines(caplog, "intents.built")
    assert records, "先得有这条日志，这个检查才有意义"
    for record in records:
        for name, value in _fields(record).items():
            assert _BODY not in str(value), f"{name} 把观众正文带进了日志"


def test_a_feed_only_kind_says_why_it_will_never_speak(caplog: pytest.LogCaptureFixture) -> None:
    """「我关注了怎么一点反应都没有」 — because follow has no speaking path.

    Knowing is not speaking (plan section 2.7): these kinds reach memory and
    the panel and stop there. Without this line the event simply vanishes.
    """
    caplog.set_level(logging.DEBUG)

    assert intent_for(LiveEvent(kind=EventKind.FOLLOW, room_id=777), now=0.0) is None

    (record,) = _lines(caplog, "intents.no_speaking_path")
    assert record.levelno == logging.DEBUG
    assert _fields(record)["kind"] == "follow"
    assert _lines(caplog, "intents.built") == [], "没造出意图却记了一条 built"


def test_the_burst_welcome_uses_the_same_event_name(caplog: pytest.LogCaptureFixture) -> None:
    """One vocabulary for "an intent was built", told apart by source.

    The entry lane's one voice is this batched hello, so it is the line that
    shows the lane is alive at all.
    """
    caplog.set_level(logging.DEBUG)

    burst_welcome_intent(5, now=100.0)

    fields = _fields(_lines(caplog, "intents.built")[0])
    assert fields["source"] == "entry"
    assert fields["priority"] == "DANMAKU", "打招呼不许打断别人，日志里也该是这个rung"


def test_the_default_level_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Boundary: at info — the panel's default filter — this module says nothing.

    That is the whole reason these are debug. A busy room builds an intent every
    few hundred milliseconds; if any of it showed at info the decisions worth
    reading would be buried.
    """
    caplog.set_level(logging.INFO)

    intent_for(_danmaku(), now=0.0)
    intent_for(LiveEvent(kind=EventKind.LIKE, room_id=777), now=0.0)

    assert caplog.records == [], f"info 档不该有 intents 的日志：{caplog.records}"


# ------------------------------------------------------------ per-kind rules


def test_danmaku_reply_repeats_the_question_before_answering() -> None:
    """A listener hears only audio: without the question restated, the answer
    floats free of whatever it answers."""
    intent = intent_for(_danmaku("显存爆了是为什么？"), now=0.0)
    assert intent is not None
    rules = intent.injection.reply.instructions or ""
    assert "重复一遍观众的问题" in rules
    assert "先判断弹幕是在对主播、对你" in rules


def test_danmaku_instruction_answers_with_its_own_judgment() -> None:
    intent = intent_for(_danmaku(), now=0.0)
    assert intent is not None
    rules = intent.injection.reply.instructions or ""
    assert "不要默认让主播回答" in rules
    assert "这得问主播" in rules, "the banned phrasing is named, not implied"


def test_amounts_never_enter_any_paid_prompt() -> None:
    """The stronger guarantee: the model cannot leak a number it never saw."""
    sc = LiveEvent(
        kind=EventKind.SUPER_CHAT,
        room_id=777,
        viewer=Viewer(uid=7, name="金主"),
        text="能出教程吗",
        value_cny=520.0,
        event_id="sc:9",
    )
    intent = intent_for(sc, now=0.0)
    assert intent is not None
    assert "520" not in (intent.injection.item_text or "")
    assert "严禁说出、换算、暗示或比较 SC 金额" in (intent.injection.reply.instructions or "")


def test_guard_buy_line_and_ceremony_scale_with_tier() -> None:
    from bilisama.ingest.events import GuardLevel

    def _guard(level: GuardLevel) -> LiveEvent:
        return LiveEvent(
            kind=EventKind.GUARD_BUY,
            room_id=777,
            viewer=Viewer(uid=8, name="新舰长", guard_level=level),
            value_cny=198.0,
            event_id=f"guard:{level.value}",
        )

    captain = intent_for(_guard(GuardLevel.CAPTAIN), now=0.0)
    governor = intent_for(_guard(GuardLevel.GOVERNOR), now=0.0)
    assert captain is not None and governor is not None
    assert "[上舰·舰长]" in (captain.injection.item_text or "")
    assert "[上舰·总督]" in (governor.injection.item_text or "")
    assert "最高一档" in (governor.injection.reply.instructions or "")
    assert "不背诵会员权益" in (captain.injection.reply.instructions or "")


def test_vip_welcome_knows_the_verified_tier_and_varies_its_phrasing() -> None:
    from bilisama.ingest.events import GuardLevel, Medal

    captain = LiveEvent(
        kind=EventKind.VIP_ENTER,
        room_id=777,
        viewer=Viewer(uid=9, name="老观众", guard_level=GuardLevel.CAPTAIN),
        event_id="vip:1",
    )
    medal = LiveEvent(
        kind=EventKind.VIP_ENTER,
        room_id=777,
        viewer=Viewer(uid=10, name="铁粉", medal=Medal(name="豆腐", level=7, anchor_room_id=777)),
        event_id="vip:2",
    )
    a = intent_for(captain, now=0.0)
    b = intent_for(medal, now=0.0)
    assert a is not None and b is not None
    assert "[进房·舰长]" in (a.injection.item_text or "")
    assert "[进房·本房粉丝牌 7 级]" in (b.injection.item_text or "")
    rules = a.injection.reply.instructions or ""
    assert "最近三次进房回复" in rules, "phrasing is checked against history"
    assert "不要提消费记录" in rules


def test_entry_welcome_names_one_and_merges_many() -> None:
    from bilisama.director.intents import entry_welcome_intent
    from tests.fakes.bili import entry_event

    single = entry_welcome_intent((entry_event(1),), now=10.0)
    group = entry_welcome_intent((entry_event(1), entry_event(2), entry_event(3)), now=20.0)
    assert "点名欢迎" in (single.injection.reply.instructions or "")
    assert "不要播报、暗示或猜测人数" in (group.injection.reply.instructions or "")
    assert (group.injection.item_text or "").count("[进房]") == 3
    assert single.priority.name == "DANMAKU", "a hello queues, it never preempts"
    assert single.dedup_key != group.dedup_key


def test_anchor_danmaku_becomes_shared_context_not_a_reply() -> None:
    from bilisama.director.intents import anchor_danmaku_context_item

    anchor = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=777,
        viewer=Viewer(uid=42, name="主播本人", is_anchor=True),
        text="等下测试一下新场景",
        event_id="dm:a1",
    )
    item = anchor_danmaku_context_item(anchor)
    assert "[主播弹幕] 主播本人: 等下测试一下新场景" in item
    assert "不需要单独回复" in item

    with pytest.raises(ValueError):
        anchor_danmaku_context_item(_danmaku())
