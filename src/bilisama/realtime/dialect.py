"""协议方言。

OpenAI Realtime 前后有两版，事件名和字段名都变过：

- 早期版（DashScope 实现的）：`modalities`、`response.text.delta`、扁平的
  `input_audio_format`、嵌套的工具声明
- 正式版 GA（speech-to-speech 和 OpenAI 实现的）：`output_modalities`、
  `response.output_text.delta`、嵌套的 `audio.input.format`、扁平的工具声明

有意思的是 DashScope 用的那套早期方言，正是 OpenAI 自己已经退役的 beta 方言,
`openai` SDK 里 `types/beta/realtime/` 至今还留着，字段形状一模一样。

codec 的职责只有一件：wire 上的原始 JSON ↔ 我们内部的归一化名字。
**只归一化一次**,不要先把 GA 名翻成 beta 名再翻成内部名。
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
    """我们会发出去的事件。内部名，跟 wire 名解耦。"""

    SESSION_UPDATE = "session.update"
    AUDIO_APPEND = "input_audio_buffer.append"
    ITEM_CREATE = "conversation.item.create"
    RESPONSE_CREATE = "response.create"
    RESPONSE_CANCEL = "response.cancel"
    ITEM_TRUNCATE = "conversation.item.truncate"


class ServerEvent(StrEnum):
    """我们会收到的事件。归一化之后 L2 只认这些名字。"""

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


# wire 名 → 内部名。两套方言各一张表。
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


@dataclass(frozen=True, slots=True)
class Codec:
    """方言编解码。两个实例，选一个。"""

    dialect: Dialect
    modalities_key: str
    needs_session_type: bool
    nested_audio_format: bool
    nested_tools: bool
    _inbound: Mapping[str, ServerEvent]

    def normalize(self, raw: Mapping[str, Any]) -> tuple[ServerEvent | None, Mapping[str, Any]]:
        """wire JSON → (内部事件名, 原始负载)。不认识的返回 None，由调用方决定怎么办。"""
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
)

BETA = Codec(
    dialect=Dialect.BETA,
    modalities_key="modalities",
    needs_session_type=False,
    nested_audio_format=False,
    nested_tools=True,
    _inbound=_BETA_INBOUND,
)


def outbound_name(codec: Codec, event: ServerEvent) -> str:
    """内部名 → wire 名。只有 Mock 服务端需要它。"""
    for wire, internal in codec._inbound.items():
        if internal is event:
            return wire
    raise KeyError(event)
