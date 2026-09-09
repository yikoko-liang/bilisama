"""Seed TTS tests use in-memory SSE, never a real paid endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self, cast

import aiohttp
import pytest

from bilisama import secrets
from bilisama.config.schema import TestVoiceConfig
from bilisama.ui import test_seed_tts as seed_module
from bilisama.ui.test_seed_tts import SeedTestVoiceClient, SeedTestVoiceError

PCM = b"\x00\x20" * 512


def _sse(code: int = 0, pcm: bytes | None = PCM, **extra: object) -> bytes:
    data = base64.b64encode(pcm).decode() if pcm is not None else None
    return f"data: {json.dumps({'code': code, 'data': data, **extra})}\n\n".encode()


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.entered = asyncio.Event()
        self.block = False

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class _Response:
    def __init__(self) -> None:
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.content = _Stream([_sse(), _sse(20000000, None)])
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


class _Session:
    def __init__(self) -> None:
        self.response = _Response()
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.options: dict[str, Any] = {}
        self.failure: aiohttp.ClientError | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def post(self, endpoint: str, **options: Any) -> _Response:
        self.requests.append((endpoint, options))
        if self.failure is not None:
            raise self.failure
        return self.response


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Session:
    session = _Session()

    def make(**options: Any) -> aiohttp.ClientSession:
        session.options = options
        return cast(aiohttp.ClientSession, session)

    monkeypatch.setattr(aiohttp, "ClientSession", make)
    monkeypatch.setattr(secrets, "resolve", lambda _ref: "test-key-not-real")
    return session


async def test_seed_request_uses_official_audio_params_and_stream_completion(
    wire: _Session,
) -> None:
    cfg = TestVoiceConfig(speech_rate=10)
    assert await SeedTestVoiceClient(cfg).synthesize("Mia，什么是工具调用？") == PCM
    endpoint, request = wire.requests[0]
    assert endpoint == cfg.endpoint
    assert request["allow_redirects"] is False
    headers = request["headers"]
    assert headers["X-Api-Key"] == "test-key-not-real"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert len(headers["X-Api-Request-Id"]) == 36
    params = request["json"]["req_params"]
    assert params["text"] == "Mia，什么是工具调用？"
    assert params["speaker"] == "zh_female_vv_uranus_bigtts"
    assert params["audio_params"] == {"format": "pcm", "sample_rate": 16000, "speech_rate": 10}
    assert "sample_rate" not in params
    assert wire.options["timeout"].total == 60
    assert wire.closed and wire.response.closed


async def test_sse_supports_fragmented_crlf_whitespace_comments_and_multiline_data(
    wire: _Session,
) -> None:
    encoded = base64.b64encode(PCM).decode()
    raw = (
        ": heartbeat\r\nevent: chunk\r\n  data: {\r\n"
        f' data: "code":0, "data":"{encoded}"}}\r\n\r\n'
    ).encode() + _sse(20000000, None).rstrip()
    wire.response.content.chunks = [bytes([byte]) for byte in raw]
    assert await SeedTestVoiceClient(TestVoiceConfig()).synthesize("不要截断音频。") == PCM


async def test_missing_completion_does_not_return_partial_audio(wire: _Session) -> None:
    wire.response.content.chunks = [_sse()]
    with pytest.raises(SeedTestVoiceError, match="完整"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("只收到一半。")
    assert wire.closed and wire.response.closed


@pytest.mark.parametrize(
    "status,match", [(401, "凭据"), (403, "权限"), (429, "额度"), (503, "稍后"), (302, "重定向")]
)
async def test_http_failures_are_actionable_without_provider_body(
    wire: _Session, status: int, match: str
) -> None:
    wire.response.status = status
    wire.response.content.chunks = [b"provider-private-body"]
    with pytest.raises(SeedTestVoiceError, match=match) as failure:
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("服务拒绝请求。")
    assert "test-key" not in str(failure.value) and "provider-private" not in str(failure.value)
    assert wire.closed and wire.response.closed


async def test_provider_failure_code_never_leaks_message_or_cached_pcm(wire: _Session) -> None:
    wire.response.content.chunks = [_sse(), _sse(45000000, None, message="private-key: user text")]
    with pytest.raises(SeedTestVoiceError, match="45000000") as failure:
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("错误回执。")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"data: {broken}\n\n",
        b"data: []\n\n",
        b'data: {"data":"AA=="}\n\n',
        b'data: {"code":0,"data":"bad%%%"}\n\n',
    ],
)
async def test_malformed_sse_fails_instead_of_skipping_bad_events(
    wire: _Session, payload: bytes
) -> None:
    wire.response.content.chunks = [payload, _sse(20000000, None)]
    with pytest.raises(SeedTestVoiceError):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("不能跳过坏数据。")
    assert wire.closed and wire.response.closed


@pytest.mark.parametrize(
    "pcm", [b"", b"x", b"RIFF" + bytes(40), b"OggS" + bytes(40), b"ID3" + bytes(41)]
)
async def test_empty_odd_or_container_audio_is_not_pcm16(wire: _Session, pcm: bytes) -> None:
    wire.response.content.chunks = [_sse(pcm=pcm), _sse(20000000, None)]
    with pytest.raises(SeedTestVoiceError, match=r"音频|PCM"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("返回的格式不对。")


async def test_non_sse_http_success_is_rejected(wire: _Session) -> None:
    wire.response.headers["Content-Type"] = "application/json"
    with pytest.raises(SeedTestVoiceError, match="流式"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("不是音频响应。")


async def test_oversized_audio_is_bounded(wire: _Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_module, "MAX_PCM_BYTES", 100)
    with pytest.raises(SeedTestVoiceError, match="上限"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("音频过长。")


async def test_oversized_sse_comments_are_bounded(
    wire: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "MAX_RESPONSE_BYTES", 100)
    wire.response.content.chunks = [b":" + b"x" * 101]
    with pytest.raises(SeedTestVoiceError, match="上限"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("不能无限读心跳。")


async def test_timeout_closes_stream_and_session(wire: _Session) -> None:
    wire.response.content.block = True
    with pytest.raises(SeedTestVoiceError, match="超时"):
        await SeedTestVoiceClient(TestVoiceConfig(request_timeout_s=0.01)).synthesize("卡住了。")
    assert wire.closed and wire.response.closed


async def test_cancel_closes_stream_and_session_without_wrapping_cancellation(
    wire: _Session,
) -> None:
    wire.response.content.block = True
    task = asyncio.create_task(SeedTestVoiceClient(TestVoiceConfig()).synthesize("停止这条测试。"))
    await asyncio.wait_for(wire.response.content.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.closed and wire.response.closed


async def test_missing_key_is_local_and_never_sends_request(
    wire: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "resolve", lambda _ref: None)
    with pytest.raises(SeedTestVoiceError, match="api_key_ref"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("没有凭据。")
    assert wire.requests == []


async def test_network_error_is_safe_and_closes_session(wire: _Session) -> None:
    wire.failure = aiohttp.ClientConnectionError("private endpoint and key")
    with pytest.raises(SeedTestVoiceError, match="网络") as failure:
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("网络不通。")
    assert "private" not in str(failure.value) and wire.closed


async def test_single_sse_line_is_bounded_across_network_chunks(
    wire: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "MAX_SSE_LINE_BYTES", 100)
    wire.response.content.chunks = [b":" + b"x" * 50, b"x" * 50, b"\n\n"]
    with pytest.raises(SeedTestVoiceError, match=r"单行.*上限"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("不能无限累计一行。")
    assert wire.closed and wire.response.closed


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/tts",
        "https://user:password@example.com/tts",
        "file:///tmp/tts",
        "https://example.com/tts#fragment",
        "https://[broken",
    ],
)
async def test_unsafe_endpoint_does_not_send_credentials(wire: _Session, endpoint: str) -> None:
    with pytest.raises(SeedTestVoiceError, match="接口"):
        await SeedTestVoiceClient(TestVoiceConfig(endpoint=endpoint)).synthesize("不能泄露凭据。")
    assert wire.requests == []


@pytest.mark.parametrize("text", ["", " \t\n", "字" * 2001, "不能\x00用"])
async def test_invalid_text_does_not_send_request(wire: _Session, text: str) -> None:
    with pytest.raises(SeedTestVoiceError, match="字符"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize(text)
    assert wire.requests == []


async def test_header_newline_is_rejected_locally(wire: _Session) -> None:
    with pytest.raises(SeedTestVoiceError, match="换行"):
        await SeedTestVoiceClient(TestVoiceConfig(resource_id="seed\r\nother: header")).synthesize(
            "不能注入头。"
        )
    assert wire.requests == []


async def test_multi_chunk_audio_is_joined_without_alignment_assumptions(wire: _Session) -> None:
    wire.response.content.chunks = [_sse(pcm=PCM[:1]), _sse(pcm=PCM[1:]), _sse(20000000, None)]
    assert await SeedTestVoiceClient(TestVoiceConfig()).synthesize("网络块不用刚好偶数。") == PCM


async def test_too_deep_json_is_a_safe_protocol_error(wire: _Session) -> None:
    wire.response.content.chunks = [b"data: " + b"[" * 2000 + b"]" * 2000 + b"\n\n"]
    with pytest.raises(SeedTestVoiceError, match="回执"):
        await SeedTestVoiceClient(TestVoiceConfig()).synthesize("畸形嵌套不能影响运行。")
