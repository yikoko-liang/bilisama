"""The side model: one cheap chat-completions call, used by background jobs.

Proactive topics and memory distillation both ride this. It is deliberately
not a SpeechLink — it never touches the realtime session — and deliberately
minimal: no streaming, no tools, no thinking (the config pins both off, plan
section 4.7), one bounded request with a timeout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

if TYPE_CHECKING:
    from bilisama.config.schema import SideModelConfig

__all__ = ["OpenAICompatSideModel", "SideModel", "SideModelError"]


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
        }
        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise SideModelError(f"侧路模型返回 {resp.status}: {body}")
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise SideModelError(f"侧路模型请求失败: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SideModelError(f"侧路模型响应形状不对: {str(data)[:200]}") from exc
        return str(content or "")

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
