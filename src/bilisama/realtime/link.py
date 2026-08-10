"""The interface L2 shows upward, and nothing else.

L3 depends on SpeechLink plus the LinkEvent vocabulary below — never on wire
event names, never on a provider module. The eight provider rules from plan
section 3.3 live inside the adapters; if one of them leaks upward, the
dependency gate (tests/unit/test_dependency_direction.py) goes red.

ReplySpec describes intent, not protocol: the s2s adapter turns write_history
into the two-step inject (item in-band, reply out-of-band), a hosted adapter
may do it differently, and L3 cannot tell.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

__all__ = [
    "LinkError",
    "LinkEvent",
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
]

_handle_ids = itertools.count(1)


@dataclass(frozen=True, slots=True)
class ReplySpec:
    """What L3 wants said. How it reaches the wire is the adapter's business."""

    instructions: str | None = None
    max_tokens: int | None = None
    write_history: bool = False
    protected: bool = False
    protect_ms: int = 4000


@dataclass(slots=True)
class ReplyHandle:
    """One requested reply. `stale` flips when the reply was superseded;
    late frames carrying it are dropped without ceremony."""

    handle_id: int = field(default_factory=lambda: next(_handle_ids))
    stale: bool = False


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


@dataclass(frozen=True, slots=True)
class UserTranscriptDone:
    text: str


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
    | LinkError
)


class SpeechLink(Protocol):
    """What L3 is allowed to know about a speech backend.

    The speculative-quiet gate (plan section 3.3 rule 1) is deliberately not
    here: it is scheduling policy built on SpeechStopped timing, and it arrives
    with the SpeakingFloor in stage 2.
    """

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    async def set_context(self, instructions: str) -> None: ...

    async def push_audio(self, pcm: bytes) -> None: ...

    async def add_context_item(self, text: str, *, role: str = "user") -> None: ...

    async def request_reply(self, spec: ReplySpec) -> ReplyHandle: ...

    async def cancel(self, handle: ReplyHandle) -> None: ...

    async def end_protection(self) -> None:
        """Re-arm barge-in after a protected reply. The scheduler calls this
        when a protected reply settles and again on the protect_ms hard cap;
        adapters where protection is a no-op just return."""
        ...

    def events(self) -> AsyncIterator[LinkEvent]: ...
