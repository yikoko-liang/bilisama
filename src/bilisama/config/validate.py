"""跨字段校验。

schema 的类型检查管不到这些。报错要给人话和可点的修复动作，不给 traceback。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from bilisama.config.enums import ProviderName

if TYPE_CHECKING:
    from bilisama.config.schema import Settings


class ConfigProblem(BaseModel):
    """校验结果。给人话和可点的修复动作，不给 traceback。"""

    field: str
    message: str
    fix: str = ""
    fatal: bool = True


def check(s: Settings) -> list[ConfigProblem]:
    """跨字段校验。schema 的类型检查管不到这些。"""
    problems: list[ConfigProblem] = []
    owns_tts = (
        s.speech.provider is not ProviderName.S2S or "text_modality" not in s.speech.s2s.patches
    )

    if owns_tts and s.avatar.expression_source == "tag":
        problems.append(
            ConfigProblem(
                field="avatar.expression_source",
                message="当前语音后端自己出音频，内联表情标签会被念出来。",
                fix="把表情驱动方式改成 lexicon 或 tool_call。",
            )
        )

    if s.audio.output_route == "direct" and s.audio.echo_guard == "off":
        problems.append(
            ConfigProblem(
                field="audio.output_route",
                message="AI 的声音会进你的麦克风，判停会一直误触发。",
                fix="改用虚拟声卡输出，或者戴耳机并打开抢跑静音。",
                fatal=False,
            )
        )

    if s.speech.provider is ProviderName.S2S and not s.speech.s2s.llm_model:
        problems.append(
            ConfigProblem(
                field="speech.s2s.llm_model",
                message="自建语音服务需要指定对话模型 id。",
                fix="在设置里填上模型 id，或改用托管服务。",
            )
        )

    if s.speech.provider is not ProviderName.S2S:
        hosted = getattr(s.speech, s.speech.provider.value)
        if not hosted.endpoint:
            problems.append(
                ConfigProblem(
                    field=f"speech.{s.speech.provider.value}.endpoint",
                    message="托管语音服务缺少地址。",
                    fix="在设置里填服务地址。",
                )
            )

    if s.room.room_id and not s.room.credential_ref:
        problems.append(
            ConfigProblem(
                field="room.credential_ref",
                message="没有登录凭据，观众 id 会被平台掩码成 0，认不出常客。",
                fix="在设置里扫码登录。",
                fatal=False,
            )
        )

    return problems
