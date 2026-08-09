"""Protocol dialects.

The OpenAI Realtime protocol shipped twice with different names:

- The early one, which DashScope implements: `modalities`, `response.text.delta`,
  a flat `input_audio_format`, nested tool declarations.
- The GA one, which speech-to-speech and OpenAI implement: `output_modalities`,
  `response.output_text.delta`, a nested `audio.input.format`, flat tools.

DashScope's dialect is in fact OpenAI's own retired beta — the shapes still sit in
`openai`'s `types/beta/realtime/`, field for field.

A codec does exactly one thing: raw wire JSON to our internal names and back.
Normalise once. Do not translate GA names into beta names and then into ours.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Dialect(StrEnum):
    BETA = "beta"
    GA = "ga"


class ClientEvent(StrEnum):
    """Events we send. Internal names, decoupled from the wire."""

    SESSION_UPDATE = "session.update"
    AUDIO_APPEND = "input_audio_buffer.append"
    ITEM_CREATE = "conversation.item.create"
    RESPONSE_CREATE = "response.create"
    RESPONSE_CANCEL = "response.cancel"
    ITEM_TRUNCATE = "conversation.item.truncate"


class ServerEvent(StrEnum):
    """Events we receive. After normalisation, L2 knows only these."""

    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    ITEM_CREATED = "item.created"
    RESPONSE_CREATED = "response.created"
    TEXT_DELTA = "response.text.delta"
    TEXT_DONE = "response.text.done"
    AUDIO_DELTA = "response.audio.delta"
    AUDIO_DONE = "response.audio.done"
    TRANSCRIPT_DELTA = "response.transcript.delta"
    TRANSCRIPT_DONE = "response.transcript.done"
    FUNCTION_ARGS_DONE = "response.function_call_arguments.done"
    RESPONSE_DONE = "response.done"
    ITEM_TRUNCATED = "item.truncated"
    ERROR = "error"


# Wire name to internal name, one table per dialect.
_GA_INBOUND: Mapping[str, ServerEvent] = {
    "session.created": ServerEvent.SESSION_CREATED,
    "session.updated": ServerEvent.SESSION_UPDATED,
    "input_audio_buffer.speech_started": ServerEvent.SPEECH_STARTED,
    "input_audio_buffer.speech_stopped": ServerEvent.SPEECH_STOPPED,
    "conversation.item.created": ServerEvent.ITEM_CREATED,
    "conversation.item.added": ServerEvent.ITEM_CREATED,
    "conversation.item.truncated": ServerEvent.ITEM_TRUNCATED,
    "response.created": ServerEvent.RESPONSE_CREATED,
    "response.output_text.delta": ServerEvent.TEXT_DELTA,
    "response.output_text.done": ServerEvent.TEXT_DONE,
    "response.output_audio.delta": ServerEvent.AUDIO_DELTA,
    "response.output_audio.done": ServerEvent.AUDIO_DONE,
    "response.output_audio_transcript.delta": ServerEvent.TRANSCRIPT_DELTA,
    "response.output_audio_transcript.done": ServerEvent.TRANSCRIPT_DONE,
    "response.function_call_arguments.done": ServerEvent.FUNCTION_ARGS_DONE,
    "response.done": ServerEvent.RESPONSE_DONE,
    "error": ServerEvent.ERROR,
}

_BETA_INBOUND: Mapping[str, ServerEvent] = {
    "session.created": ServerEvent.SESSION_CREATED,
    "session.updated": ServerEvent.SESSION_UPDATED,
    "input_audio_buffer.speech_started": ServerEvent.SPEECH_STARTED,
    "input_audio_buffer.speech_stopped": ServerEvent.SPEECH_STOPPED,
    "conversation.item.created": ServerEvent.ITEM_CREATED,
    # Beta defines truncation too, this table just never listed it
    # (openai/types/beta/realtime/conversation_item_truncated_event.py:23). Without
    # the entry, wire_name() raises on the first barge-in against a beta endpoint
    # that supports truncate.
    "conversation.item.truncated": ServerEvent.ITEM_TRUNCATED,
    "response.created": ServerEvent.RESPONSE_CREATED,
    "response.text.delta": ServerEvent.TEXT_DELTA,
    "response.text.done": ServerEvent.TEXT_DONE,
    "response.audio.delta": ServerEvent.AUDIO_DELTA,
    "response.audio.done": ServerEvent.AUDIO_DONE,
    "response.audio_transcript.delta": ServerEvent.TRANSCRIPT_DELTA,
    "response.audio_transcript.done": ServerEvent.TRANSCRIPT_DONE,
    "response.function_call_arguments.done": ServerEvent.FUNCTION_ARGS_DONE,
    "response.done": ServerEvent.RESPONSE_DONE,
    "error": ServerEvent.ERROR,
}


def _reverse(table: Mapping[str, ServerEvent]) -> dict[ServerEvent, str]:
    """Internal name to wire name.

    Several wire names can normalise to one internal name — GA's
    conversation.item.created and .added both become ITEM_CREATED — so the first
    one listed wins on the way back out.
    """
    out: dict[ServerEvent, str] = {}
    for wire, internal in table.items():
        out.setdefault(internal, wire)
    return out


@dataclass(frozen=True, slots=True)
class Codec:
    """A dialect codec. Two instances exist; pick one."""

    dialect: Dialect
    modalities_key: str
    needs_session_type: bool
    nested_audio_format: bool
    nested_tools: bool
    _inbound: Mapping[str, ServerEvent]
    _outbound: Mapping[ServerEvent, str]

    def wire_name(self, event: ServerEvent) -> str:
        """Internal name to this dialect's wire name.

        The reverse table is precomputed at import: the mock server calls this once
        per delta, and a linear scan has no business on that path.
        """
        return self._outbound[event]

    def normalize(self, raw: Mapping[str, Any]) -> tuple[ServerEvent | None, Mapping[str, Any]]:
        """Normalise a raw wire frame to an internal event name.

        Args:
            raw: One frame off the WebSocket, already parsed from JSON.

        Returns:
            (internal name, original payload). Unknown events come back as None —
            whether that is worth a warning is the caller's call, not ours.
        """
        wire = raw.get("type")
        if not isinstance(wire, str):
            return None, raw
        return self._inbound.get(wire), raw

    def session_patch(self, *, instructions: str, text_only: bool) -> dict[str, Any]:
        session: dict[str, Any] = {"instructions": instructions}
        if self.needs_session_type:
            session["type"] = "realtime"
        if text_only:
            session[self.modalities_key] = ["text"]
        return {"type": ClientEvent.SESSION_UPDATE.value, "session": session}

    def tool_spec(self, name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
        flat = {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        if self.nested_tools:
            return {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
        return flat


GA = Codec(
    dialect=Dialect.GA,
    modalities_key="output_modalities",
    needs_session_type=True,
    nested_audio_format=True,
    nested_tools=False,
    _inbound=_GA_INBOUND,
    _outbound=_reverse(_GA_INBOUND),
)

BETA = Codec(
    dialect=Dialect.BETA,
    modalities_key="modalities",
    needs_session_type=False,
    nested_audio_format=False,
    nested_tools=True,
    _inbound=_BETA_INBOUND,
    _outbound=_reverse(_BETA_INBOUND),
)
