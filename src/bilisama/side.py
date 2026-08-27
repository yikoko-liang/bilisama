"""The side model: one cheap chat-completions call, used by background jobs.

Proactive topics and memory distillation both ride this. It is deliberately
not a SpeechLink — it never touches the realtime session — and deliberately
minimal: no streaming, no tools, no thinking (the config pins both off, plan
section 4.7), one bounded request with a timeout.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from bilisama.config.schema import SideModelConfig

__all__ = ["OpenAICompatSideModel", "SideModel", "SideModelError"]

log = get_logger(__name__)


class SideModelError(Exception):
    """The side call failed. Callers log and carry on — a background job must
    never take the voice loop down with it."""


class SideModel(Protocol):
    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str: ...

    async def aclose(self) -> None: ...


class OpenAICompatSideModel:
    """POST {base_url}/chat/completions, OpenAI shape, non-streaming."""

    def __init__(self, cfg: SideModelConfig, *, api_key: str = "", timeout_s: float = 45.0) -> None:
        self._base_url = cfg.base_url.rstrip("/")
        self._model = cfg.model
        self._api_key = api_key
        # Plan section 4.7 pins both off for every background call. The prompt
        # says so too (distill.py:44), but a sentence is a request and a field
        # is a rule — a model that ignores the sentence still cannot answer with
        # a tool call it was never offered. `thinking` has no field in the
        # OpenAI shape and differs per vendor, so it stays prompt-side; the
        # config value is kept honest by its Literal["off"].
        self._tool_choice = cfg.tool_choice
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": False,
            "tool_choice": self._tool_choice,
        }
        # The only path in the product that spends money per call, and until now
        # the only one with no log line at all: a background job whose side model
        # had been 503-ing for an hour looked exactly like a background job with
        # nothing to say. Lengths, never bodies — both prompts quote danmaku.
        log.info(
            "side.call_started",
            model=self._model,
            system_len=len(system),
            user_len=len(user),
            max_tokens=max_tokens,
        )
        # time.monotonic() rather than the injected Clock: nothing schedules on
        # this number, it only dates a log line, and a FakeClock would report
        # 0 ms for a call that really took two seconds.
        started = time.monotonic()

        def elapsed_ms() -> int:
            return round((time.monotonic() - started) * 1000)

        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                status = resp.status
                if status != 200:
                    body = (await resp.text())[:200]
                    log.warning(
                        "side.call_failed",
                        model=self._model,
                        reason="http_status",
                        status=status,
                        elapsed_ms=elapsed_ms(),
                        error_text=body,
                    )
                    raise SideModelError(f"侧路模型返回 {status}: {body}")
                data = await resp.json()
        except TimeoutError as exc:
            # aiohttp's total timeout raises bare TimeoutError (verified on
            # 3.14.3), NOT ClientError — uncaught it killed the refresh task
            # with "exception was never retrieved" instead of a warning (A11).
            log.warning(
                "side.call_failed",
                model=self._model,
                reason="timeout",
                elapsed_ms=elapsed_ms(),
                error_text=f"{self._timeout.total}s 内没有响应",
            )
            raise SideModelError(f"侧路模型超时（{self._timeout.total}s）") from exc
        except aiohttp.ClientError as exc:
            log.warning(
                "side.call_failed",
                model=self._model,
                reason="transport",
                elapsed_ms=elapsed_ms(),
                error_text=str(exc)[:200],
            )
            raise SideModelError(f"侧路模型请求失败: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            log.warning(
                "side.call_failed",
                model=self._model,
                reason="bad_shape",
                status=status,
                elapsed_ms=elapsed_ms(),
                error_text=str(data)[:200],
            )
            raise SideModelError(f"侧路模型响应形状不对: {str(data)[:200]}") from exc
        reply = str(content or "")
        log.info(
            "side.call_finished",
            model=self._model,
            status=status,
            elapsed_ms=elapsed_ms(),
            reply_chars=len(reply),
        )
        return reply

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
