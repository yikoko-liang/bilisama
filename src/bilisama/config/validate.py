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
from bilisama.config.schema import CURRENT_VERSION

if TYPE_CHECKING:
    from bilisama.config.schema import Settings


def _customised(section: BaseModel) -> tuple[str, ...]:
    """Which fields of one config section were moved off their shipped default.

    "Did the streamer configure this at all" has no other answer: every field
    here has a default, so `[custom_tts]` looks equally filled in whether it was
    typed out or left alone. Comparing against the defaults is what separates a
    section that means something from one that is just sitting there.
    """
    return tuple(
        name
        for name, info in type(section).model_fields.items()
        if getattr(section, name) != info.default
    )


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

    # A file a NEWER build wrote. Migrations only run forwards (migrate.py), so
    # there is nothing to bring this one back with, and reading it anyway means
    # honouring values whose meaning may have changed under the same name.
    if s.config_version > CURRENT_VERSION:
        problems.append(
            ConfigProblem(
                field="config_version",
                message=(
                    f"这份配置是更新版本的 BiliSama 写的（config_version={s.config_version}，"
                    f"本机只认到 {CURRENT_VERSION}）。"
                ),
                fix="装回新版本再打开它；或者改用另一份配置文件。",
            )
        )

    # Plan section 7.6 row 7: a missing wordlist refuses to start — a mouth
    # with no backstop must not reach an audience. "auto" resolves the same way
    # director/output_guard.load_guard does (keep the two in step).
    #
    # The room_id decides how loud, not whether. It used to decide whether, and
    # that made this rule unreachable for the config that ships (room_id = 0,
    # config/bilisama.toml): `bilisama config validate` — the check gate.sh runs
    # every commit — answered 「配置没问题」 for a config that dev-talk --director
    # then refuses outright (dev_talk.py, load_guard raises). Fatal only once a
    # room is named, so a fresh install is told rather than blocked.
    if config_dir is not None:
        raw = s.safety.wordlist_path
        wordlist = (
            config_dir / "safety" / "wordlist.txt" if raw == "auto" else Path(raw).expanduser()
        )
        if not wordlist.is_file():
            going_live = bool(s.room.room_id)
            tail = (
                "没有输出兜底不能开播。"
                if going_live
                else "现在只是提醒，但 dev-talk --director 会直接拒绝启动。"
            )
            problems.append(
                ConfigProblem(
                    field="safety.wordlist_path",
                    message=f"敏感词表文件不存在：{wordlist}。{tail}",
                    fix="把词表放到该路径，或在设置里改成真实文件的位置。",
                    fatal=going_live,
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

    # Plan section 7.6 rows 2 and 4, split by which field the streamer should go
    # and change. Both only fire once `[custom_tts]` has actually been filled in:
    # the section has a default for everything, so warning about it on every
    # hosted run would warn on the shipped config of the most common setup.
    touched = _customised(s.custom_tts)
    if touched and owns_tts:
        if s.speech.provider is not ProviderName.S2S:
            hosted_voice = f"speech.{s.speech.provider.value}.voice"
            problems.append(
                ConfigProblem(
                    field="custom_tts.engine",
                    message=(
                        f"托管语音服务自己出音频，[custom_tts] 这一节"
                        f"（{'、'.join(touched)}）没人读。"
                    ),
                    fix=f"改 {hosted_voice}——那才是这条路上决定她声音的字段。",
                    fatal=False,
                )
            )
        else:
            # Without the text_modality patch the s2s server synthesises its own
            # replies (tools/s2s_shim/bilisama_s2s_shim/patches.py:113-140 explains
            # which two places the patch has to touch), so our chain never sees
            # the text.
            problems.append(
                ConfigProblem(
                    field="speech.s2s.patches",
                    message=(
                        "没打 text_modality 补丁时，自建语音服务自己出音频，"
                        "[custom_tts] 不会被调用。"
                    ),
                    fix="把 text_modality 加回补丁列表，或者把 [custom_tts] 改回默认。",
                    fatal=False,
                )
            )

    # Plan section 7.6 row 5, and it is a note rather than a refusal on purpose.
    # The rate mismatch is real (24k both ways per plan section 3.1, against the
    # 16k uplink everywhere else — dev_talk.py:67-68), but so is the fact that
    # this provider has no adapter at all yet (dev_talk.py:1066).
    if s.speech.provider is ProviderName.OPENAI_GA:
        problems.append(
            ConfigProblem(
                field="speech.provider",
                message="openai_ga 收发都是 24kHz，而这条链路的上行按 16kHz 发，中间要重采样。",
                fix="换成 dashscope 或自建这两条接通了的路；真要走 openai_ga，得先补上重采样。",
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

    # Either an API Key on its own, or the older App ID / Access Token pair —
    # and they are alternatives, not complements. Not fatal here because
    # path.sh can still supply one at run time; the factory refuses for real
    # when no layer has any, and it names both shapes.
    if s.speech.provider is ProviderName.VOLCANO:
        volcano = s.speech.volcano
        has_pair = bool(volcano.app_id_ref and volcano.access_key_ref)
        if not volcano.api_key_ref and not has_pair:
            problems.append(
                ConfigProblem(
                    field="speech.volcano.api_key_ref",
                    message="火山没配凭据：要么一个 API Key，要么 App ID ＋ Access Token 那一对。",
                    fix="控制台 > API Key 管理拿一个填进 api_key_ref，最省事。",
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
