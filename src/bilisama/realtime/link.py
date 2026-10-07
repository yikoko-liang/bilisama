"""The interface L2 shows upward, and nothing else.

L3 depends on SpeechLink plus the LinkEvent vocabulary below — never on wire
event names, never on a provider module. The eight provider rules from plan
section 3.3 live inside the adapters; if one of them leaks upward, the
dependency gate (tests/unit/test_dependency_direction.py) goes red.

ReplySpec describes intent, not protocol. The two-step inject is NOT hidden
from L3, though — this paragraph said it was until 2026-09-20, describing an
intention that b3025df superseded and nobody came back to correct. L3 sends
both halves itself (director/scheduler.py's _dispatch) and mirrors her
finished words back with a third call (_write_history); write_history is
honoured there, and no adapter reads the field.

What the adapters do hide is what those two calls BECOME: two frames on the
OpenAI-realtime dialects, one combined query on volcano, and nothing on the
wire at all on duplex, whose add_context_item only stashes for the next
session. Exactly one capability reaches L3 on purpose — composes_on_demand,
because a backend that cannot be made to start a turn needs its words written
before the ask (capabilities.py says why that is a product fact rather than a
wire detail).
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

__all__ = [
    "LinkDown",
    "LinkError",
    "LinkEvent",
    "LinkUp",
    "ReplyAudioDelta",
    "ReplyDone",
    "ReplyHandle",
    "ReplySpec",
    "ReplyStarted",
    "ReplyStatus",
    "ReplyTextDelta",
    "SpeechLink",
    "SpeechStarted",
    "SpeechStopped",
    "ToolCall",
    "UserTranscriptDelta",
    "UserTranscriptDone",
    "added_text",
]

_handle_ids = itertools.count(1)


@dataclass(frozen=True, slots=True)
class ReplySpec:
    """What L3 wants said. How it reaches the wire is the adapter's business."""

    instructions: str | None = None
    # The base the per-turn instructions compose onto, FOR THIS REPLY ONLY.
    # None keeps the session's own context (the implicit-voice-turn default);
    # a value replaces it on the wire without touching the session or forking
    # its history — how event turns get event rules while microphone turns
    # keep voice rules. Adapters without a per-reply instruction channel
    # ignore it (capabilities.per_reply_base_instructions).
    base_instructions: str | None = None
    max_tokens: int | None = None
    # Finished words, to be said as they stand. Only for endpoints that cannot
    # compose (capabilities.composes_on_demand False); on the other four it is
    # None and `instructions` is what the model works from. Filled by L3 —
    # who ghostwrites for her, and in whose voice, is a product decision and
    # never an adapter's.
    text: str | None = None
    write_history: bool = False
    protected: bool = False
    protect_ms: int = 4000


@dataclass(slots=True)
class ReplyHandle:
    """One requested reply. `stale` flips when the reply was superseded;
    late frames carrying it are dropped without ceremony.

    `implicit` marks a reply the provider started on its own VAD turn — the
    streamer's voice, never one of our requests. Set at construction by the
    adapter that minted the handle and never flipped afterwards: the voice
    gate and the scheduler key on it to tell her own turn from ours.
    """

    handle_id: int = field(default_factory=lambda: next(_handle_ids))
    stale: bool = False
    implicit: bool = False


class ReplyStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMED_OUT = "timed_out"  # the client watchdog fired, not the provider


# ---------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    """The streamer began talking. audio_ms is the provider's audio clock."""

    audio_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SpeechStopped:
    audio_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReplyStarted:
    handle: ReplyHandle


@dataclass(frozen=True, slots=True)
class ReplyTextDelta:
    handle: ReplyHandle
    text: str


@dataclass(frozen=True, slots=True)
class ReplyAudioDelta:
    handle: ReplyHandle
    pcm: bytes


@dataclass(frozen=True, slots=True)
class ReplyDone:
    handle: ReplyHandle
    status: ReplyStatus
    text: str = ""  # whatever was collected; cancelled replies keep the partial


@dataclass(frozen=True, slots=True)
class ToolCall:
    handle: ReplyHandle
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class UserTranscriptDelta:
    """The STREAMER's words, never the assistant's — the memory layer keys on
    this distinction (dialect.USER_TRANSCRIPT_* versus TRANSCRIPT_*)."""

    text: str
    """What this delta adds. An increment, on every provider."""
    line_id: str = ""
    """Which spoken line this belongs to, when the provider numbers them.
    Equal ids are one line; the chat page keeps one row per id."""
    whole: str = ""
    """The line as it reads now, when the provider knows it. A transcriber
    that revises itself (punctuation settling, a number re-read) cannot say
    so in increments; this can. Empty means only `text` is known."""


@dataclass(frozen=True, slots=True)
class UserTranscriptDone:
    text: str
    """The finished line."""
    line_id: str = ""
    """As on the delta. A second done with an id already finished SUPERSEDES
    the first — the line went on after it was closed — and carries the whole
    line again: a consumer that appends should take only what it had not
    seen (`added_text`), a consumer that draws rows should rewrite the row."""


def added_text(seen: str, now: str) -> str:
    """What `now` adds to a line already taken up to `seen`.

    `seen` should be the longest reading taken, not the last: a transcriber's
    tail flaps (「…游戏？」「…游戏」「…游戏？」, measured on the duplex
    endpoint), and measuring against the last reading would hand out 「？」
    twice. A revision in the middle — 「今天。」 then 「今天我们」 — continues
    from the common prefix. A reading no longer than `seen` adds nothing.
    """
    if now.startswith(seen):
        return now[len(seen) :]
    if len(now) <= len(seen):
        return ""
    common = 0
    for a, b in zip(seen, now, strict=False):
        if a != b:
            break
        common += 1
    return now[common:]


@dataclass(frozen=True, slots=True)
class LinkDown:
    """The transport is gone. Nothing can be sent until LinkUp arrives.

    Distinct from LinkError on purpose: an error is one thing that failed, a
    down link is a STATE that persists, and L3 decides very different things
    about the two. Every reply in flight has already been settled FAILED by
    the time this lands.
    """

    reason: str
    """Why it dropped, in wire terms — a close code, or a transport message."""

    retrying: bool = True
    """False means the client has given up; nothing more is coming."""


@dataclass(frozen=True, slots=True)
class LinkUp:
    """The transport is back AND the session has been restored.

    Emitted after the adapter has replayed its bootstrap and context, so a
    consumer seeing this can send immediately.
    """

    attempts: int = 1
    """How many tries it took, for the log and the health card."""


@dataclass(frozen=True, slots=True)
class LinkError:
    code: str
    detail: str


LinkEvent = (
    SpeechStarted
    | SpeechStopped
    | ReplyStarted
    | ReplyTextDelta
    | ReplyAudioDelta
    | ReplyDone
    | ToolCall
    | UserTranscriptDelta
    | UserTranscriptDone
    | LinkDown
    | LinkUp
    | LinkError
)


class SpeechLink(Protocol):
    """What L3 is allowed to know about a speech backend.

    The speculative-quiet gate (plan section 3.3 rule 1) is scheduling policy
    and stays in the SpeakingFloor. How LONG that gate holds is not: it is a
    number only this backend knows, so it is asked for here.
    """

    quiet_window_s: float
    """How long after SpeechStopped the floor must stay shut for this backend.

    Plan section 3.3 rule 1, and 4.3 is explicit that the gate must not know
    field names like `smart_turn_max_wait_ms`. Read off the link rather than
    recomputed by the caller: the value is assembled from endpointing settings
    that the adapter is also about to put on the wire, and a caller doing its
    own arithmetic over the same config is a second reader at a different
    altitude — which is how volcano once inherited DashScope's timing from an
    `else` branch and opened the gate about 0.9 s early."""

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    async def suspend(self) -> None:
        """Close the socket and hold every automatic reconnect.

        The pause gate's link half: nothing is torn down for good — context,
        dialog continuity and pending re-arms stay with the adapter — but no
        frame moves and no reconnect ladder climbs until resume().
        """
        ...

    async def resume(self) -> None:
        """Reopen after suspend() and replay what a fresh socket needs.

        Raising is allowed and means "still down": the caller keeps the
        session paused and says so, rather than un-pausing into silence.
        """
        ...

    async def set_context(self, instructions: str) -> None: ...

    async def push_audio(self, pcm: bytes) -> None: ...

    async def add_context_item(self, text: str, *, role: str = "user") -> None: ...

    async def request_reply(self, spec: ReplySpec) -> ReplyHandle: ...

    async def cancel(self, handle: ReplyHandle, *, unheard: bool = False) -> None:
        """Stop this reply, and settle it with exactly one ReplyDone.

        `unheard` is a fact L3 states, not an order: nothing of this reply has
        reached a speaker, because the voice gate held every frame of it. An
        adapter whose protocol is harmed by a server-side cancel may stop such
        a reply by dropping it instead (duplex: a cancel scrambles the
        streamer's transcription, 2026-10-05). False means it may have been
        heard, and the reply must actually stop.
        """
        ...

    async def end_protection(self) -> None:
        """Re-arm barge-in after a protected reply. The scheduler calls this
        when a protected reply settles and again on the protect_ms hard cap;
        adapters where protection is a no-op just return."""
        ...

    def events(self) -> AsyncIterator[LinkEvent]: ...
