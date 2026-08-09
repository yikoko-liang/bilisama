"""Load configuration from TOML.

A profile is an overlay: it only names the fields it cares about, everything else
falls through to the base file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from bilisama.config.schema import Settings


def load(path: Path | None = None, *, overrides: dict[str, Any] | None = None) -> Settings:
    """Load from TOML, layering the active profile on top."""
    raw: dict[str, Any] = {}
    if path is not None and path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    profile_name = raw.get("active_profile", "normal")
    if path is not None:
        profile_path = path.parent / "profiles" / f"{profile_name}.toml"
        if profile_path.exists():
            raw = _deep_merge(raw, tomllib.loads(profile_path.read_text(encoding="utf-8")))

    if overrides:
        raw = _deep_merge(raw, overrides)
    return Settings.model_validate(raw)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
