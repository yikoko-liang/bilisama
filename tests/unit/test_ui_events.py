"""The UI vocabulary is an append-only wire contract; pin it by value.

The pet page, the panel and (stage 5) a generated .d.ts all key on these exact
strings. A rename that would be a refactor anywhere else is a protocol break
here, so the snapshot tests below spell every value out — same discipline as
SkipReason.
"""

from __future__ import annotations

import json

from bilisama.ingest.events import EventKind, Gift, GuardLevel, LiveEvent, Medal, Viewer
from bilisama.realtime import link
from bilisama.ui.events import ClientEvent, ServerEvent, frame, link_frames, live_event_payload

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
        "audio.level",
        "live_mock.state",
        "live_mock.event",
    ]


def test_client_vocabulary_is_pinned() -> None:
    assert [event.value for event in ClientEvent] == [
        "pet.poke",
        "panel.set",
        "console.line",
        "test.run",
        "test.stop",
        "live_mock.check",
        "live_mock.start",
        "live_mock.stop",
        "live_mock.capture_stop",
        "audio.chunk",
        "app.quit",
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


def _frames(event: link.LinkEvent) -> list[tuple[ServerEvent, dict[str, object]]]:
    return list(link_frames(event))


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
    payload = {
        "reply_id": handle.handle_id,
        "source": "voice",
        "status": "cancelled",
        "text": "话说到一半",
    }
    assert _frames(done) == [
        (ServerEvent.REPLY_DONE, payload),
        (ServerEvent.EVENT_FEED, {"kind": "reply", **payload}),
    ]


def test_reply_context_is_copied_to_delta_done_and_chat_feed() -> None:
    handle = link.ReplyHandle()
    context = {"source": "gift", "reference": {"kind": "gift", "name": "阿强"}}
    delta = list(link_frames(link.ReplyTextDelta(handle, "谢谢"), reply_context=context))
    done = list(
        link_frames(
            link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text="谢谢"),
            reply_context=context,
        )
    )
    assert delta[0][1]["source"] == "gift"
    assert delta[0][1]["reference"] == context["reference"]
    assert done[0][1]["reference"] == context["reference"]
    assert done[1][1]["reference"] == context["reference"]


def test_live_event_payload_keeps_available_viewer_identity() -> None:
    event = LiveEvent(
        kind=EventKind.GIFT,
        room_id=777,
        viewer=Viewer(
            uid=42,
            name="阿强",
            user_level=50,
            wealth_level=22,
            guard_level=GuardLevel.CAPTAIN,
            is_admin=True,
            medal=Medal(name="代码侠", level=12, anchor_room_id=777),
        ),
        gift=Gift(name="能量石", num=10, unit_battery=5),
    )
    payload = live_event_payload(event)
    assert payload["identity"] == "uid:42"
    assert payload["user_level"] == 50
    assert payload["wealth_level"] == 22
    assert payload["guard_level"] == "captain"
    assert payload["is_admin"] is True
    assert payload["medal"] == {
        "name": "代码侠",
        "level": 12,
        "up_name": "",
        "this_room": True,
    }
    assert payload["gift"]["unit_battery"] == 5
    assert payload["gift"]["total_battery"] == 50


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
