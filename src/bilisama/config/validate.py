"""Cross-field validation.

Type checking cannot catch these. Problems come back as plain sentences with a
concrete next step, never as a traceback — a streamer who sees
`pydantic.ValidationError` will just file a ticket.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bilisama.config.enums import GrowthMode, ProviderName

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


def check(s: Settings, *, config_dir: Path | None = None) -> list[ConfigProblem]:
    """Validate combinations that individual field types cannot express.

    Args:
        s: A settings object that already passed schema validation.
        config_dir: Directory holding bilisama.toml; enables the checks that
            must touch the filesystem (the safety wordlist). None skips them.

    Returns:
        Everything wrong with it. Empty means good to go.
    """
    problems: list[ConfigProblem] = []

    # Plan section 7.6 row 7: a missing wordlist refuses to start — a mouth
    # with no backstop must not reach an audience. Gated on room_id like the
    # credential rule: the backstop matters when going live, and nagging a
    # fresh install trains people to ignore the list. "auto" resolves the
    # same way director/output_guard.load_guard does (keep the two in step).
    if config_dir is not None and s.room.room_id:
        raw = s.safety.wordlist_path
        wordlist = (
            config_dir / "safety" / "wordlist.txt" if raw == "auto" else Path(raw).expanduser()
        )
        if not wordlist.is_file():
            problems.append(
                ConfigProblem(
                    field="safety.wordlist_path",
                    message=f"敏感词表文件不存在：{wordlist}。没有输出兜底不能开播。",
                    fix="把词表放到该路径，或在设置里改成真实文件的位置。",
                )
            )
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

    # Acoustic echo cancellation exists since 2026-08-24, but it only covers
    # what the shell itself plays: anything another process renders — OBS
    # monitoring, background music, a game — still reaches the mic and
    # false-triggers turn detection, which looks like a broken endpointer and
    # sends people tuning VAD thresholds that were never the problem.
    #
    # Neither field below has a runtime reader; this advice is all they do.
    if s.audio.output_route == "virtual":
        problems.append(
            ConfigProblem(
                field="audio.output_route",
                message="「走虚拟声卡给 OBS」这条路会绕过回声消除。",
                fix="让她的声音从桌宠壳直接出扬声器；OBS 只采集、别开「监听并输出」。"
                "面板「现场」页的回声卡会告诉你有没有漏。",
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

    # The growth layers run on the side model. Turning one on without that model
    # configured would not error anywhere — distillation simply never runs and
    # the files never grow, silent degradation. Proactive topics have the same
    # dependency but stay out of this rule: speak.proactive is the shipped
    # default, and nagging every fresh install trains people to ignore the list.
    # The proactive loop reports the missing model at runtime instead (health).
    growth = s.persona.growth
    growth_on = growth.relationship is not GrowthMode.OFF or growth.voice is not GrowthMode.OFF
    if growth_on and not s.speech.side.base_url:
        problems.append(
            ConfigProblem(
                field="speech.side.base_url",
                message="人设生长层开了，但侧路模型还没配地址，生长层会静默不长。",
                fix="在设置里填侧路模型地址，或把生长层拨回 off。",
                fatal=False,
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
