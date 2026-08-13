"""Structured logging.

Three rules, all in service of making "why didn't the assistant say anything?"
answerable:

1. Event names are fixed constants, not formatted sentences. `log.info(
   "vad.speech_stopped", audio_end_ms=...)` can be grouped and counted; an
   f-string reads fine once and then cannot. These names double as the probe
   points for the latency benchmark, so one investment covers both.
2. Correlation ids ride in contextvars rather than being threaded through every
   call site.
3. Danmaku bodies are not logged by default. That text belongs to the audience;
   turn it on only while chasing a specific bug.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final, Literal, TextIO

# Correlation ids, carried across the call stack. All three may be unset.
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_intent_id: ContextVar[str | None] = ContextVar("intent_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

# Field names whose values never reach the log, whatever the caller passes.
_REDACTED: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "key",
        "token",
        "sessdata",
        "bili_jct",
        "buvid3",
        "authorization",
        "cookie",
        "password",
        "secret",
    }
)

# Audience-authored content. Logged as a length unless explicitly enabled.
_VIEWER_CONTENT: Final[frozenset[str]] = frozenset({"text", "danmaku", "message", "content"})

# Trailing words that describe a sensitive field instead of carrying it:
# `token_count` and `text_len` are metrics, `content_type` is a label.
_DESCRIPTOR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"count", "len", "length", "size", "bytes", "chars", "ms", "type"}
)

# Word boundaries in a field name: any separator, plus the camelCase seam so
# `apiKey` splits like `api_key` does.
_WORD_BOUNDARY: Final[re.Pattern[str]] = re.compile(r"[^0-9A-Za-z]+|(?<=[a-z0-9])(?=[A-Z])")


def _matches(key: str, markers: frozenset[str]) -> bool:
    """Whether a field name names one of `markers`, matching whole words.

    Substring matching redacts `keyframe`; exact matching lets `danmaku_text`
    through. Both are wrong, so compare word by word.

    Args:
        key: Field name as the caller wrote it.
        markers: Words that make a field sensitive.

    Returns:
        True if the field should be treated as sensitive.
    """
    if key.lower() in markers:
        return True
    words = [word.lower() for word in _WORD_BOUNDARY.split(key) if word]
    if not words or words[-1] in _DESCRIPTOR_SUFFIXES:
        return False
    return any(word in markers for word in words)


@contextmanager
def bind(
    *, turn_id: str | None = None, intent_id: str | None = None, job_id: str | None = None
) -> Iterator[None]:
    """Attach correlation ids to every log line emitted inside this block."""
    tokens = []
    if turn_id is not None:
        tokens.append((_turn_id, _turn_id.set(turn_id)))
    if intent_id is not None:
        tokens.append((_intent_id, _intent_id.set(intent_id)))
    if job_id is not None:
        tokens.append((_job_id, _job_id.set(job_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _scrub(key: str, value: Any, *, log_viewer_content: bool) -> Any:
    """Redact secrets and fold audience content down to a length.

    Args:
        key: Field name as the caller wrote it.
        value: Field value.
        log_viewer_content: Whether audience-authored text may be logged verbatim.

    Returns:
        The value, `"***"`, or a placeholder standing in for it.
    """
    if _matches(key, _REDACTED):
        return "***"
    if not log_viewer_content and _matches(key, _VIEWER_CONTENT):
        if value is None:
            return None
        # Non-str values carry the body too — a LiveEvent repr, a list of
        # danmaku. Name the type rather than let json.dumps stringify it.
        return f"<{len(value)} chars>" if isinstance(value, str) else f"<{type(value).__name__}>"
    return value


class _JsonFormatter(logging.Formatter):
    def __init__(self, *, log_viewer_content: bool) -> None:
        super().__init__()
        self._log_viewer_content = log_viewer_content

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        for var, name in ((_turn_id, "turn_id"), (_intent_id, "intent_id"), (_job_id, "job_id")):
            value = var.get()
            if value is not None:
                payload[name] = value

        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                payload[key] = _scrub(key, value, log_viewer_content=self._log_viewer_content)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class EventLogger:
    """Thin wrapper that forces the event-name-plus-fields style.

    Write ``log.info("vad.speech_stopped", audio_end_ms=12345)``, not
    ``log.info(f"speech stopped at {ms}")`` — the second one cannot be grouped,
    filtered or counted.
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        self._logger.log(level, event, extra={"fields": fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.exception(event, extra={"fields": fields})


def get_logger(name: str) -> EventLogger:
    return EventLogger(name)


def setup(
    *,
    level: Literal["debug", "info", "warning", "error"] = "info",
    log_viewer_content: bool = False,
    stream: TextIO | None = None,
    extra_handlers: Sequence[logging.Handler] = (),
) -> None:
    """Configure the root logger. Call once at process start.

    Args:
        level: Root log level.
        log_viewer_content: Whether to log danmaku bodies verbatim. Off by
            default — that text belongs to the audience.
        stream: Where lines go. Defaults to stderr.
        extra_handlers: Handlers installed alongside the stream handler, each
            given the same JSON formatter so scrubbing has one source of
            truth. dev-talk reconfigures logging around its console patch;
            passing the same handlers to every setup() call keeps them alive
            across the `handlers.clear()` below.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    formatter = _JsonFormatter(log_viewer_content=log_viewer_content)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    for extra in extra_handlers:
        extra.setFormatter(formatter)
        root.addHandler(extra)
    root.setLevel(getattr(logging, level.upper()))
    # Quiet the third-party chatter. uvicorn is started with log_config=None,
    # so its records propagate to root and this parent-level cap applies.
    for noisy in ("websockets", "asyncio", "aiohttp", "httpx", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # The vendored danmaku client logs on its own hardcoded 'blivedm' name —
    # NOT its module path — and a busy room draws unknown-command WARNINGs
    # every few seconds, which would shred dev-talk's input line. Real errors
    # (parse failures, giving up on reconnect) still surface at ERROR.
    logging.getLogger("blivedm").setLevel(logging.ERROR)
