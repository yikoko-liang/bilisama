"""apply_panel_edits and speak_paths: the panel's whole write path in one call.

test_config_edit.py pins apply_config_edit, the per-field half. The half above it
had no unit cover at all: its only reader test was tests/ui/test_pet_page.py:99,
which lives in the browser tier — deselected by default and skipped whole on a
machine without chromium. So on most machines, the function dev_talk.py:1402
hands every panel.set payload to was running unwatched.

What matters here is that both payload shapes take the same route. The live tab's
speak matrix could have been a `setattr` loop, and then the config tab's refusals,
its coercion and its receipt would have applied to one tab and not the other.

Kept out of test_config_edit.py only because that file was not part of this change;
the two belong together.
"""

from __future__ import annotations

from collections.abc import Callable

from bilisama.config.schema import Settings
from bilisama.ui.config_edit import apply_panel_edits, speak_paths


def _recorder() -> tuple[list[str], Callable[[str], None]]:
    """A collector standing in for the terminal-and-feed announce callback."""
    said: list[str] = []
    return said, said.append


# ------------------------------------------------------------ speak_paths


def test_speak_paths_names_every_switch_the_schema_has() -> None:
    """Derived from the model, not written out by hand.

    A hand-kept list is how a new switch ends up in the panel's matrix and
    nowhere in the write path: the toggle renders, the click reports 「未知开关」,
    and nothing says the two lists ever disagreed.
    """
    settings = Settings()
    paths = speak_paths(settings)

    assert set(paths) == set(type(settings.interaction.speak).model_fields)
    assert paths["danmaku"] == "interaction.speak.danmaku"
    assert all(path.startswith("interaction.speak.") for path in paths.values())


# ------------------------------------------------------------ the speak matrix


def test_a_speak_toggle_applies_and_reports_itself() -> None:
    """Normal path: the live tab's shape, through the same gate as the config tab."""
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(settings, {"speak": {"danmaku": False}}, announce=announce)

    assert settings.interaction.speak.danmaku is False
    assert changed == ["interaction.speak.danmaku"]
    assert len(said) == 1
    assert "普通弹幕" in said[0] and "关" in said[0]


def test_several_switches_in_one_payload_all_land() -> None:
    """The matrix sends whatever the operator flipped, which can be more than one."""
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(
        settings, {"speak": {"danmaku": False, "follow": True}}, announce=announce
    )

    assert settings.interaction.speak.danmaku is False
    assert settings.interaction.speak.follow is True
    assert sorted(changed) == ["interaction.speak.danmaku", "interaction.speak.follow"]
    assert len(said) == 2


def test_an_unknown_switch_is_named_and_the_rest_still_apply() -> None:
    """Error path: one bad key must not cost the operator the click.

    A payload from an older panel build carries a switch this schema dropped.
    Refusing the whole batch would leave the visible toggles disagreeing with
    the config they claim to show.
    """
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(
        settings, {"speak": {"made_up": True, "danmaku": False}}, announce=announce
    )

    assert changed == ["interaction.speak.danmaku"]
    assert any("未知开关 made_up" in line for line in said), said
    assert settings.interaction.speak.danmaku is False


# ------------------------------------------------------------ the config tab


def test_a_config_edit_applies_and_reports_itself() -> None:
    """The other shape, and the receipt it earns.

    The 「本场生效，重启还原」 half of the line is the whole reason the receipt
    exists: a panel edit is not written back to the TOML, and an operator who
    believes it was will lose the change without noticing.
    """
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(
        settings,
        {"config": {"path": "interaction.speak.gift", "value": "false"}},
        announce=announce,
    )

    assert settings.interaction.speak.gift is False
    assert changed == ["interaction.speak.gift"]
    assert "本场生效，重启还原" in said[0]


def test_a_refused_config_edit_says_why_and_changes_nothing() -> None:
    """Error path: the refusal reaches the operator instead of a traceback.

    apply_config_edit raises; this layer's job is to turn that into one line and
    keep going. A path that escaped here would take down the panel's websocket
    handler over a mistyped field name.
    """
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(
        settings,
        {"config": {"path": "interaction.chattiness", "value": "high"}},
        announce=announce,
    )

    assert changed == []
    assert any("直播中改不了" in line for line in said), said
    assert settings.interaction.chattiness != "high"


def test_a_secret_is_refused_through_this_door_too() -> None:
    """The refusal that must not have a second entrance.

    Whether a credential can be edited from the panel is decided in one place;
    this checks the panel really goes through that place rather than around it.
    """
    settings = Settings()
    said, announce = _recorder()

    changed = apply_panel_edits(
        settings,
        {"config": {"path": "speech.side.api_key_ref", "value": "env:LEAK"}},
        announce=announce,
    )

    assert changed == []
    assert any("密钥" in line for line in said), said


# ------------------------------------------------------------ payload edges


def test_both_halves_of_one_payload_are_applied() -> None:
    """A payload may carry a matrix flip and a config edit at once."""
    settings = Settings()
    _, announce = _recorder()

    changed = apply_panel_edits(
        settings,
        {
            "speak": {"danmaku": False},
            "config": {"path": "interaction.speak.gift", "value": False},
        },
        announce=announce,
    )

    assert changed == ["interaction.speak.danmaku", "interaction.speak.gift"]
    assert settings.interaction.speak.danmaku is False
    assert settings.interaction.speak.gift is False


def test_an_empty_payload_says_nothing_and_changes_nothing() -> None:
    """Boundary: the panel sends a heartbeat-shaped set with neither key."""
    settings = Settings()
    said, announce = _recorder()

    assert apply_panel_edits(settings, {}, announce=announce) == []
    assert said == []


def test_a_non_dict_under_speak_or_config_is_ignored() -> None:
    """Boundary: the payload arrives off a websocket, so its shape is a claim.

    `{"speak": null}` and `{"config": "danmaku"}` are what a half-written client
    sends. Neither may reach .items() or .get() and raise out of the handler.
    """
    settings = Settings()
    said, announce = _recorder()

    assert apply_panel_edits(settings, {"speak": None}, announce=announce) == []
    assert apply_panel_edits(settings, {"config": "danmaku"}, announce=announce) == []
    assert apply_panel_edits(settings, {"speak": ["danmaku"]}, announce=announce) == []
    assert said == []
