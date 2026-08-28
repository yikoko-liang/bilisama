"""The UI vocabulary is an append-only wire contract; pin it by value.

The pet page, the panel and (stage 5) a generated .d.ts all key on these exact
strings. A rename that would be a refactor anywhere else is a protocol break
here, so the snapshot tests below spell every value out — same discipline as
SkipReason.
"""

from __future__ import annotations

import json
from typing import Any

from bilisama.realtime import link
from bilisama.ui.events import ClientEvent, ServerEvent, frame, link_frames

# ------------------------------------------------------------ vocabulary


def test_server_vocabulary_is_pinned() -> None:
    assert [event.value for event in ServerEvent] == [
        "hello",
        "voice.state",
        "reply.delta",
        "reply.done",
        "transcript.final",
        "event.feed",
        "playback.clear",
        "log.line",
        "panel.state",
        "audio.owner",
        "audio.command",
        "audio.devices",
        "audio.level",
        "app.exiting",
    ]


def test_client_vocabulary_is_pinned() -> None:
    assert [event.value for event in ClientEvent] == [
        "pet.poke",
        "panel.set",
        "console.line",
        "playback.started",
        "playback.ended",
        "playback.cancelled",
        "audio.ask",
        "audio.report",
        "app.quit",
        "test.run",
        "test.stop",
    ]


# ------------------------------------------------------------ frame


def test_frame_shape_and_readable_chinese() -> None:
    line = frame(ServerEvent.REPLY_DELTA, {"text": "晚上好呀"})
    assert "晚上好呀" in line  # ensure_ascii=False; \u escapes are useless in devtools
    assert json.loads(line) == {"event": "reply.delta", "data": {"text": "晚上好呀"}}


def test_frame_stringifies_awkward_values_instead_of_raising() -> None:
    payload = json.loads(frame(ServerEvent.EVENT_FEED, {"status": link.ReplyStatus.COMPLETED}))
    assert payload["data"]["status"] == "completed"


# ------------------------------------------------------------ link translation


def _frames(
    event: link.LinkEvent, context: dict[str, Any] | None = None
) -> list[tuple[ServerEvent, dict[str, Any]]]:
    return list(link_frames(event, reply_context=context))


def test_reply_text_delta_becomes_reply_delta() -> None:
    handle = link.ReplyHandle()
    assert _frames(link.ReplyTextDelta(handle, "你好")) == [
        (
            ServerEvent.REPLY_DELTA,
            {"reply_id": handle.handle_id, "source": "voice", "text": "你好"},
        )
    ]


def test_reply_done_yields_done_plus_feed_entry() -> None:
    handle = link.ReplyHandle()
    done = link.ReplyDone(handle, link.ReplyStatus.CANCELLED, text="话说到一半")
    base = {
        "reply_id": handle.handle_id,
        "source": "voice",
        "status": "cancelled",
        "text": "话说到一半",
    }
    assert _frames(done) == [
        (ServerEvent.REPLY_DONE, base),
        (ServerEvent.EVENT_FEED, {"kind": "reply", **base}),
    ]


def test_user_transcript_done_yields_final_plus_feed_entry() -> None:
    assert _frames(link.UserTranscriptDone("今天玩什么")) == [
        (ServerEvent.TRANSCRIPT_FINAL, {"text": "今天玩什么"}),
        (ServerEvent.EVENT_FEED, {"kind": "transcript", "text": "今天玩什么"}),
    ]


def test_link_error_lands_in_the_feed() -> None:
    assert _frames(link.LinkError("connection_lost", "socket closed")) == [
        (
            ServerEvent.EVENT_FEED,
            {"kind": "error", "code": "connection_lost", "detail": "socket closed"},
        )
    ]


def test_audio_and_speech_edges_produce_nothing() -> None:
    """PCM never reaches the browser, and raw speech edges belong to the
    state poller — forwarding both would give the page two clocks."""
    handle = link.ReplyHandle()
    assert _frames(link.ReplyAudioDelta(handle, b"\x00\x01" * 480)) == []
    assert _frames(link.SpeechStarted()) == []
    assert _frames(link.SpeechStopped()) == []
    assert _frames(link.ReplyStarted(handle)) == []
    assert _frames(link.UserTranscriptDelta("说到一半")) == []


def test_reply_context_rides_both_reply_frames() -> None:
    """The scheduler resolves the trigger; these frames carry it so the chat
    page can render 「引用 弹幕 · 张三：…」 next to the reply."""
    from bilisama.ui.events import live_event_payload
    from tests.fakes.bili import danmaku_event

    handle = link.ReplyHandle()
    reference = live_event_payload(danmaku_event("显存怎么算", uid=3))
    context = {"source": "danmaku", "reference": reference}
    frames = _frames(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="是这样"), context)
    for _name, payload in frames:
        assert payload["source"] == "danmaku"
        assert payload["reference"]["name"] == "观众3"
        assert payload["reference"]["text"] == "显存怎么算"


def test_live_event_payload_carries_identity_and_battery_but_no_raw() -> None:
    from bilisama.ui.events import live_event_payload
    from tests.fakes.bili import gift_event

    payload = live_event_payload(gift_event(uid=9, coin=15_000, num=1))
    assert payload["kind"] == "gift"
    assert payload["gift"]["total_battery"] == 150
    assert payload["identity"] == "uid:9"
    assert "raw" not in payload
