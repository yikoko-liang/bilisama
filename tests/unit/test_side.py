"""The side model's error paths, each against a real local HTTP server.

The happy path is exercised everywhere (proactive, distill); what was never
pinned is the classification promise: every failure mode surfaces as
SideModelError — the one exception background jobs catch — and never as a
naked aiohttp type that would kill the refresh task with "exception was never
retrieved" (A11 was exactly that, for TimeoutError).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from aiohttp import web

from bilisama.config.schema import SideModelConfig
from bilisama.side import OpenAICompatSideModel, SideModelError

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@contextmanager
def _capturing() -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """Collect (event, level, fields) for everything side.py logs.

    A handler rather than caplog: the promises under test are about the
    structured fields — that the prompts appear only as lengths, and that
    `error_text` survives whole — and those live on `record.fields`.
    """
    seen: list[tuple[str, str, dict[str, Any]]] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "fields", {})
            seen.append(
                (
                    record.getMessage(),
                    record.levelname.lower(),
                    fields if isinstance(fields, dict) else {},
                )
            )

    logger = logging.getLogger("bilisama.side")
    sink = _Sink()
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        logger.setLevel(previous)
        logger.removeHandler(sink)


@asynccontextmanager
async def _serving(handler: Handler) -> AsyncIterator[str]:
    """One POST /chat/completions route on an ephemeral loopback port."""
    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


def _model(base_url: str, *, timeout_s: float = 5.0) -> OpenAICompatSideModel:
    cfg = SideModelConfig.model_validate({"base_url": base_url, "model": "test-model"})
    return OpenAICompatSideModel(cfg, timeout_s=timeout_s)


async def test_a_working_endpoint_returns_the_content() -> None:
    async def ok(request: web.Request) -> web.StreamResponse:
        return web.json_response({"choices": [{"message": {"content": "好的话题"}}]})

    async with _serving(ok) as base_url:
        model = _model(base_url)
        try:
            assert await model.complete(system="s", user="u") == "好的话题"
        finally:
            await model.aclose()


async def test_non_200_becomes_a_side_model_error_with_the_status() -> None:
    async def busy(request: web.Request) -> web.StreamResponse:
        return web.Response(status=503, text="overloaded")

    async with _serving(busy) as base_url:
        model = _model(base_url)
        try:
            with pytest.raises(SideModelError, match="503"):
                await model.complete(system="s", user="u")
        finally:
            await model.aclose()


async def test_a_malformed_response_shape_is_classified_not_raised_raw() -> None:
    """A KeyError escaping here would kill the refresh task silently."""

    async def weird(request: web.Request) -> web.StreamResponse:
        return web.json_response({"unexpected": True})

    async with _serving(weird) as base_url:
        model = _model(base_url)
        try:
            with pytest.raises(SideModelError, match="形状不对"):
                await model.complete(system="s", user="u")
        finally:
            await model.aclose()


async def test_a_hanging_endpoint_times_out_as_a_side_model_error() -> None:
    """A11: aiohttp's total timeout raises BARE TimeoutError, not ClientError.
    Uncaught, it surfaced as 'exception was never retrieved' instead of a
    proactive.refresh_failed warning."""

    async def hang(request: web.Request) -> web.StreamResponse:
        # Just past the client timeout; runner.cleanup() waits this out, so
        # keeping it short keeps the whole test short.
        await asyncio.sleep(1.0)
        return web.json_response({})

    async with _serving(hang) as base_url:
        model = _model(base_url, timeout_s=0.2)
        try:
            with pytest.raises(SideModelError, match="超时"):
                await model.complete(system="s", user="u")
        finally:
            await model.aclose()


async def test_a_dead_endpoint_is_a_side_model_error_too() -> None:
    """Connection refused: the EasyConnect-not-running case every dev box hits."""

    async def ok(request: web.Request) -> web.StreamResponse:
        return web.json_response({})

    async with _serving(ok) as base_url:
        pass  # the server is torn down; the port is now dead
    model = _model(base_url)
    try:
        with pytest.raises(SideModelError, match="请求失败"):
            await model.complete(system="s", user="u")
    finally:
        await model.aclose()


async def test_a_call_is_visible_in_the_log_without_the_prompt_leaking() -> None:
    """This is the path that spends money, and it had no log line at all.

    Both ends at info — which model, how big the prompts were, how long it took,
    how much came back — and the prompt bodies themselves nowhere: `system` and
    `user` both quote danmaku.
    """
    prompt = "观众阿强刚才说的那句话"

    async def ok(request: web.Request) -> web.StreamResponse:
        return web.json_response({"choices": [{"message": {"content": "好的话题"}}]})

    async with _serving(ok) as base_url:
        model = _model(base_url)
        with _capturing() as seen:
            try:
                assert await model.complete(system="你是米娅", user=prompt) == "好的话题"
            finally:
                await model.aclose()

    started = [fields for event, _, fields in seen if event == "side.call_started"]
    finished = [fields for event, _, fields in seen if event == "side.call_finished"]
    assert len(started) == 1 and len(finished) == 1, seen
    assert {level for _, level, _ in seen} == {"info"}, "a healthy call is not a warning"

    assert started[0]["model"] == "test-model"
    assert started[0]["system_len"] == len("你是米娅")
    assert started[0]["user_len"] == len(prompt)
    assert finished[0]["status"] == 200
    assert finished[0]["reply_chars"] == len("好的话题")
    assert finished[0]["elapsed_ms"] >= 0

    flat = repr(seen)
    assert prompt not in flat, "the danmaku the prompt quotes must not reach the log"
    assert "你是米娅" not in flat, "the persona is not a log field either"


async def test_a_failing_call_warns_with_the_reason_intact() -> None:
    """`error_text` is a diagnostic name on purpose: the scrubber folds `text`
    down to a length, and folding this one would delete the only field that
    answers why the background job stopped producing topics."""

    async def busy(request: web.Request) -> web.StreamResponse:
        return web.Response(status=503, text="upstream quota exhausted")

    async with _serving(busy) as base_url:
        model = _model(base_url)
        with _capturing() as seen:
            try:
                with pytest.raises(SideModelError):
                    await model.complete(system="s", user="u")
            finally:
                await model.aclose()

    failures = [(level, fields) for event, level, fields in seen if event == "side.call_failed"]
    assert len(failures) == 1, seen
    level, fields = failures[0]
    assert level == "warning"
    assert fields["reason"] == "http_status"
    assert fields["status"] == 503
    assert "upstream quota exhausted" in fields["error_text"]
    assert not [event for event, _, _ in seen if event == "side.call_finished"]
