"""Which failures are worth retrying, and which are worth stopping for.

Plan section 4.12 asks for three classes and a latch: a network hiccup retries,
a busy backend backs off, a bad credential stops dead rather than hammering a
wrong key eight times. Everything here is transport-level, so it lives beside
the client rather than in any one adapter — a 401 means the same thing on both
hosted endpoints.

Deliberately narrow. The classes below cover what the transport itself can
tell us: HTTP status at the handshake, close codes, OS-level failures. What a
specific endpoint sends at its own session cap is NOT in here, because nobody
has watched one expire yet (debt #19); when that probe lands, its findings
belong in this table with the close code written down.
"""

from __future__ import annotations

import asyncio
import enum

import websockets

__all__ = ["ErrorClass", "classify_error", "describe"]


class ErrorClass(enum.StrEnum):
    """What to do about a transport failure."""

    RETRYABLE = "retryable"
    """A hiccup: reconnect promptly. Dropped socket, refused connection, DNS."""

    BACKOFF = "backoff"
    """The far side is full or rate-limiting. Reconnect, but slowly."""

    FATAL = "fatal"
    """Retrying cannot help: bad credentials, a wrong URL, a rejected model.
    The caller latches and reports — eight retries on a stale key is how a
    stream stays silent for an hour with a green-looking log."""


# Handshake statuses we can read a verdict off directly.
_FATAL_STATUS = frozenset({400, 401, 403, 404, 405, 409, 422})
_BACKOFF_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Close codes that mean "you are not welcome" rather than "we got cut off".
# 1008 policy violation and 1003 unsupported data are both us being wrong.
_FATAL_CLOSE = frozenset({1003, 1008})


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status a handshake rejection carries, if it carries one.

    websockets moved this attribute around across versions, so read whatever
    shape is present rather than pinning one.
    """
    response = getattr(exc, "response", None)
    for holder in (response, exc):
        code = getattr(holder, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def classify_error(exc: BaseException) -> ErrorClass:
    """Sort one transport failure into a class.

    Args:
        exc: What the transport raised, or the exception behind a close.

    Returns:
        RETRYABLE unless there is a reason to believe otherwise — an unknown
        failure is treated as transient on purpose, because the cost of one
        extra reconnect is a second and the cost of a wrong FATAL is a silent
        stream.
    """
    if isinstance(exc, asyncio.CancelledError):  # pragma: no cover - caller filters
        return ErrorClass.FATAL

    status = _status_of(exc)
    if status is not None:
        if status in _FATAL_STATUS:
            return ErrorClass.FATAL
        if status in _BACKOFF_STATUS:
            return ErrorClass.BACKOFF
        return ErrorClass.RETRYABLE

    if isinstance(exc, websockets.ConnectionClosed):
        code = getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)
        if code in _FATAL_CLOSE:
            return ErrorClass.FATAL
        return ErrorClass.RETRYABLE

    if isinstance(exc, TimeoutError | OSError):
        return ErrorClass.RETRYABLE

    return ErrorClass.RETRYABLE


def describe(cls: ErrorClass, detail: str) -> str:
    """One line a streamer can act on. CLI output is Chinese (CONTRIBUTING)."""
    if cls is ErrorClass.FATAL:
        return f"语音服务拒绝连接，重试也没用：{detail}。检查密钥、服务地址和模型名。"
    if cls is ErrorClass.BACKOFF:
        return f"语音服务忙或限流：{detail}。会放慢重试节奏。"
    return f"语音连接断了：{detail}。正在自动重连。"
