"""Small, comment-preserving writes to the active TOML configuration.

The settings panel edits one scalar at a time. Re-serialising the complete
Pydantic model would erase the hand-written comments that make bilisama.toml
usable, and adding a TOML writer only for this path would still reorder the
file. This module therefore replaces or inserts exactly one scalar assignment
after the value has already passed schema validation.
"""

from __future__ import annotations

import json
import os
import stat
from enum import Enum
from pathlib import Path
from typing import Any

from bilisama.config.schema import Settings

__all__ = ["ConfigWriteError", "TomlConfigWriter"]


class ConfigWriteError(OSError):
    """A configuration write that did not reach durable storage."""


def _toml_scalar(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int | float):
        return str(value)
    raise TypeError(f"unsupported TOML scalar: {type(value).__name__}")


def _comment_start(line: str) -> int | None:
    """Find an inline comment without mistaking a quoted # for one."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return index
    return None


def _replace_scalar(text: str, *, section: str, key: str, rendered: str) -> str:
    lines = text.splitlines(keepends=True)
    section_start = 0 if not section else -1
    section_end = len(lines)
    target_header = f"[{section}]"

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_start >= 0:
                section_end = index
                break
            if stripped == target_header:
                section_start = index + 1

    if section_start < 0:
        if text and not text.endswith("\n"):
            text += "\n"
        spacer = "" if not text or text.endswith("\n\n") else "\n"
        return f"{text}{spacer}[{section}]\n{key} = {rendered}\n"

    for index in range(section_start, section_end):
        line = lines[index]
        body = line.rstrip("\r\n")
        comment_at = _comment_start(body)
        uncommented = body[:comment_at].rstrip() if comment_at is not None else body
        if "=" not in uncommented:
            continue
        existing_key = uncommented.split("=", 1)[0].strip()
        if existing_key != key:
            continue
        suffix = ""
        if comment_at is not None:
            suffix = "  " + body[comment_at:].lstrip()
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        lines[index] = f"{key} = {rendered}{suffix}{newline}"
        return "".join(lines)

    lines.insert(section_end, f"{key} = {rendered}\n")
    return "".join(lines)


class TomlConfigWriter:
    """Persist effective panel values to the active profile atomically."""

    def __init__(self, base_path: Path, settings: Settings) -> None:
        self._base_path = base_path
        self._settings = settings

    def target_for(self, field_path: str) -> Path:
        if field_path == "active_profile":
            return self._base_path
        profile = self._settings.active_profile.strip()
        if not profile:
            return self._base_path
        return self._base_path.parent / "profiles" / f"{profile}.toml"

    def write(self, field_path: str, value: Any) -> Path:
        """Write one validated scalar and return the file that changed."""
        target = self.target_for(field_path)
        section, _, key = field_path.rpartition(".")
        try:
            original = target.read_text(encoding="utf-8") if target.exists() else ""
            updated = _replace_scalar(
                original,
                section=section,
                key=key or field_path,
                rendered=_toml_scalar(value),
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
            tmp = target.with_name(target.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise ConfigWriteError(f"配置保存失败：{target}：{exc}") from exc
        return target
