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

from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import annotated_types
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from bilisama.config._ui import Reload
from bilisama.config.schema import Settings
from bilisama.config.ui_meta import UI_META, FieldMeta
from bilisama.config.validate import ConfigProblem, check
from bilisama.obs.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ConfigEditError",
    "apply_checked_runtime_edit",
    "apply_config_edit",
    "apply_panel_edits",
    "apply_runtime_config_edit",
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


def _apply_config_edit(
    settings: Settings,
    path: Any,
    value: Any,
    *,
    allowed_reload: frozenset[Reload],
) -> tuple[FieldMeta, Any]:
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
    if meta.reload not in allowed_reload:
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


def apply_config_edit(settings: Settings, path: Any, value: Any) -> tuple[FieldMeta, Any]:
    """Apply an edit whose consumers already read the value live."""
    return _apply_config_edit(settings, path, value, allowed_reload=frozenset({Reload.LIVE}))


def apply_runtime_config_edit(settings: Settings, path: Any, value: Any) -> tuple[FieldMeta, Any]:
    """Apply a panel edit that has an explicit runtime reload hook.

    The caller must run the matching hook before acknowledging the edit —
    apply-without-hook is the "面板说改好了但没生效" bug in one line. ENGINE
    stays excluded: dev-talk cannot replace the separate local speech engine
    safely inside one process.
    """
    return _apply_config_edit(
        settings,
        path,
        value,
        allowed_reload=frozenset({Reload.LIVE, Reload.RECONNECT, Reload.RESTART}),
    )


def introduced_fatal_problems(
    before: Iterable[ConfigProblem], after: Iterable[ConfigProblem]
) -> list[ConfigProblem]:
    """The fatal problems in `after` on fields that had none in `before`.

    Keyed on the field alone, not the message: three rules embed live values
    in their sentence (gift tiers, the volcano speaker, the wordlist path), so
    a standing problem would read as new every time an edit moved the number
    without clearing it — and the refusal would blame this edit for a mess
    that predates it. A field already broken before the edit stays the
    streamer's standing problem; only a field the edit newly breaks counts.
    """
    standing = {p.field for p in before if p.fatal}
    return [p for p in after if p.fatal and p.field not in standing]


def apply_checked_runtime_edit(
    settings: Settings, path: Any, value: Any, *, config_dir: Path | None
) -> tuple[FieldMeta, Any]:
    """apply_runtime_config_edit, refused when the edit would BREAK the config.

    Only problems the edit introduces count. The gate used to be 「整份配置
    有没有致命问题」, which refused every edit while any fatal problem stood —
    including one on a field the streamer was not touching and had never been
    told about. 2026-09-23: 「回复先扣多少毫秒再放」bounced three times off a
    rule about the avatar's expression tags, with a sentence that named
    neither the knob nor the connection. Now a standing problem stays standing
    and the edit goes through; an edit that makes a NEW fatal problem is
    rolled back and refused with the knob, the value, and the rule it would
    break.

    Args:
        settings: The live Settings object consumers read at call time.
        path: Dotted field path.
        value: The client-supplied value.
        config_dir: Where relative config paths resolve (the base file's
            directory), as `check` wants it.

    Returns:
        (meta, applied value), as apply_runtime_config_edit.

    Raises:
        ConfigEditError: any refusal apply_runtime_config_edit raises, or the
            edit introducing a fatal cross-field problem.
    """
    before = list(check(settings, config_dir=config_dir))
    resolved = _resolve(settings, path) if isinstance(path, str) else None
    old = getattr(resolved[0], resolved[1]) if resolved is not None else None
    meta, applied = apply_runtime_config_edit(settings, path, value)
    if resolved is None:  # apply_ refuses such a path before this line; belt and braces
        raise ConfigEditError(f"「{meta.label}」是一个分组，不是可改的字段")
    introduced = introduced_fatal_problems(before, check(settings, config_dir=config_dir))
    if introduced:
        setattr(resolved[0], resolved[1], old)
        raise ConfigEditError(_breaks_config_reason(meta, str(path), applied, introduced[0]))
    return meta, applied


def _breaks_config_reason(meta: FieldMeta, path: str, applied: Any, problem: ConfigProblem) -> str:
    """Name the knob, the value, and the rule it would break — and, when the
    rule reports on ANOTHER field, that field too, so the sentence explains
    the connection instead of leaving the streamer to guess it."""
    other = UI_META.get(problem.field)
    dragged = (
        ""
        if problem.field == path
        else f"，会连累「{other.label if other is not None else problem.field}」"
    )
    message = problem.message
    if message and message[-1] not in "。！？":
        message += "。"
    fix = f"怎么改：{problem.fix}" if problem.fix else ""
    return f"「{meta.label}」改不成 {shown_value(applied)}{dragged}：{message}{fix}"


def shown_value(applied: Any) -> str:
    """How a receipt line spells a value: 开/关 for switches, else as-is."""
    if applied is True:
        return "开"
    if applied is False:
        return "关"
    return str(applied.value) if hasattr(applied, "value") else str(applied)


def speak_paths(settings: Settings) -> dict[str, str]:
    """Speak-switch name → its config path, so the live matrix and the config
    tab write through the SAME channel instead of two with different gates."""
    speak = settings.interaction.speak
    return {name: f"interaction.speak.{name}" for name in type(speak).model_fields}


# The system page's audio controls, panel key → config path. One table shared
# by dev-talk's on_panel_set and apply_panel_edits below, so the browser-test
# harness exercises the same shape the pet's quick keys send — an audio patch
# only one of the two understood is how the mic switch became a placebo once.
AUDIO_PANEL_PATHS: dict[str, str] = {
    "input_enabled": "audio.input_enabled",
    "output_enabled": "audio.output_enabled",
    "noise_sensitivity": "audio.noise_sensitivity",
}


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
                # Same event as a rejected field below: from the log's side
                # both are 「面板要改的东西没改成」, and splitting them would
                # mean grepping twice for one question.
                log.info("ui.config_edit_refused", path=f"speak.{name}", error_text="未知开关")
    audio_patch = data.get("audio")
    if isinstance(audio_patch, dict):
        for name, value in audio_patch.items():
            if name in AUDIO_PANEL_PATHS:
                edits.append((AUDIO_PANEL_PATHS[name], value))
            else:
                announce(f"未知音频配置 {name}，忽略")
                log.info("ui.config_edit_refused", path=f"audio.{name}", error_text="未知音频配置")
    edit = data.get("config")
    if isinstance(edit, dict):
        edits.append((edit.get("path"), edit.get("value")))
    for path, value in edits:
        try:
            meta, applied = apply_config_edit(settings, path, value)
        except ConfigEditError as exc:
            announce(str(exc))
            # info, not warning: a refusal is the gate working. This is the
            # answer to 「我在面板上改了为什么没用」 — which field, and which
            # of the three gates said no (error_text carries the reason
            # verbatim; the scrubber leaves that field alone).
            log.info("ui.config_edit_refused", path=str(path), error_text=str(exc))
            continue
        announce(f"配置已改：{meta.label} → {shown_value(applied)}（本场生效，重启还原）")
        # The value is safe to log verbatim: secrets are refused by
        # apply_config_edit before they reach here (meta.secret).
        log.info("ui.config_edit_applied", path=str(path), applied=str(applied))
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
