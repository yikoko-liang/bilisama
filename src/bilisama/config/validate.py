"""Cross-field validation.

Type checking cannot catch these. Problems come back as plain sentences with a
concrete next step, never as a traceback — a streamer who sees
`pydantic.ValidationError` will just file a ticket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from bilisama.config.enums import ProviderName

if TYPE_CHECKING:
    from bilisama.config.schema import Settings


class ConfigProblem(BaseModel):
    """One thing wrong with the configuration.

    `message` and `fix` are shown to the streamer, so they stay in Chinese and
    stay free of jargon. `fix` should name an action, not restate the problem.
    """

    field: str
    message: str
    fix: str = ""
    fatal: bool = True


class ConfigError(Exception):
    """The config is fatally wrong, so we refuse to start.

    Carries the problems instead of a formatted string: every caller has to show
    `field` and `fix` too, not just the message.
    """

    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = problems
        super().__init__("\n".join(p.message for p in problems))


def check(s: Settings) -> list[ConfigProblem]:
    """Validate combinations that individual field types cannot express.

    Args:
        s: A settings object that already passed schema validation.

    Returns:
        Everything wrong with it. Empty means good to go.
    """
    problems: list[ConfigProblem] = []
    owns_tts = (
        s.speech.provider is not ProviderName.S2S or "text_modality" not in s.speech.s2s.patches
    )

    # Inline <expr/> tags only survive if we hold the text before it reaches a
    # synthesizer. When the provider speaks for us, the tag gets read aloud —
    # speech-to-speech's own SPEECHABLE_PATTERN (LLM/utils.py:18-20) whitelists
    # square brackets, so its filter does not save us either.
    if owns_tts and s.avatar.expression_source == "tag":
        problems.append(
            ConfigProblem(
                field="avatar.expression_source",
                message="当前语音后端自己出音频，内联表情标签会被念出来。",
                fix="把表情驱动方式改成 lexicon 或 tool_call。",
            )
        )

    # Nothing in the pipeline does acoustic echo cancellation. Without either a
    # virtual output device or the local energy gate, the assistant's own voice
    # re-enters the mic and false-triggers turn detection continuously — which
    # looks like a broken endpointer and sends people tuning VAD thresholds that
    # were never the problem.
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

    # Anonymous connections still work, but Bilibili masks every uid to 0, so
    # per-viewer memory, name-checking and per-uid cooldowns all stop working —
    # which is most of what makes a co-host feel present.
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
