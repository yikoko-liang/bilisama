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

# Operator-facing diagnostics: whatever these carry, it was written by the
# code, not by the audience. `error_text` ends in `text`, so the viewer check
# below folded all nineteen call sites that log a failure reason under that
# name (client.py:346, sources.py:126, scheduler.py:337 …) down to
# `<N chars>` — deleting the only field that answers "why", in the log file and
# on the panel alike (ui/hub.py:70-83). Redaction runs first, so a name in here
# still cannot carry a credential out.
_DIAGNOSTIC: Final[frozenset[str]] = frozenset({"error_text", "detail", "advice"})

# The namespace this formatter owns. Reserved whether or not the key happens to
# be present on a given line, because these names MEAN something here: `turn_id`
# is the correlation id bound around the call, not a field, and `exc` is the
# traceback. A caller's field of the same name is a different thing, and letting
# it sit in that slot would have anything grouping by turn_id group the wrong
# lines together.
#
# Being unconditional also removes a trap: `record.exc_info` is not a reliable
# "is there an exception" test. logging fills it from sys.exc_info(), which
# outside an except block is the tuple (None, None, None) — truthy.
_OWNED_KEYS: Final[frozenset[str]] = frozenset(
    {"ts", "level", "event", "logger", "turn_id", "intent_id", "job_id", "exc"}
)

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
    if key.lower() in _DIAGNOSTIC:
        return value
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
            # A field named like a key this formatter owns loses data in
            # whichever direction the collision happens: `event` and `logger`
            # would overwrite the two keys every consumer groups by, while
            # `exc` lands after the fields and would be overwritten by the
            # traceback. Prefixed rather than dropped — dropping is the same
            # silent-loss shape that folding `error_text` into `<N chars>`
            # turned out to be.
            #
            # Values all survive; NAMES do not round-trip. A caller writing
            # both `ts` and `field_ts` gets one of them under `field_ts` and
            # the other under `field_field_ts`, decided by kwargs order.
            # Accepted: nothing is lost, and the alternative is an escaping
            # scheme nobody reading a log line would thank us for.
            taken = set(payload) | _OWNED_KEYS
            for key, value in extra.items():
                # Scrubbed under the name the CALLER wrote, before any rename.
                # Every redaction rule reads field names, and `_matches` splits
                # on word boundaries: `bili_jct` is redacted by exact match,
                # while `field_bili_jct` splits into three words that match
                # nothing. Judging the renamed key would walk that credential
                # straight out.
                scrubbed = _scrub(key, value, log_viewer_content=self._log_viewer_content)
                name = key
                while name in taken:
                    name = f"field_{name}"
                taken.add(name)
                payload[name] = scrubbed

        # Not `if record.exc_info:` — logging fills it from sys.exc_info(),
        # which outside an except block is the tuple (None, None, None), and a
        # tuple is truthy. Formatting that produced `exc: NoneType: None`: a
        # line that says nothing and reads like a swallowed error.
        if record.exc_info is not None and record.exc_info[0] is not None:
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

    # The event name is positional-only on every one of these, and that slash
    # is load-bearing. As a normal parameter it reserved `event` (and `level`
    # on _emit) as field names, so the natural way to record WHICH event failed
    # — log.exception("scheduler.event_failed", event=frame) — raised TypeError
    # instead of logging. It surfaced while writing the catch-all in
    # director/scheduler.py (d34b750), which renamed its field to `frame` to get
    # past it. What makes it worth a slash rather than a note: the natural place
    # to write that call is inside an except block, where a TypeError would take
    # down the handler meant to keep the task alive. Never observed in
    # production — the call was fixed before it ran.

    def _emit(self, level: int, event: str, /, **fields: Any) -> None:
        self._logger.log(level, event, extra={"fields": fields})

    def debug(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, /, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def exception(self, event: str, /, **fields: Any) -> None:
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
