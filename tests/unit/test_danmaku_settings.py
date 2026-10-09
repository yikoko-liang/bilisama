"""Live config edits for ordinary-danmaku protection and one retry."""

from __future__ import annotations

import pytest

from bilisama.config.schema import Settings
from bilisama.ui.config_edit import ConfigEditError, apply_config_edit


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protect_danmaku_replies", True),
        ("requeue_danmaku_replies", True),
        ("danmaku_protect_ms", 0),
        ("danmaku_protect_ms", 15000),
        ("danmaku_requeue_percent", 0),
        ("danmaku_requeue_percent", 100),
    ],
)
def test_danmaku_controls_accept_live_edits(field: str, value: bool | int) -> None:
    settings = Settings()
    _meta, applied = apply_config_edit(settings, f"interaction.{field}", value)
    assert applied == value
    assert getattr(settings.interaction, field) == value
    if field == "protect_danmaku_replies":
        assert settings.interaction.requeue_danmaku_replies is False
    if field == "requeue_danmaku_replies":
        assert settings.interaction.protect_danmaku_replies is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("danmaku_protect_ms", -1),
        ("danmaku_protect_ms", 15001),
        ("danmaku_requeue_percent", -1),
        ("danmaku_requeue_percent", 101),
        ("danmaku_requeue_percent", 12.5),
        ("protect_danmaku_replies", "也许"),
    ],
)
def test_invalid_danmaku_edits_leave_settings_unchanged(
    field: str, value: int | float | str
) -> None:
    settings = Settings()
    before = settings.model_dump()
    with pytest.raises(ConfigEditError):
        apply_config_edit(settings, f"interaction.{field}", value)
    assert settings.model_dump() == before


def test_danmaku_defaults_preserve_immediate_interruption_without_retry() -> None:
    interaction = Settings().interaction
    assert interaction.protect_danmaku_replies is False
    assert interaction.requeue_danmaku_replies is False
    assert interaction.danmaku_protect_ms == 2000
    assert interaction.danmaku_requeue_percent == 100
