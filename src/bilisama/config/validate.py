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


def volcano_voice_problems(model: str, speaker: str) -> list[ConfigProblem]:
    """Whether this voice and this model generation can work together.

    Its own function because the pairing has two judges. `check` reads the
    config, and the factory reads what will ACTUALLY be sent — `--voice`
    overrides the config, and overriding into the wrong pairing is the obvious
    way to try the other generation. Both failures are silent on the wire, so
    the check has to follow the value rather than the field.

    Args:
        model: The generation, `1.2.1.1` (O2.0) or `2.2.0.0` (SC2.0).
        speaker: The voice id that will be sent, after any override.
    """
    cloned = speaker.startswith(("saturn_", "ICL_", "S_"))
    problems: list[ConfigProblem] = []
    if not speaker:
        # Reproduced on the real endpoint 2026-08-28, and it is the combination
        # nothing had ever tested: the shipped config left this blank with the
        # comment 「留空用服务端默认」, and that default voice brings its own
        # server-side character. It beats `dialog.bot_name` AND a 557-character
        # persona that names her in its first sentence — asked who she is, she
        # answers 「豆包」. Naming a voice fixes it on the spot.
        #
        # Fatal, like the other two: nothing about it looks wrong at run time.
        # She talks, she sounds fine, she is somebody else.
        problems.append(
            ConfigProblem(
                field="speech.volcano.speaker",
                message=(
                    "火山的音色留空了。服务端默认音色自带它自己的角色，会盖过人设——"
                    "问她是谁她答「豆包」，而且不会报任何错。"
                ),
                fix=(
                    "O2.0（1.2.1.1）填官方音色，比如 zh_female_vv_jupiter_bigtts；"
                    "SC2.0（2.2.0.0）填 saturn_ 开头的克隆音色。"
                ),
            )
        )
        return problems
    if model == "2.2.0.0" and not cloned:
        problems.append(
            ConfigProblem(
                field="speech.volcano.speaker",
                message=(
                    "SC2.0（2.2.0.0）只认克隆音色，这里"
                    + (f"填的是「{speaker}」。" if speaker else "留空了。")
                    + "配错了她会一声不吭——服务端回的是 InvalidSpeaker，不是拒绝启动。"
                ),
                fix="换成 saturn_ 开头的官方克隆音色，或你自己注册的 S_ 音色。",
            )
        )
    if model == "1.2.1.1" and cloned:
        problems.append(
            ConfigProblem(
                field="speech.volcano.speaker",
                message=(
                    f"O2.0（1.2.1.1）配了克隆音色「{speaker}」。克隆音色自带服务端角色，"
                    "会盖过人设——她会用别人的名字和口吻说话，而且不报错。"
                ),
                fix="换成官方音色（zh_female_vv_jupiter_bigtts 这类），或把模型版本改成 2.2.0.0。",
            )
        )
    return problems


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

    # Both bounds pass ge=1 alone; only their ORDER makes the tiers mean
    # anything. Inverted, every gift takes the >= high branch first and the
    # medium tier is unreachable — no error, just thank-yous one register too
    # grand. Fatal: a panel edit that would do this must be refused, not saved.
    if s.interaction.gift_battery_medium > s.interaction.gift_battery_high:
        problems.append(
            ConfigProblem(
                field="interaction.gift_battery_medium",
                message=(
                    f"中额礼物门槛（{s.interaction.gift_battery_medium} 电池）高于高额门槛"
                    f"（{s.interaction.gift_battery_high} 电池），中额档永远轮不到。"
                ),
                fix="把中额门槛改到不超过高额门槛，或者把高额门槛改上去。",
                fatal=True,
            )
        )

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
            # Not every backend calls it `voice`: volcano's is `speaker`, and
            # its section forbids extras, so the old hard-coded name sent the
            # streamer to a field that would be rejected if they created it.
            voice_field = "speaker" if s.speech.provider is ProviderName.VOLCANO else "voice"
            hosted_voice = f"speech.{s.speech.provider.value}.{voice_field}"
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

    # Plan section 7.6 row 5. The rate mismatch that used to make this a real
    # obstacle is handled now (realtime/resample.py converts the uplink), so
    # what is left is a commercial and regulatory caution, not a technical one:
    # plan section 3.1 has the supported-countries wording and the per-hour
    # audio pricing against a microphone that is open all stream.
    if s.speech.provider is ProviderName.OPENAI_GA:
        problems.append(
            ConfigProblem(
                field="speech.provider",
                message="openai_ga 是开发期的参照实现，不是出货路径。",
                fix="出货前改成 dashscope、volcano 或自建；拿它当基准对照没问题。",
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
        section = getattr(s.speech, s.speech.provider.value)
        # A blank endpoint is only a problem when nothing else can supply one.
        # Some backends answer at one address for everybody, so the registry
        # ships it and `resolve_endpoint` falls back to it — refusing here
        # rejected the very config config/bilisama.toml and the runbook tell
        # people to write (「地址留空就用内置的公网地址」). DashScope has no
        # such default, its address being tenant-specific, so it still refuses.
        #
        # Imported inside the function on purpose: the registry imports
        # ConfigProblem from this module, so the arrow only goes one way at
        # import time. Same dependency knot cli.py's cmd_validate names.
        from bilisama.realtime.providers import PROFILES

        if not section.endpoint and not PROFILES[s.speech.provider].default_url:
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

    # Voice family and model generation have to match, and neither mismatch
    # announces itself. Probed against the real endpoint 2026-08-28:
    #
    # * SC2.0 with an empty or catalogue voice answers 「ClientError:
    #   InvalidSpeaker」 on a frame that carries no event number — the session
    #   starts, the query is acked, and then nothing. Silence.
    # * O2.0 with a cloned voice DOES speak, as somebody else: the official
    #   cloned voices ship with a server-side character that outranks
    #   system_role, so she introduced herself as 夏栀 and wrote the stage
    #   directions our persona forbids.
    #
    # Fatal on both counts. A persona that silently does not apply is worse
    # than a refusal, and this one is decidable before a socket opens.
    #
    # One guard for both volcano rules. They used to be two adjacent `if`s on
    # the same condition with nothing between them.
    if s.speech.provider is ProviderName.VOLCANO:
        problems += volcano_voice_problems(s.speech.volcano.model, s.speech.volcano.speaker)

        # Either an API Key on its own, or the older App ID / Access Token pair
        # — and they are alternatives, not complements. Not fatal here because
        # path.sh can still supply one at run time; the factory refuses for
        # real when no layer has any, and it names both shapes.
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
