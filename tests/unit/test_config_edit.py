"""The panel write path: only honest-LIVE leaves change, everything else refuses."""

from __future__ import annotations

import pytest

from bilisama.config._ui import Reload
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


def test_opinion_collection_window_is_a_real_live_config_field() -> None:
    settings = Settings()
    meta, applied = apply_config_edit(settings, "interaction.proactive.collection_window_s", 45)
    assert meta.reload is Reload.LIVE
    assert meta.label == "观点征集时长"
    assert applied == 45
    assert settings.interaction.proactive.collection_window_s == 45


@pytest.mark.parametrize("value", [0, 4, 1801])
def test_opinion_collection_window_rejects_out_of_bounds(value: int) -> None:
    settings = Settings()
    with pytest.raises(ConfigEditError):
        apply_config_edit(settings, "interaction.proactive.collection_window_s", value)
    assert settings.interaction.proactive.collection_window_s == 30
