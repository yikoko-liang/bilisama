"""Runtime patches for speech-to-speech. Its source is never edited.

The patches live in this repo and go in over PYTHONPATH, so the upstream checkout
stays pristine.

Neither patch has anything to do with wiring up our own model — that part is pure
configuration. They are the price of two things we chose on top:

- A: text-only output, so we can use our own Chinese VTuber voice.
- B: full control of the persona prompt.

Turn both off and you get zero-patch mode: upstream's TTS, upstream's prompt tail,
nothing touched. That is also the fallback if a patch ever stops applying.

Each patch checks that its target symbols exist before doing anything. Upstream
drift should blow up at startup, not turn into a silent failure mid-stream.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class PatchError(RuntimeError):
    """A target symbol is gone or has changed shape."""


@dataclass(frozen=True, slots=True)
class PatchResult:
    name: str
    applied: bool
    detail: str = ""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PatchError(message)


# ------------------------------------------------------------ 补丁 A


def patch_text_modality() -> PatchResult:
    """Make the implicit VAD-driven turn produce text instead of audio.

    Upstream's `_on_audio_input_completed` builds a GenerateResponseRequest without
    a `response=`, and everything downstream reads that as "wants audio". Setting
    output_modalities at session level therefore has no effect on that path.

    Two places need patching, and one is not enough:

    1. The default `response` on GenerateResponseRequest decides whether the model
       takes the text path or the audio path.
    2. `ConnState.current_response_params` decides which event names the server
       emits. Patch only the first and the model does produce text, but the client
       still receives output_audio_transcript.done.
    """
    from openai.types.realtime import response_create_event as _rce  # noqa: F401
    from speech_to_speech.api.openai_realtime import service as svc

    _require(
        hasattr(svc, "GenerateResponseRequest"),
        "service 模块里没有 GenerateResponseRequest,上游结构变了",
    )
    _require(
        hasattr(svc, "RealtimeResponseCreateParams"),
        "service 模块里没有 RealtimeResponseCreateParams,上游结构变了",
    )
    _require(
        hasattr(svc.RealtimeService, "_on_audio_input_completed"),
        "RealtimeService 上没有 _on_audio_input_completed,上游结构变了",
    )

    original_cls = svc.GenerateResponseRequest
    params_cls = svc.RealtimeResponseCreateParams

    def text_only_params() -> Any:
        return params_cls(output_modalities=["text"])

    def patched_request(*args: Any, **kwargs: Any) -> Any:
        # setdefault semantics: an explicit response.create brings its own params.
        if kwargs.get("response") is None:
            kwargs["response"] = text_only_params()
        return original_cls(*args, **kwargs)

    svc.GenerateResponseRequest = patched_request

    original_handler: Callable[..., Any] = svc.RealtimeService._on_audio_input_completed

    def patched_handler(self: Any, conn_id: str, event: Any) -> Any:
        state = self._state(conn_id)
        # This is what picks the outbound event names. Without it the client
        # never sees output_text.delta.
        state.current_response_params = text_only_params()
        return original_handler(self, conn_id, event)

    svc.RealtimeService._on_audio_input_completed = patched_handler
    return PatchResult("text_modality", True, "隐式轮次改走纯文本，两处都打了")


# ------------------------------------------------------------ 补丁 B


def patch_raw_instructions() -> PatchResult:
    """Stop upstream from appending its Voice Rules to our persona prompt.

    That block lands last, which is the strongest position in the prompt. The
    "usually one sentence" part is a soft default with an explicit escape hatch,
    but the ban on action text like `*laughs*` is hard, and a VTuber persona uses
    that constantly.

    The same block also carries the expression- and motion-tool conventions, so
    turning it off means writing those into our own persona.
    """
    from speech_to_speech.LLM import base_openai_compatible_language_model as mod

    for name in ("build_voice_system_prompt", "build_text_system_prompt"):
        _require(hasattr(mod, name), f"目标模块里没有 {name},上游结构变了")

    def identity(prompt: str, tool_section: str | None = None) -> str:
        return prompt

    mod.build_voice_system_prompt = identity
    mod.build_text_system_prompt = identity
    return PatchResult("raw_instructions", True, "人设按原样下发，不再被追加尾巴")


# ------------------------------------------------------------ 装配

_PATCHES: dict[str, Callable[[], PatchResult]] = {
    "text_modality": patch_text_modality,
    "raw_instructions": patch_raw_instructions,
}


def apply_patches(names: list[str] | None = None) -> list[PatchResult]:
    """Apply patches by name. Reads the env var when given None; an empty list
    means zero-patch mode."""
    if names is None:
        raw = os.environ.get("BILISAMA_S2S_PATCHES", "text_modality,raw_instructions")
        names = [n.strip() for n in raw.split(",") if n.strip()]

    results: list[PatchResult] = []
    for name in names:
        fn = _PATCHES.get(name)
        if fn is None:
            raise PatchError(f"不认识的补丁：{name}。可选：{', '.join(_PATCHES)}")
        results.append(fn())
    if not names:
        results.append(PatchResult("none", False, "零补丁模式：用它自带的 TTS 和提示词"))
    return results
