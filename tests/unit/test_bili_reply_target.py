"""Reply-target metadata must survive the real Web danmaku parser."""

from __future__ import annotations

import json

import pytest

from bilisama.clock import FakeClock
from bilisama.ingest.bilibili._vendor.blivedm.models import web as web_models
from bilisama.ingest.bilibili.source import BilibiliEventSource, event_from_danmaku
from bilisama.ingest.events import LiveEvent
from bilisama.ui.events import live_event_payload
from tests.fakes.bili import danmu_info


def _event(extra: object, *, text: str = "我也遇到了") -> LiveEvent:
    info = danmu_info(msg=text)
    info[0][15]["extra"] = extra
    return event_from_danmaku(
        web_models.DanmakuMessage.from_command(info), room_id=777, recv_at=1.0, generation=2
    )


@pytest.mark.parametrize("encoded", [False, True])
def test_reply_target_survives_parser_and_redaction(encoded: bool) -> None:
    extra = {"reply_mid": 3546731499228115, "reply_uname": "小松"}
    event = _event(json.dumps(extra) if encoded else extra).redacted()
    assert event.reply_to_uid == 3546731499228115
    assert event.reply_to_name == "小松"
    assert event.viewer.uid == 42
    assert event.text == "我也遇到了"


@pytest.mark.parametrize("uid", [0, -1, None, True, 1.5, "", "abc", "1.5", [], {}])
def test_invalid_target_uid_stays_unknown(uid: object) -> None:
    event = _event({"reply_mid": uid, "reply_uname": "小松"})
    assert event.reply_to_uid == 0
    assert event.reply_to_anchor is None


@pytest.mark.parametrize("extra", [None, "{broken", "null", "[]", "42", {}, []])
def test_missing_or_malformed_extra_does_not_drop_danmaku(extra: object) -> None:
    event = _event(extra, text="@小松 我也遇到了")
    assert event.reply_to_uid == 0
    assert event.reply_to_name == ""
    assert event.text == "@小松 我也遇到了"


def test_decimal_uid_is_parsed_without_using_name_as_identity() -> None:
    event = _event({"reply_mid": "123456", "reply_uname": ["错误类型"]})
    assert event.reply_to_uid == 123456
    assert event.reply_to_name == ""


@pytest.mark.parametrize(
    ("owner_uid", "target_uid", "expected"),
    [(42, 42, True), (42, 99, False), (42, 0, None), (0, 99, None), (0, 0, None)],
)
def test_source_matches_target_uid_and_keeps_event(
    owner_uid: int, target_uid: int, expected: bool | None
) -> None:
    source = BilibiliEventSource(777, FakeClock(), queue_size=4)
    source._room_owner_uid = owner_uid
    source.offer(_event({"reply_mid": target_uid, "reply_uname": "同一个昵称"}))
    queued = source._queue.get_nowait()
    assert queued is not None
    assert queued.reply_to_anchor is expected
    assert queued.reply_to_uid == target_uid
    payload = live_event_payload(queued)
    assert payload["reply_to_uid"] == target_uid
    assert payload["reply_to_name"] == "同一个昵称"
    assert payload["reply_to_anchor"] is expected
    assert source.status()["counts"] == {"danmaku": 1}
