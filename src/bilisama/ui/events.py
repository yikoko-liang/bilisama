"""The UI wire vocabulary: the first real slice of plan section 6.3.

One WebSocket, JSON text frames, shape {"event": ..., "data": {...}}. Audio
never crosses THIS wire: PCM runs at about 48 KB/s, and this queue broadcasts
to every client and drops the oldest frame when it fills — policies that are
right for state and wrong for samples. Audio has its own socket, point to
point with whichever client owns the devices (see ui/audio.py). What crosses
here are the words about audio: who owns it, and what the browser has
finished playing.

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
    # Who holds the microphone and speaker. Broadcast, because the clients that
    # did NOT get them need to say so rather than look broken.
    AUDIO_OWNER = "audio.owner"
    # Device settings live in the panel; the devices themselves live in
    # whichever window holds them, and inside the shell those are two separate
    # windows that cannot reach each other. So the panel asks over the wire and
    # the holder answers over the wire. In a plain browser tab both ends are the
    # same page and the round trip is simply free.
    AUDIO_COMMAND = "audio.command"
    AUDIO_DEVICES = "audio.devices"
    AUDIO_LEVEL = "audio.level"


class ClientEvent(StrEnum):
    """Client → server. Append-only."""

    PET_POKE = "pet.poke"
    PANEL_SET = "panel.set"
    CONSOLE_LINE = "console.line"
    # Section 6.3's playback receipts, arriving for real at last. Per SEGMENT,
    # not per reply: one reply plays as several scheduled buffers, and between
    # two of them there is always an instant where the previous has ended and
    # the next has not started. Reading that instant as "finished speaking" is
    # how the backlog comes back (ledger #41), so the gate counts outstanding
    # segments instead.
    PLAYBACK_STARTED = "playback.started"
    PLAYBACK_ENDED = "playback.ended"
    # Answer to playback.clear. played_ms is the number stage 5 needs to trim a
    # remembered reply down to what the audience actually heard.
    PLAYBACK_CANCELLED = "playback.cancelled"
    # The panel's half of the exchange above: a request from any window, and
    # the holder's answers. The server only relays.
    AUDIO_ASK = "audio.ask"
    AUDIO_REPORT = "audio.report"


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
    elif isinstance(event, link.LinkDown):
        # The panel shows this to a streamer mid-stream: say what happened and
        # whether anything is being done about it, not the close code.
        detail = "正在自动重连" if event.retrying else "已放弃重连，需要手动重启"
        yield ServerEvent.EVENT_FEED, {
            "kind": "error",
            "code": "link_down",
            "detail": f"语音连接断了，{detail}",
        }
    elif isinstance(event, link.LinkUp):
        yield ServerEvent.EVENT_FEED, {
            "kind": "system",
            "code": "link_up",
            "detail": "语音连接已恢复",
        }
    elif isinstance(event, link.LinkError):
        yield ServerEvent.EVENT_FEED, {"kind": "error", "code": event.code, "detail": event.detail}
