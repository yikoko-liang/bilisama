"""Vocabulary for UI metadata.

The Electron settings page is generated from this metadata rather than hand-written,
so adding a config option touches one place. The metadata itself lives in
`ui_meta.UI_META`, keyed by field path.
"""

from __future__ import annotations

from enum import StrEnum


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
