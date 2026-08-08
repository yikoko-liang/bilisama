"""运行时补丁。**不改 speech-to-speech 一个字节。**

补丁住在 BiliSama 仓库里，通过 PYTHONPATH 注入，上游检出目录保持干净。

两个补丁都不是为了「接自研模型」,那件事是纯配置。它们是我们额外挑的两件事的代价：

- A：拿纯文本输出，好用我们自己的中文 VTuber 音色
- B：人设完全由我们控制

两个都关掉就是「零补丁模式」：用它自带的 TTS 和提示词尾巴，一个字节都不碰。
那也是补丁出问题时的退路。

每个补丁都先自检目标符号存在且形状对得上，对不上就 fail fast,上游漂移要在启动时
炸出来，不能变成直播中途的静默失效。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class PatchError(RuntimeError):
    """目标符号不在了，或者形状变了。"""


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
    """让服务端 VAD 发起的隐式轮次也吐纯文本。

    上游 `service.py` 里 `_on_audio_input_completed` 构造 GenerateResponseRequest
    时不带 `response=`，下游就当成要音频,于是会话级设了 output_modalities 也没用。

    要改两处，只改一处不够：

    1. `GenerateResponseRequest` 的默认 `response`,决定模型走文本还是音频路径
    2. `ConnState.current_response_params`,决定服务端按哪套事件名往外发。
       只改前者的话模型确实走文本，但客户端收到的还是 output_audio_transcript.done
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
        # setdefault：显式 response.create 自己带了参数，不覆盖
        if kwargs.get("response") is None:
            kwargs["response"] = text_only_params()
        return original_cls(*args, **kwargs)

    svc.GenerateResponseRequest = patched_request  # type: ignore[misc]

    original_handler: Callable[..., Any] = svc.RealtimeService._on_audio_input_completed

    def patched_handler(self: Any, conn_id: str, event: Any) -> Any:
        state = self._state(conn_id)
        # 这一处决定服务端按哪套事件名发。漏了它客户端收不到 output_text.delta
        state.current_response_params = text_only_params()
        return original_handler(self, conn_id, event)

    svc.RealtimeService._on_audio_input_completed = patched_handler  # type: ignore[method-assign]
    return PatchResult("text_modality", True, "隐式轮次改走纯文本，两处都打了")


# ------------------------------------------------------------ 补丁 B


def patch_raw_instructions() -> PatchResult:
    """关掉它注入的 system prompt 尾巴。

    那段 Voice Rules 被追加在**最后**，也就是最强位置。其中"回复通常一句"是软
    默认还给了豁免，但**禁止 `*laughs*` 这类动作文本是硬的**,VTuber 人设经常要用。

    注意它同时也带了表情/动作工具的使用规范，关掉之后那部分要我们自己在人设里补。
    """
    from speech_to_speech.LLM import base_openai_compatible_language_model as mod

    for name in ("build_voice_system_prompt", "build_text_system_prompt"):
        _require(hasattr(mod, name), f"目标模块里没有 {name},上游结构变了")

    def identity(prompt: str, tool_section: str | None = None) -> str:
        return prompt

    mod.build_voice_system_prompt = identity  # type: ignore[assignment]
    mod.build_text_system_prompt = identity  # type: ignore[assignment]
    return PatchResult("raw_instructions", True, "人设按原样下发，不再被追加尾巴")


# ------------------------------------------------------------ 装配

_PATCHES: dict[str, Callable[[], PatchResult]] = {
    "text_modality": patch_text_modality,
    "raw_instructions": patch_raw_instructions,
}


def apply_patches(names: list[str] | None = None) -> list[PatchResult]:
    """按名字打补丁。默认从环境变量读，空列表就是零补丁模式。"""
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
