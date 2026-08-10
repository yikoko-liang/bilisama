"""Render [speech.s2s] into speech-to-speech's launch JSON.

Our turn-detection field names match `vad_arguments.py` word for word, so this is
a direct mapping with no translation table to keep in sync.

The names get checked against upstream before anything is written, because
`s2s_pipeline.py:241` parses this file with `allow_extra_keys=True`: a misspelled
key is swallowed without a word, and you find out mid-stream.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from bilisama.config import S2SConfig

# Upstream dataclasses, scanned to reconcile field names.
_UPSTREAM_ARG_DIR = "arguments_classes"

_DEFAULT_PORT = 8765

_FIELD_RE = re.compile(r"^\s{4}([a-z_][a-z0-9_]*)\s*:\s*\S", re.MULTILINE)


class S2SConfigError(RuntimeError):
    """Rendered config does not match what upstream accepts."""


class Reconciliation(Enum):
    """Whether the field names actually got compared against upstream.

    Empty `unknown_keys` means nothing on its own. It only means "clean" when this
    is CHECKED. UNAVAILABLE is the dangerous one: the caller asked for the check
    and it did not happen.
    """

    NOT_REQUESTED = "not_requested"
    UNAVAILABLE = "unavailable"
    CHECKED = "checked"


@dataclass(frozen=True, slots=True)
class RenderResult:
    payload: dict[str, object]
    unknown_keys: tuple[str, ...]
    missing_turn_fields: tuple[str, ...]
    # No default, so every construction site has to say which state it is in.
    reconciliation: Reconciliation


def upstream_field_names(s2s_root: Path) -> frozenset[str]:
    """Scan upstream's argument dataclasses for every field name it accepts.

    Args:
        s2s_root: The speech-to-speech checkout.

    Returns:
        Every valid field name, or an empty set when the sources are not there.
        Callers must treat empty as "could not check" rather than "checked and fine".
    """
    arg_dir = s2s_root / "src" / "speech_to_speech" / _UPSTREAM_ARG_DIR
    if not arg_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    for path in sorted(arg_dir.glob("*.py")):
        names.update(_FIELD_RE.findall(path.read_text(encoding="utf-8")))
    return frozenset(names)


def render(cfg: S2SConfig) -> dict[str, object]:
    """Render the launch config.

    Deliberately omits mac_optimal_settings. It only supplies defaults, so an
    explicit `stt` still wins, but it quietly moves a dozen other knobs and that is
    one more layer to peel back when something misbehaves.

    Args:
        cfg: Our provider (b) settings.

    Returns:
        Launch parameters ready to serialise. Keys are upstream field names.
    """
    payload: dict[str, object] = {
        # Skip STT so VAD audio reaches the model directly. This is the
        # VAD -> S2T -> TTS path the requirements ask for.
        "stt": "none",
        "llm_backend": "chat-completions",
        "model_name": cfg.llm_model,
        "responses_api_base_url": cfg.llm_base_url,
        "responses_api_api_key": "none",
        "responses_api_stream": True,
        "responses_api_audio_content_type": "input_audio",
        "tts": cfg.tts_placeholder,
        # Unused by non-qwen3 placeholder engines, load-bearing in zero-patch
        # mode: an unset speaker means a random voice per reply upstream.
        "qwen3_tts_speaker": cfg.tts_speaker,
        "host": "127.0.0.1",
        "port": _port_of(cfg.endpoint),
        "num_pipelines": 1,
        # Burns CPU for nothing once STT is skipped.
        "enable_live_transcription": False,
    }
    turn = cfg.turn.model_dump()
    # inf is not valid JSON, and upstream reads a missing value as "no limit".
    if turn.get("max_speech_ms") == float("inf"):
        turn.pop("max_speech_ms")
    payload.update(turn)
    return payload


def render_checked(cfg: S2SConfig, s2s_root: Path | None) -> RenderResult:
    """Render, then reconcile field names against upstream when we can.

    Args:
        cfg: Our provider (b) settings.
        s2s_root: The speech-to-speech checkout, or None to skip reconciliation.

    Returns:
        The payload plus what the reconciliation found. Read `reconciliation`
        before believing `unknown_keys`: empty means "clean" only when the check
        actually ran.
    """
    payload = render(cfg)
    if s2s_root is None:
        return RenderResult(payload, (), (), Reconciliation.NOT_REQUESTED)

    known = upstream_field_names(s2s_root)
    if not known:
        # Asked to check, could not. Reporting this as clean is the failure mode
        # this whole module exists to prevent.
        return RenderResult(payload, (), (), Reconciliation.UNAVAILABLE)

    unknown = tuple(sorted(k for k in payload if k not in known))
    turn_fields = set(type(cfg.turn).model_fields) - _intentionally_omitted(cfg)
    missing = tuple(sorted(f for f in turn_fields if f in known and f not in payload))
    return RenderResult(payload, unknown, missing, Reconciliation.CHECKED)


def _intentionally_omitted(cfg: S2SConfig) -> set[str]:
    """Fields left out on purpose. Distinguishing these from oversights is the point."""
    omitted: set[str] = set()
    if cfg.turn.max_speech_ms == float("inf"):
        # inf is not valid JSON; upstream defaults to unlimited anyway.
        omitted.add("max_speech_ms")
    return omitted


def write(cfg: S2SConfig, dest: Path, *, s2s_root: Path | None = None) -> RenderResult:
    """Render, reconcile, then write the launch JSON.

    Nothing reaches disk unless the config is either verified or was never asked
    to be. A live stream launches from this file, and scripts/smoke_provider_b.sh
    only checks that it exists.

    Args:
        cfg: Our provider (b) settings.
        dest: Where to write the launch JSON. Parent directories are created.
        s2s_root: The speech-to-speech checkout, or None to skip reconciliation.

    Returns:
        What render_checked found, once the file is on disk.

    Raises:
        S2SConfigError: Upstream would swallow one of our keys, or s2s_root was
            given but holds no upstream sources to reconcile against.
    """
    result = render_checked(cfg, s2s_root)
    if result.reconciliation is Reconciliation.UNAVAILABLE:
        raise S2SConfigError(
            f"没能在这个目录里找到上游的参数定义，字段名对账没做成：{s2s_root}\n"
            f"怎么办：确认 --s2s-root 指向 speech-to-speech 的检出"
            f"（它应该有 src/speech_to_speech/{_UPSTREAM_ARG_DIR}/）。"
        )
    if result.unknown_keys:
        raise S2SConfigError(
            "这些配置项上游不认识，会被静默忽略：" + "、".join(result.unknown_keys)
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(result.payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _port_of(endpoint: str) -> int:
    """Port from a ws:// endpoint, falling back to upstream's default."""
    return urlsplit(endpoint).port or _DEFAULT_PORT
