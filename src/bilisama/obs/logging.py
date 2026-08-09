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
import sys
from collections.abc import Iterator
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

    KNOWN BROKEN, both directions — see the refactor backlog, item 2. Secrets use
    substring matching, so `token_count` is redacted as if it were a credential.
    Audience content uses exact equality, so `user_text` and `danmaku_text` pass
    through verbatim. Both should match on whole words. Left as-is here because
    this pass is not allowed to change behaviour.
    """
    lowered = key.lower()
    if any(marker in lowered for marker in _REDACTED):
        return "***"
    if not log_viewer_content and lowered in _VIEWER_CONTENT and isinstance(value, str):
        return f"<{len(value)} chars>"
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
) -> None:
    """Configure the root logger. Call once at process start.

    Args:
        level: Root log level.
        log_viewer_content: Whether to log danmaku bodies verbatim. Off by
            default — that text belongs to the audience.
        stream: Where lines go. Defaults to stderr.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonFormatter(log_viewer_content=log_viewer_content))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))
    # Quiet the third-party chatter.
    for noisy in ("websockets", "asyncio", "aiohttp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
