"""The UI wire vocabulary: the first real slice of plan section 6.3.

One WebSocket, JSON text frames, shape {"event": ..., "data": {...}}. Audio
never crosses this wire — the preview keeps playback and the microphone in the
dev-talk process, and stage 5 adds audio.* as new vocabulary rather than by
changing anything here.

Names are an append-only contract (same discipline as SkipReason): the panel,
the pet page and later the generated .d.ts all key on them, so a rename is a
protocol break. Where section 6.3 already has a word — voice.state, event.feed,
panel.set, playback.clear — this module uses it verbatim. Section 6.3's single
`transcript.delta` was ambiguous between the streamer's words and the
assistant's; this vocabulary splits it into reply.delta/reply.done (assistant)
and transcript.final (streamer).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

from bilisama.ingest.events import LiveEvent
from bilisama.realtime import link

__all__ = ["ClientEvent", "ServerEvent", "frame", "link_frames", "live_event_payload"]


class ServerEvent(StrEnum):
    """Server → client. Append-only."""

    HELLO = "hello"
    VOICE_STATE = "voice.state"
    REPLY_DELTA = "reply.delta"
    REPLY_DONE = "reply.done"
    TRANSCRIPT_FINAL = "transcript.final"
    EVENT_FEED = "event.feed"
    PLAYBACK_CLEAR = "playback.clear"
    LOG_LINE = "log.line"
    PANEL_STATE = "panel.state"
    AUDIO_LEVEL = "audio.level"
    LIVE_MOCK_STATE = "live_mock.state"
    LIVE_MOCK_EVENT = "live_mock.event"


class ClientEvent(StrEnum):
    """Client → server. Append-only."""

    PET_POKE = "pet.poke"
    PANEL_SET = "panel.set"
    CONSOLE_LINE = "console.line"
    TEST_RUN = "test.run"
    TEST_STOP = "test.stop"
    LIVE_MOCK_CHECK = "live_mock.check"
    LIVE_MOCK_START = "live_mock.start"
    LIVE_MOCK_STOP = "live_mock.stop"
    LIVE_MOCK_CAPTURE_STOP = "live_mock.capture_stop"
    AUDIO_CHUNK = "audio.chunk"
    APP_QUIT = "app.quit"


def frame(event: ServerEvent, data: Mapping[str, Any]) -> str:
    """Serialize one wire frame.

    Args:
        event: The vocabulary entry.
        data: Frame payload; values must be JSON-friendly (default=str catches
            the stragglers such as Path).

    Returns:
        The JSON text for the WebSocket, Chinese kept readable.
    """
    return json.dumps({"event": str(event), "data": dict(data)}, ensure_ascii=False, default=str)


def live_event_payload(event: LiveEvent) -> dict[str, Any]:
    """Build the shared room-feed and reply-reference payload."""
    viewer = event.viewer
    medal = viewer.medal
    gift = event.gift
    return {
        "kind": event.kind.value,
        "room_id": event.room_id,
        "name": viewer.display_name,
        "uid": viewer.uid,
        "uid_hash": viewer.uid_hash,
        "identity": viewer.identity,
        "user_level": viewer.user_level,
        "wealth_level": viewer.wealth_level,
        "guard_level": viewer.guard_level.value,
        "is_admin": viewer.is_admin,
        "medal": (
            {
                "name": medal.name,
                "level": medal.level,
                "up_name": medal.up_name,
                "this_room": medal.is_this_room(event.room_id),
            }
            if medal is not None
            else None
        ),
        "text": event.text,
        "gift": (
            {
                "name": gift.name,
                "num": gift.num,
                "unit_battery": gift.unit_battery,
                "total_battery": gift.total_battery,
                "combo_count": gift.combo_count,
                "aggregated_count": gift.aggregated_count,
            }
            if gift is not None
            else None
        ),
        "value_cny": event.value_cny,
        "ts_ms": event.ts_ms,
        "mock": bool(event.raw and event.raw.get("manual_mock")),
    }


def link_frames(
    event: link.LinkEvent,
    *,
    reply_context: Mapping[str, Any] | None = None,
) -> Iterator[tuple[ServerEvent, dict[str, Any]]]:
    """Translate one LinkEvent into zero or more UI frames.

    PCM is dropped here, on purpose: the browser renders text and state only.
    Speech start/stop is also absent — the voice-state poller owns that story,
    and forwarding the raw edges would give the page two clocks to disagree on.

    Args:
        event: A normalised event from SpeechLink.events().

    Yields:
        (event, payload) pairs ready for UiHub.broadcast.
    """
    if isinstance(event, link.ReplyTextDelta):
        payload = {
            "reply_id": event.handle.handle_id,
            "source": "voice",
            "text": event.text,
        }
        payload.update(reply_context or {})
        yield ServerEvent.REPLY_DELTA, payload
    elif isinstance(event, link.ReplyDone):
        payload = {
            "reply_id": event.handle.handle_id,
            "source": "voice",
            "status": str(event.status),
            "text": event.text,
        }
        payload.update(reply_context or {})
        yield ServerEvent.REPLY_DONE, payload
        yield (
            ServerEvent.EVENT_FEED,
            {"kind": "reply", **payload},
        )
    elif isinstance(event, link.UserTranscriptDone):
        yield ServerEvent.TRANSCRIPT_FINAL, {"text": event.text}
        yield ServerEvent.EVENT_FEED, {"kind": "transcript", "text": event.text}
    elif isinstance(event, link.LinkDown):
        # The panel shows this to a streamer mid-stream: say what happened and
        # whether anything is being done about it, not the close code.
        detail = "正在自动重连" if event.retrying else "已放弃重连，需要手动重启"
        yield (
            ServerEvent.EVENT_FEED,
            {
                "kind": "error",
                "code": "link_down",
                "detail": f"语音连接断了，{detail}",
            },
        )
    elif isinstance(event, link.LinkUp):
        yield (
            ServerEvent.EVENT_FEED,
            {
                "kind": "system",
                "code": "link_up",
                "detail": "语音连接已恢复",
            },
        )
    elif isinstance(event, link.LinkError):
        yield ServerEvent.EVENT_FEED, {"kind": "error", "code": event.code, "detail": event.detail}
