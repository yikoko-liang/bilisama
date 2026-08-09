"""结构化日志。

三条纪律，都是为了让「为什么小助手刚才没说话」可回答（计划 §4.12）：

1. event 名是固定的字符串常量，不是格式化出来的句子。探针点的名字跟
   bench_latency 的打点名共用一套（§2.8），一次投入两处收益。
2. turn_id / intent_id / job_id 用 contextvars 携带，不用每个调用点手传。
3. **默认不记弹幕正文**。那是观众的话，出问题时再开。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final, Literal

# 跨调用栈携带的关联 id。三个都可能为空。
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_intent_id: ContextVar[str | None] = ContextVar("intent_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

# 这些 key 的值永远不进日志，不管调用方传了什么
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

# 弹幕正文这类观众内容，默认打成长度而不是原文
_VIEWER_CONTENT: Final[frozenset[str]] = frozenset({"text", "danmaku", "message", "content"})


@contextmanager
def bind(
    *, turn_id: str | None = None, intent_id: str | None = None, job_id: str | None = None
) -> Iterator[None]:
    """在这个上下文里发出的所有日志都自动带上这些 id。"""
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
    lowered = key.lower()
    if any(marker in lowered for marker in _REDACTED):
        return "***"
    if not log_viewer_content and lowered in _VIEWER_CONTENT and isinstance(value, str):
        return f"<{len(value)}字>"
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
    """薄封装，强制 event 名 + 结构化字段的写法。

    用法：``log.info("vad.speech_stopped", audio_end_ms=12345)``
    不要写 ``log.info(f"speech stopped at {ms}")``,那种日志没法聚合。
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
    stream: Any = None,
) -> None:
    """装配根 logger。进程启动时调一次。

    Args:
        level: 根 logger 的级别。
        log_viewer_content: 要不要把弹幕正文原样写进日志。默认关,那是观众的话，
            排查问题时再开。
        stream: 日志写到哪，默认 stderr。
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonFormatter(log_viewer_content=log_viewer_content))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))
    # 第三方库的噪音压下去
    for noisy in ("websockets", "asyncio", "aiohttp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
