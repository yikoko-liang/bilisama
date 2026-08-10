"""Load configuration from TOML.

A profile is an overlay: it only names the fields it cares about, everything else
falls through to the base file.

This is also where a fatally broken config is refused. Every consumer goes through
here — the CLI today, the Electron backend later — so it is the one chokepoint that
can say no without knowing how to talk to a human.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from bilisama.config.schema import Settings
from bilisama.config.validate import ConfigError, check


def load(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    strict: bool = True,
) -> Settings:
    """Load from TOML, layering the active profile on top.

    Layers, lowest first (plan §7.4): packaged defaults, the base file, the active
    profile, then the overrides.

    Args:
        path: The base TOML file. Profiles are read from its `profiles/` sibling.
        overrides: Runtime panel values, the last layer to win.
        strict: Refuse a config with a fatal cross-field problem. Turn it off only
            to inspect a config that cannot start.

    Returns:
        The merged settings.

    Raises:
        ConfigError: A fatal cross-field rule is broken and `strict` is on.
        tomllib.TOMLDecodeError: The base file or the profile is not valid TOML.
        pydantic.ValidationError: A field has the wrong type or is out of range.
    """
    raw: dict[str, Any] = {}
    if path is not None and path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    overrides = overrides or {}
    # An override that sets active_profile has to pick the profile, so read the
    # name from the override layer first. The profile is still merged underneath
    # the overrides, so an ordinary overridden field still beats the profile.
    profile_name = overrides.get("active_profile", raw.get("active_profile", "normal"))
    if path is not None:
        profile_path = path.parent / "profiles" / f"{profile_name}.toml"
        if profile_path.exists():
            raw = _deep_merge(raw, tomllib.loads(profile_path.read_text(encoding="utf-8")))

    if overrides:
        raw = _deep_merge(raw, overrides)

    settings = Settings.model_validate(raw)
    if strict:
        config_dir = path.parent if path is not None else None
        fatal = [p for p in check(settings, config_dir=config_dir) if p.fatal]
        if fatal:
            raise ConfigError(fatal)
    return settings


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
