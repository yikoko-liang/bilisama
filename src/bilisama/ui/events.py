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

from bilisama.realtime import link

__all__ = ["ClientEvent", "ServerEvent", "frame", "link_frames"]


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


class ClientEvent(StrEnum):
    """Client → server. Append-only."""

    PET_POKE = "pet.poke"
    PANEL_SET = "panel.set"
    CONSOLE_LINE = "console.line"


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


def link_frames(event: link.LinkEvent) -> Iterator[tuple[ServerEvent, dict[str, Any]]]:
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
        yield ServerEvent.REPLY_DELTA, {"text": event.text}
    elif isinstance(event, link.ReplyDone):
        yield ServerEvent.REPLY_DONE, {"status": str(event.status), "text": event.text}
        yield (
            ServerEvent.EVENT_FEED,
            {"kind": "reply", "status": str(event.status), "text": event.text},
        )
    elif isinstance(event, link.UserTranscriptDone):
        yield ServerEvent.TRANSCRIPT_FINAL, {"text": event.text}
        yield ServerEvent.EVENT_FEED, {"kind": "transcript", "text": event.text}
    elif isinstance(event, link.LinkError):
        yield ServerEvent.EVENT_FEED, {"kind": "error", "code": event.code, "detail": event.detail}
