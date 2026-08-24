"""Load configuration from TOML.

A profile is an overlay: it only names the fields it cares about, everything else
falls through to the base file.

This is also where a fatally broken config is refused. Every consumer goes through
here — the CLI today, the Electron backend later — so it is the one chokepoint that
can say no without knowing how to talk to a human.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bilisama.config.migrate import migrate
from bilisama.config.schema import Settings
from bilisama.config.validate import ConfigError, ConfigProblem, check

log = logging.getLogger(__name__)


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse one TOML file, naming it on failure.

    A bare TOMLDecodeError says "line 3, column 7" but not in which file —
    useless once profiles exist, because the broken line is as likely in
    profiles/<name>.toml as in the base file (D7).
    """
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            [
                ConfigProblem(
                    field=str(path),
                    message=f"TOML 语法错误：{exc}",
                    fix="打开这个文件，检查报错位置附近的引号、括号和等号。",
                )
            ]
        ) from exc


@dataclass(frozen=True, slots=True)
class Layer:
    """One overlay, with the name a streamer would recognise it by."""

    name: str
    values: dict[str, Any]


def layers(path: Path | None = None, *, overrides: dict[str, Any] | None = None) -> list[Layer]:
    """The overlays `load` merges, lowest first and still separate.

    Split out so that "which layer did this value come from" has an answer at all
    (plan §7.7). `load` merges exactly this list, so the two cannot disagree about
    the order — which they would within a week as two copies.

    Args:
        path: The base TOML file. Profiles are read from its `profiles/` sibling.
        overrides: Runtime panel values, the last layer to win.

    Returns:
        Only the layers that exist. The packaged defaults are not one of them:
        they are the schema, and a field nobody overlaid comes from there.

    Raises:
        ConfigError: A file is not valid TOML (the problem names which file).
    """
    found: list[Layer] = []
    base: dict[str, Any] = {}
    if path is not None and path.exists():
        base = _read_toml(path)
        found.append(Layer(path.name, base))

    overrides = overrides or {}
    # An override that sets active_profile has to pick the profile, so read the
    # name from the override layer first. The profile is still merged underneath
    # the overrides, so an ordinary overridden field still beats the profile.
    profile_name = overrides.get("active_profile", base.get("active_profile", "normal"))
    if path is not None:
        profile_path = path.parent / "profiles" / f"{profile_name}.toml"
        if profile_path.exists():
            found.append(Layer(f"profiles/{profile_name}.toml", _read_toml(profile_path)))

    if overrides:
        found.append(Layer("面板改动", overrides))
    return found


def origins(found: list[Layer]) -> dict[str, str]:
    """Which layer each value ended up coming from.

    Args:
        found: The layers, lowest first — what `layers()` returns.

    Returns:
        Dotted field path -> layer name. Only paths some layer sets are in here;
        anything missing came from the schema default, and saying that in words
        is the caller's job (it is the one talking to a person).
    """
    where: dict[str, str] = {}
    for layer in found:
        for field_path in _leaf_paths(layer.values):
            where[field_path] = layer.name
    return where


def _leaf_paths(values: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten a parsed TOML table to dotted paths, one per value."""
    paths: list[str] = []
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.extend(_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


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
        ConfigError: A fatal cross-field rule is broken and `strict` is on, or a
            file is not valid TOML (the problem names which file).
        pydantic.ValidationError: A field has the wrong type or is out of range.
    """
    raw: dict[str, Any] = {}
    for layer in layers(path, overrides=overrides):
        raw = _deep_merge(raw, layer.values)

    # After the merge, not per file: a profile overlay names only the fields it
    # cares about and has no version of its own to migrate.
    raw, notes = migrate(raw)
    for note in notes:
        log.info("config.migrated: %s", note)

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
