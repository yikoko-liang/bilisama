"""Vocabulary for UI metadata.

The Electron settings page is generated from this metadata rather than hand-written,
so adding a config option touches one place. The metadata itself lives in
`ui_meta.UI_META`, keyed by field path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Audience(StrEnum):
    """Who sees a field. The streamer view is a dozen or so items; developers
    see everything."""

    STREAMER = "streamer"
    OPERATOR = "operator"
    DEVELOPER = "developer"


class Reload(StrEnum):
    """When a change takes effect. The UI greys out controls that cannot be
    changed mid-stream."""

    LIVE = "live"  # takes effect immediately
    RECONNECT = "reconnect"  # needs the speech link reconnected
    ENGINE = "engine"  # needs the speech engine restarted
    RESTART = "restart"  # needs a full app restart


def ui(
    *,
    label: str,
    help: str = "",
    audience: Audience = Audience.DEVELOPER,
    reload: Reload = Reload.RESTART,
    group: str = "",
    order: int = 0,
    unit: str = "",
    widget: str = "",
    provider_scoped: str = "",
    derived_from: str = "",
    secret: bool = False,
    wizard_step: int = 0,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Attach UI metadata to a field.

    Widget type is inferred from the schema — bool to toggle, bounded number to
    slider, enum to select — so `widget` is only worth setting when inference
    cannot work it out.
    """
    return {
        "ui": {
            "label": label,
            "help": help,
            "audience": audience.value,
            "reload": reload.value,
            "group": group,
            "order": order,
            "unit": unit,
            "widget": widget,
            "provider_scoped": provider_scoped,
            "derived_from": derived_from,
            "secret": secret,
            "wizard_step": wizard_step,
            "aliases": list(aliases),
        }
    }
