"""The panel write path: only honest-LIVE leaves change, everything else refuses."""

from __future__ import annotations

import pytest

from bilisama.config.enums import VoiceReplyMode
from bilisama.config.schema import RuntimeConfig, Settings
from bilisama.ui.config_edit import ConfigEditError, apply_config_edit, field_control


def test_unknown_path_refused() -> None:
    with pytest.raises(ConfigEditError, match="没有"):
        apply_config_edit(Settings(), "interaction.made_up", True)


def test_non_string_path_refused() -> None:
    with pytest.raises(ConfigEditError, match="字符串"):
        apply_config_edit(Settings(), 42, True)


def test_secret_refused_before_anything_else() -> None:
    # speech.side.api_key_ref is secret AND non-live; the secret reason must win
    # so nobody is invited to "restart" their way into editing a credential.
    with pytest.raises(ConfigEditError, match="密钥"):
        apply_config_edit(Settings(), "speech.side.api_key_ref", "env:X")


def test_non_live_field_refused_with_reload_reason() -> None:
    with pytest.raises(ConfigEditError, match="直播中改不了"):
        apply_config_edit(Settings(), "avatar.renderer", "sprite")


def test_runtime_gate_admits_hooked_reload_classes_but_never_engine() -> None:
    """The widened gate: RECONNECT and RESTART pass (the caller owes the
    hook), ENGINE stays out — dev-talk cannot swap the local speech engine
    inside one process."""
    from bilisama.ui.config_edit import apply_runtime_config_edit

    settings = Settings()
    _meta, applied = apply_runtime_config_edit(settings, "speech.dashscope.voice", "longanqian")
    assert applied == "longanqian" and settings.speech.dashscope.voice == "longanqian"
    with pytest.raises(ConfigEditError, match="直播中改不了"):
        apply_runtime_config_edit(settings, "speech.s2s.turn.thresh", 0.5)


def test_section_header_refused() -> None:
    # interaction.speak is LIVE for grouping but resolves to a whole sub-model.
    with pytest.raises(ConfigEditError, match="分组"):
        apply_config_edit(Settings(), "interaction.speak", {"danmaku": True})


def test_bool_edit_applies_in_place() -> None:
    settings = Settings()  # ships with speak.danmaku True
    meta, applied = apply_config_edit(settings, "interaction.speak.danmaku", False)
    assert applied is False
    assert settings.interaction.speak.danmaku is False
    assert meta.label == "普通弹幕"


def test_bool_edit_coerces_lax_values() -> None:
    settings = Settings()
    _, applied = apply_config_edit(settings, "interaction.speak.gift", "false")
    assert applied is False
    assert settings.interaction.speak.gift is False


def test_bool_edit_rejects_garbage_with_reason() -> None:
    with pytest.raises(ConfigEditError, match="只能开或关"):
        apply_config_edit(Settings(), "interaction.speak.danmaku", "也许吧")


def test_literal_edit_applies() -> None:
    settings = Settings()
    _, applied = apply_config_edit(settings, "runtime.log_level", "debug")
    assert applied == "debug"
    assert settings.runtime.log_level == "debug"


def test_literal_edit_rejects_with_choices_listed() -> None:
    with pytest.raises(ConfigEditError, match="debug / info / warning / error"):
        apply_config_edit(Settings(), "runtime.log_level", "verbose")


def test_an_enum_edit_rejects_with_its_choices_listed() -> None:
    with pytest.raises(ConfigEditError, match="always / when_addressed"):
        apply_config_edit(Settings(), "interaction.voice_reply", "sometimes")
    settings = Settings()
    _, applied = apply_config_edit(settings, "interaction.voice_reply", "when_addressed")
    assert applied == "when_addressed"
    assert settings.interaction.voice_reply is VoiceReplyMode.WHEN_ADDRESSED


def test_field_control_shapes() -> None:
    fields = RuntimeConfig.model_fields
    assert field_control(fields["log_viewer_content"])["kind"] == "bool"
    level = field_control(fields["log_level"])
    assert level["kind"] == "select"
    assert level["choices"] == ["debug", "info", "warning", "error"]
    port = field_control(fields["ui_port"])
    assert port["kind"] == "number"
    assert (port["min"], port["max"]) == (0.0, 65535.0)
    assert field_control(Settings.model_fields["active_profile"])["kind"] == "text"


# ------------------------------------------------------------ the checked gate


def _duplex_settings(*, inverted_tiers: bool = False) -> Settings:
    """A duplex session, as adopt_endpoint leaves it: the provider that is
    running, not the one in the file. `inverted_tiers` plants a fatal problem
    on the gift thresholds — a field no test here edits."""
    from bilisama.config.enums import ProviderName

    settings = Settings()
    settings.speech.provider = ProviderName.VOLCANO_DUPLEX
    if inverted_tiers:
        settings.interaction.gift_battery_medium = 1000
        settings.interaction.gift_battery_high = 500
    return settings


def test_a_problem_already_standing_on_another_field_does_not_block_an_edit() -> None:
    """2026-09-23: the reply-hold knob bounced three times off the avatar's
    expression-tag rule — a fatal problem that was there before the edit, on a
    field the edit never touched, and that startup had not mentioned. The gate
    is about what THIS edit breaks; a standing problem stays standing. (The
    tag rule has since gone advisory; the inverted gift tiers stand in for it
    as the unrelated fatal problem.)"""
    from bilisama.config.validate import check
    from bilisama.ui.config_edit import apply_checked_runtime_edit

    settings = _duplex_settings(inverted_tiers=True)
    assert [p.field for p in check(settings) if p.fatal] == ["interaction.gift_battery_medium"]

    meta, applied = apply_checked_runtime_edit(
        settings, "speech.volcano_duplex.reply_hold_ms", 300, config_dir=None
    )

    assert applied == 300
    assert settings.speech.volcano_duplex.reply_hold_ms == 300
    assert meta.label == "回复先扣多少毫秒再放"


def test_an_edit_that_breaks_the_config_is_refused_and_rolled_back() -> None:
    """The half the old gate got right, kept: an edit that introduces a fatal
    rule is undone, and the refusal names the knob, the value and the rule —
    and the OTHER field the rule reports on, so the connection is spelled out.
    Here: switching the backend to s2s while its llm_model is blank."""
    from bilisama.config.enums import ProviderName
    from bilisama.ui.config_edit import apply_checked_runtime_edit

    settings = _duplex_settings()
    assert settings.speech.s2s.llm_model == ""  # the shipped default

    with pytest.raises(ConfigEditError) as exc_info:
        apply_checked_runtime_edit(settings, "speech.provider", "s2s", config_dir=None)

    said = str(exc_info.value)
    assert said.startswith("「语音后端」改不成 s2s，会连累「对话模型 id」：")
    assert "自建语音服务需要指定对话模型 id。怎么改：" in said
    assert settings.speech.provider is ProviderName.VOLCANO_DUPLEX


def test_moving_an_already_broken_field_is_not_blamed_on_the_edit() -> None:
    """The gift-tier rule embeds both numbers in its sentence. Keyed on the
    message, a standing inversion read as NEW on every edit that moved a tier
    without clearing it, and the refusal said 「会连累」 about a mess that
    predates the edit. Keyed on the field, the edit goes through; the tiers
    are still inverted, but that was true before and is not this edit's doing."""
    from bilisama.ui.config_edit import apply_checked_runtime_edit

    settings = _duplex_settings(inverted_tiers=True)  # medium 1000 > high 500

    _meta, applied = apply_checked_runtime_edit(
        settings, "interaction.gift_battery_high", 600, config_dir=None
    )

    assert applied == 600 and settings.interaction.gift_battery_high == 600


def test_the_ordinary_refusals_still_come_first() -> None:
    """Unknown paths, secrets and reload classes are apply_runtime_config_edit's
    to refuse; the checked wrapper adds a rule, it does not replace the gate."""
    from bilisama.ui.config_edit import apply_checked_runtime_edit

    with pytest.raises(ConfigEditError, match="没有"):
        apply_checked_runtime_edit(Settings(), "interaction.made_up", True, config_dir=None)
    with pytest.raises(ConfigEditError, match="密钥"):
        apply_checked_runtime_edit(Settings(), "speech.side.api_key_ref", "env:X", config_dir=None)


def test_introduced_problems_are_keyed_on_the_field_alone() -> None:
    from bilisama.config.validate import ConfigProblem
    from bilisama.ui.config_edit import introduced_fatal_problems

    standing = ConfigProblem(field="a", message="旧的")
    reworded = ConfigProblem(field="a", message="同一个字段，句子里的数字变了")
    fresh = ConfigProblem(field="b", message="新的")
    advisory = ConfigProblem(field="c", message="提醒而已", fatal=False)

    assert introduced_fatal_problems([standing], [reworded, fresh, advisory]) == [fresh]
    assert introduced_fatal_problems([], [advisory]) == []
    assert introduced_fatal_problems([standing], [standing]) == []
