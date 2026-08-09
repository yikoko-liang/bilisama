"""Runtime patches for speech-to-speech.

They live in this repo and go in over PYTHONPATH, so the upstream checkout is
never edited.

Neither patch has anything to do with wiring up our own model — that part is pure
configuration. They are the price of two things we chose on top:

- A: text-only output, so we can use our own Chinese VTuber voice.
- B: full control of the persona prompt.

Turn both off and you get zero-patch mode: upstream's TTS, upstream's prompt tail,
nothing touched. That is also the fallback if a patch ever stops applying.

Before touching anything, each patch checks every symbol, field name and call
signature it depends on — not just the ones whose absence would raise. Upstream
drift should blow up at startup, not turn into a silent failure mid-stream.

Checks all run before any write, and the writes go through `_commit` so that
"before" is a property of the code rather than of the order someone happened to
write the lines in. See `_commit` for what enforces it.
"""

from __future__ import annotations

import inspect
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


def _commit(mutations: tuple[tuple[Any, str, Any], ...]) -> None:
    """Write every replacement at once. The only place this module touches upstream.

    A patch that fails half way through leaves the process in a state that is
    neither patched nor pristine, and zero-patch mode — the documented fallback
    for exactly this situation — stops being real. So every check runs first and
    every write happens here, after the last of them.

    That used to be an ordering the comments asserted and nothing enforced: a
    stray `svc.X = ...` a few lines higher passed the whole suite, because the
    one atomicity test tripped a check that came early either way. Funnelling the
    writes through a single call is what makes the invariant checkable, and two
    tests check it: tests/unit/test_s2s_shim_structure.py reads this module and
    fails if any patch function writes outside this call or runs a check after
    it, and tests/integration/test_s2s_patches.py replays every drift injection
    it knows about and fails if a rejected patch left a fingerprint on upstream.

    Args:
        mutations: (target, attribute, replacement) triples, applied in order.
    """
    for target, attribute, replacement in mutations:
        setattr(target, attribute, replacement)


def _replacement_fits(original: Callable[..., Any], replacement: Callable[..., Any]) -> bool:
    """Whether replacement can absorb every call the original signature permits.

    Both patches swap a callable out for one of ours, and both are called by
    upstream code we do not control. Binding the widest call the original accepts
    also covers every narrower one, so this rules out a new upstream parameter
    turning into a TypeError on the first turn of a live stream.

    A `*args`/`**kwargs` original accepts unbounded calls, so there is nothing to
    prove a replacement against: treat it as a mismatch and fail loudly.
    """
    try:
        original_sig = inspect.signature(original)
        replacement_sig = inspect.signature(replacement)
    except (TypeError, ValueError):
        return False

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in original_sig.parameters.items():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            args.append(None)
        elif param.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = None
        else:
            return False

    try:
        replacement_sig.bind(*args, **kwargs)
    except TypeError:
        return False
    return True


# ------------------------------------------------------------ Patch A


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
    # Checked before it is dereferenced below: an AttributeError here would escape
    # __main__.py, which only catches PatchError and ImportError.
    _require(
        hasattr(svc, "RealtimeService"),
        "service 模块里没有 RealtimeService,上游结构变了",
    )
    _require(
        hasattr(svc, "ConnState"),
        "service 模块里没有 ConnState,上游结构变了",
    )
    _require(
        hasattr(svc.RealtimeService, "_on_audio_input_completed"),
        "RealtimeService 上没有 _on_audio_input_completed,上游结构变了",
    )
    _require(
        hasattr(svc.RealtimeService, "_state"),
        "RealtimeService 上没有 _state,上游结构变了",
    )
    _require(
        "current_response_params" in svc.ConnState.model_fields,
        "ConnState 上没有 current_response_params 字段,上游结构变了",
    )
    # These two field names must be checked, not just their classes. Both models
    # take an unknown key without complaining, so a rename would swallow our kwarg
    # and leave the turn on the audio path with no error at all — an absent
    # output_modalities reads as audio (upstream utils/utils.py:20-23).
    _require(
        "output_modalities" in svc.RealtimeResponseCreateParams.model_fields,
        "RealtimeResponseCreateParams 上没有 output_modalities 字段,上游结构变了",
    )
    _require(
        "response" in svc.GenerateResponseRequest.model_fields,
        "GenerateResponseRequest 上没有 response 字段,上游结构变了",
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

    original_handler: Callable[..., Any] = svc.RealtimeService._on_audio_input_completed

    def patched_handler(self: Any, conn_id: str, event: Any) -> Any:
        state = self._state(conn_id)
        # This is what picks the outbound event names. Without it the client
        # never sees output_text.delta.
        state.current_response_params = text_only_params()
        return original_handler(self, conn_id, event)

    # Upstream dispatches this positionally as handler(conn_id, event)
    # (service.py:398, bound method). A new parameter would only show up on the
    # first VAD turn.
    _require(
        _replacement_fits(original_handler, patched_handler),
        "_on_audio_input_completed 的签名变了,补丁替身接不住上游的调用",
    )

    # Nothing above this line mutates upstream: a failed check must leave the
    # process in the state it started in, so zero-patch mode is still reachable.
    # Both writes go together — patching the request without the response params
    # gets you text from the model under audio event names, which is worse than
    # not patching at all.
    _commit(
        (
            (svc, "GenerateResponseRequest", patched_request),
            (svc.RealtimeService, "_on_audio_input_completed", patched_handler),
        )
    )
    return PatchResult("text_modality", True, "隐式轮次改走纯文本，两处都打了")


# ------------------------------------------------------------ Patch B


def patch_raw_instructions() -> PatchResult:
    """Stop upstream from appending its Voice Rules to our persona prompt.

    The block goes in after our persona, deliberately: upstream calls that the
    strongest position (LLM/voice_prompt.py:1,33). Its "usually one sentence" rule
    is a soft default with an explicit escape hatch, but the ban on action text
    like `*laughs*` is unconditional (LLM/voice_prompt.py:11), and a VTuber persona
    uses that constantly.

    The same block also carries the expression- and motion-tool conventions
    (LLM/voice_prompt.py:15-17), so turning it off means writing those into our own
    persona.
    """
    from speech_to_speech.LLM import base_openai_compatible_language_model as mod

    def identity(prompt: str, tool_section: str | None = None) -> str:
        return prompt

    # The name existing is not enough. Upstream is (session_prompt, *,
    # tool_section="") today (LLM/voice_prompt.py:32, LLM/text_prompt.py:28), and a
    # new required keyword would only surface on the first generated reply.
    for name in ("build_voice_system_prompt", "build_text_system_prompt"):
        _require(hasattr(mod, name), f"目标模块里没有 {name},上游结构变了")
        _require(
            _replacement_fits(getattr(mod, name), identity),
            f"{name} 的签名变了,补丁替身接不住上游的调用",
        )

    # Both builders or neither: the loop above has already vetted both names, and
    # a half-applied patch would strip the tail from voice replies while text
    # replies still carry it.
    _commit(
        (
            (mod, "build_voice_system_prompt", identity),
            (mod, "build_text_system_prompt", identity),
        )
    )
    return PatchResult("raw_instructions", True, "人设按原样下发，不再被追加尾巴")


# ------------------------------------------------------------ Registry

_PATCHES: dict[str, Callable[[], PatchResult]] = {
    "text_modality": patch_text_modality,
    "raw_instructions": patch_raw_instructions,
}


def apply_patches(names: list[str] | None = None) -> list[PatchResult]:
    """Apply patches by name.

    None reads the env var, so the launcher does not have to know the default set.
    An empty list is zero-patch mode rather than "apply the defaults".
    """
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
