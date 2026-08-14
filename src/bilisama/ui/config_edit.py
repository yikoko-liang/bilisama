"""Runtime config edits arriving from the panel.

This is the write half of the ui_meta contract: the same metadata that renders
the read-only rows decides what the panel may change. Three gates, in order —
the path must exist in UI_META (unknown paths are refused, not guessed),
secrets never travel through the panel, and only Reload.LIVE fields may change
mid-run. The value itself goes through the parent pydantic model, so Field
constraints (ge/le, Literal choices) hold on this path exactly as they do at
load time.

The reload annotations were audited against consumer reality before this
module existed (2026-08-14): every field whose consumer snapshots at
construction time was downgraded from LIVE, so "LIVE" here means the change
is actually read at call time — the panel never lies about an edit working.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Literal, get_args, get_origin

import annotated_types
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from bilisama.config._ui import Reload
from bilisama.config.schema import Settings
from bilisama.config.ui_meta import UI_META, FieldMeta

__all__ = [
    "ConfigEditError",
    "apply_config_edit",
    "apply_panel_edits",
    "field_control",
    "speak_paths",
]

_RELOAD_ZH = {
    Reload.RECONNECT: "重连语音后端后生效",
    Reload.ENGINE: "要重启语音引擎",
    Reload.RESTART: "要重启后生效",
}


class ConfigEditError(Exception):
    """Refusal with a streamer-readable reason. str(exc) is the message."""


def _resolve(settings: Settings, path: str) -> tuple[BaseModel, str] | None:
    """Walk the dotted path to (parent model, leaf field name), or None."""
    parent: Any = settings
    parts = path.split(".")
    for part in parts[:-1]:
        parent = getattr(parent, part, None)
        if parent is None:
            return None
    if not isinstance(parent, BaseModel) or parts[-1] not in type(parent).model_fields:
        return None
    return parent, parts[-1]


def _choices(annotation: Any) -> list[str] | None:
    """Enumerable values of a Literal or StrEnum annotation, else None."""
    if get_origin(annotation) is Literal:
        args = get_args(annotation)
        if all(isinstance(a, str) for a in args):
            return [str(a) for a in args]
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return [str(member) for member in annotation]
    return None


def field_control(info: FieldInfo) -> dict[str, Any]:
    """Widget facts the panel needs to render an editor for one field.

    Returns kind ("bool" | "select" | "number" | "text") plus choices and
    numeric bounds where the annotation carries them. bool is checked before
    number because bool subclasses int.
    """
    # Any on purpose: FieldInfo.annotation is type[Any] | None and mypy's
    # narrowing of `is bool` / `in (int, float)` marks the later arms
    # unreachable otherwise.
    ann: Any = info.annotation
    choices = _choices(ann)
    if choices is not None:
        return {"kind": "select", "choices": choices, "min": None, "max": None}
    if ann is bool:
        return {"kind": "bool", "choices": None, "min": None, "max": None}
    if ann in (int, float):
        lo: float | None = None
        hi: float | None = None
        for constraint in info.metadata:
            if isinstance(constraint, annotated_types.Ge):
                lo = float(constraint.ge)  # type: ignore[arg-type]
            elif isinstance(constraint, annotated_types.Le):
                hi = float(constraint.le)  # type: ignore[arg-type]
        return {"kind": "number", "choices": None, "min": lo, "max": hi}
    return {"kind": "text", "choices": None, "min": None, "max": None}


def apply_config_edit(settings: Settings, path: Any, value: Any) -> tuple[FieldMeta, Any]:
    """Validate and apply one panel edit in place.

    Args:
        settings: The live Settings object consumers read at call time.
        path: Dotted field path, e.g. "interaction.speak.danmaku".
        value: The client-supplied value; coerced by the parent model.

    Returns:
        (meta, applied value) for the acknowledgement line.

    Raises:
        ConfigEditError: with a Chinese reason for every refusal.
    """
    if not isinstance(path, str):
        raise ConfigEditError("配置路径要是字符串")
    meta = UI_META.get(path)
    if meta is None:
        raise ConfigEditError(f"没有「{path}」这个配置项")
    if meta.secret:
        raise ConfigEditError(f"「{meta.label}」是密钥，不走面板——改 path.sh 或环境变量")
    if meta.reload is not Reload.LIVE:
        why = _RELOAD_ZH.get(meta.reload, "本场改不了")
        raise ConfigEditError(f"「{meta.label}」直播中改不了：{why}")
    resolved = _resolve(settings, path)
    if resolved is None or isinstance(getattr(resolved[0], resolved[1]), BaseModel):
        # Section headers carry LIVE for grouping; they are not editable leaves.
        raise ConfigEditError(f"「{meta.label}」是一个分组，不是可改的字段")
    parent, field = resolved
    # Round-trip through the parent model so ge/le and Literal checks apply.
    try:
        patched = type(parent).model_validate({**parent.model_dump(), field: value})
    except ValidationError as exc:
        raise ConfigEditError(_reject_reason(meta, type(parent).model_fields[field])) from exc
    applied = getattr(patched, field)
    setattr(parent, field, applied)
    return meta, applied


def speak_paths(settings: Settings) -> dict[str, str]:
    """Speak-switch name → its config path, so the live matrix and the config
    tab write through the SAME channel instead of two with different gates."""
    speak = settings.interaction.speak
    return {name: f"interaction.speak.{name}" for name in type(speak).model_fields}


def apply_panel_edits(
    settings: Settings,
    data: Mapping[str, Any],
    *,
    announce: Callable[[str], None],
) -> list[str]:
    """Apply one panel.set payload's config writes and report each in Chinese.

    The single place a panel edit lands, shared by dev-talk and the browser
    tests so neither can drift from the other. Two shapes reach it: the config
    tab's `{"config": {"path", "value"}}`, and the live tab's speak matrix
    `{"speak": {...}}` — which is routed through the same validation rather
    than a bare setattr, so both get the same refusals and the same receipt.

    Args:
        settings: The live Settings object consumers read at call time.
        data: The panel.set payload.
        announce: Called once per line for the operator (terminal, feed, both).

    Returns:
        The dotted paths that actually changed, for the caller's reload hooks.
    """
    applied_paths: list[str] = []
    edits: list[tuple[Any, Any]] = []
    speak_patch = data.get("speak")
    if isinstance(speak_patch, dict):
        known = speak_paths(settings)
        for name, value in speak_patch.items():
            if name in known:
                edits.append((known[name], value))
            else:
                announce(f"未知开关 {name}，忽略")
    edit = data.get("config")
    if isinstance(edit, dict):
        edits.append((edit.get("path"), edit.get("value")))
    for path, value in edits:
        try:
            meta, applied = apply_config_edit(settings, path, value)
        except ConfigEditError as exc:
            announce(str(exc))
            continue
        shown = "开" if applied is True else "关" if applied is False else str(applied)
        announce(f"配置已改：{meta.label} → {shown}（本场生效，重启还原）")
        applied_paths.append(str(path))
    return applied_paths


def _reject_reason(meta: FieldMeta, info: FieldInfo) -> str:
    """A rejection the streamer can act on, from the field's own constraints."""
    control = field_control(info)
    if control["choices"]:
        return f"「{meta.label}」只能是：{' / '.join(control['choices'])}"
    if control["kind"] == "number":
        lo, hi = control["min"], control["max"]
        if lo is not None and hi is not None:
            return f"「{meta.label}」要在 {lo:g} 到 {hi:g} 之间"
        if lo is not None:
            return f"「{meta.label}」不能小于 {lo:g}"
        if hi is not None:
            return f"「{meta.label}」不能大于 {hi:g}"
        return f"「{meta.label}」要是数字"
    if control["kind"] == "bool":
        return f"「{meta.label}」只能开或关"
    return f"「{meta.label}」的值不合法"
