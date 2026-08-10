"""The one place configuration comes from.

`bilisama.toml` is the single source of truth; secrets are the exception and live
in the OS keychain. speech-to-speech's launch JSON is rendered from here and should
never be hand-edited.

Split by responsibility:

- `_ui`      vocabulary for UI metadata (Audience, Reload)
- `enums`    plain enums shared by schema and validate
- `schema`   types and defaults
- `ui_meta`  UI metadata, keyed by field path
- `derive`   the five thresholds chattiness derives
- `validate` cross-field checks
- `loader`   TOML loading, overlay merging, and refusing a config that cannot start

Import from this module only. The split is internal structure and callers should
not have to track it.
"""

from __future__ import annotations

from bilisama.config._ui import Audience, Reload
from bilisama.config.derive import DerivedThresholds, derive
from bilisama.config.enums import Chattiness, ProviderName
from bilisama.config.loader import load
from bilisama.config.schema import (
    AudioConfig,
    AvatarConfig,
    CustomTTSConfig,
    HostedConfig,
    InteractionConfig,
    MemoryConfig,
    PersonaConfig,
    RoomConfig,
    RuntimeConfig,
    S2SConfig,
    SafetyConfig,
    Settings,
    SideModelConfig,
    SpeakSwitches,
    SpeechConfig,
    TurnConfig,
)
from bilisama.config.ui_meta import UI_META, FieldMeta
from bilisama.config.validate import ConfigError, ConfigProblem, check

__all__ = [
    "UI_META",
    "Audience",
    "AudioConfig",
    "AvatarConfig",
    "Chattiness",
    "ConfigError",
    "ConfigProblem",
    "CustomTTSConfig",
    "DerivedThresholds",
    "FieldMeta",
    "HostedConfig",
    "InteractionConfig",
    "MemoryConfig",
    "PersonaConfig",
    "ProviderName",
    "Reload",
    "RoomConfig",
    "RuntimeConfig",
    "S2SConfig",
    "SafetyConfig",
    "Settings",
    "SideModelConfig",
    "SpeakSwitches",
    "SpeechConfig",
    "TurnConfig",
    "check",
    "derive",
    "load",
]
