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
