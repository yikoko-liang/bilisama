"""Bounded Seed TTS 2 SSE requests for synthetic test input, not assistant output."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
from urllib.parse import urlsplit

import aiohttp

from bilisama import secrets
from bilisama.config.schema import TestVoiceConfig

SAMPLE_RATE = 16_000
MAX_PCM_BYTES = SAMPLE_RATE * 2 * 600
MAX_RESPONSE_BYTES = MAX_PCM_BYTES * 4 // 3 + 1_048_576
MAX_SSE_LINE_BYTES = 1_048_576


class SeedTestVoiceError(RuntimeError):
    """A safe, actionable synthesis error without credentials or provider bodies."""


def validate_pcm(pcm: bytes) -> None:
    """Require bounded raw PCM16; sample rate follows the requested API contract."""
    if len(pcm) > MAX_PCM_BYTES:
        raise SeedTestVoiceError("测试语音超过 10 分钟上限，请缩短文本后重试。")
    if not pcm or len(pcm) % 2 or pcm.startswith((b"RIFF", b"OggS", b"ID3", b"fLaC")):
        raise SeedTestVoiceError("测试语音返回的音频不是有效 PCM16，请检查 TTS 音频格式配置。")


def _http_error(status: int) -> SeedTestVoiceError:
    if status == 401:
        detail = "凭据无效，请更新 test_voice.api_key_ref 对应的环境变量"
    elif status == 403:
        detail = "没有调用权限，请在火山控制台开通 Seed TTS 2 并检查资源授权"
    elif status == 429:
        detail = "额度或并发已达上限，请检查火山控制台额度并稍后重试"
    elif 300 <= status < 400:
        detail = "不接受重定向，请在 test_voice.endpoint 填写最终 HTTPS 接口"
    elif status >= 500:
        detail = "服务暂时不可用，请稍后重试"
    else:
        detail = "请求被拒绝，请检查 test_voice 的接口、资源和音色配置"
    return SeedTestVoiceError(f"测试语音生成失败（HTTP {status}）：{detail}。")


class _SSEAudio:
    """Parse SSE lines across arbitrary network boundaries with bounded storage."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._data: list[bytes] = []
        self._pcm = bytearray()
        self._received = 0
        self.complete = False

    def feed(self, chunk: bytes) -> None:
        self._received += len(chunk)
        if self._received > MAX_RESPONSE_BYTES:
            raise SeedTestVoiceError("测试语音响应超过大小上限，请缩短文本并检查 TTS 接口。")
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            if self.complete:
                return
            self._line.extend(piece)
            if len(self._line) > MAX_SSE_LINE_BYTES:
                raise SeedTestVoiceError("测试语音响应单行超过大小上限，请检查 TTS 接口。")
            if index < len(pieces) - 1:
                self._consume_line(bytes(self._line))
                self._line.clear()

    def finish(self) -> bytes:
        if not self.complete:
            if self._line:
                self._consume_line(bytes(self._line))
                self._line.clear()
            self._dispatch()
        if not self.complete:
            raise SeedTestVoiceError("测试语音未收到完整结束回执，请检查网络后重试。")
        pcm = bytes(self._pcm)
        validate_pcm(pcm)
        return pcm

    def _consume_line(self, raw: bytes) -> None:
        line = raw.rstrip(b"\r").lstrip(b" \t")
        if not line:
            self._dispatch()
        elif line.startswith(b"data:"):
            self._data.append(line[5:].lstrip(b" "))

    def _dispatch(self) -> None:
        if not self._data or self.complete:
            return
        raw = b"\n".join(self._data)
        self._data.clear()
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise SeedTestVoiceError("测试语音流式回执格式无效，请检查 TTS 接口后重试。") from exc
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            raise SeedTestVoiceError("测试语音流式回执缺少有效状态码，请检查 TTS 接口。")
        code = event["code"]
        if code not in (0, 20000000):
            raise SeedTestVoiceError(
                f"测试语音服务返回错误码 {code}，请检查火山控制台的音色权限、资源和额度后重试。"
            )
        encoded = event.get("data")
        if encoded is not None:
            if not isinstance(encoded, str):
                raise SeedTestVoiceError("测试语音回执中的音频格式无效，请检查 TTS 接口。")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise SeedTestVoiceError("测试语音回执包含损坏的音频数据，请重试。") from exc
            if len(self._pcm) + len(pcm) > MAX_PCM_BYTES:
                raise SeedTestVoiceError("测试语音超过 10 分钟上限，请缩短文本后重试。")
            self._pcm.extend(pcm)
        self.complete = code == 20000000


class SeedTestVoiceClient:
    """Generate test input without touching the realtime provider or its voice."""

    def __init__(self, config: TestVoiceConfig) -> None:
        self._config = config.model_copy(deep=True)

    async def synthesize(self, text: str) -> bytes:
        if not text.strip() or len(text) > 2000 or "\x00" in text:
            raise SeedTestVoiceError("测试语音需要 1—2000 个字符，且不能包含空字符。")
        config = self._config
        try:
            endpoint = urlsplit(config.endpoint)
        except ValueError as exc:
            raise SeedTestVoiceError("测试语音接口地址无效，请检查 test_voice.endpoint。") from exc
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.fragment
        ):
            raise SeedTestVoiceError(
                "测试语音接口必须是无登录信息的 HTTPS 地址，请检查 test_voice.endpoint。"
            )
        key = secrets.resolve(config.api_key_ref)
        if not key:
            raise SeedTestVoiceError(
                "未配置测试语音凭据，请设置 test_voice.api_key_ref 对应的环境变量后重试。"
            )
        if any(char in key + config.resource_id for char in ("\r", "\n")):
            raise SeedTestVoiceError(
                "测试语音凭据或资源配置包含换行，请检查环境变量和 test_voice 配置。"
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Api-Key": key,
            "X-Api-Resource-Id": config.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        body = {
            "user": {"uid": "bilisama-test-voice"},
            "req_params": {
                "text": text,
                "speaker": config.speaker,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": SAMPLE_RATE,
                    "speech_rate": config.speech_rate,
                },
            },
        }
        try:
            async with asyncio.timeout(config.request_timeout_s):
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=config.request_timeout_s), trust_env=False
                ) as session:
                    async with session.post(
                        config.endpoint, headers=headers, json=body, allow_redirects=False
                    ) as response:
                        if response.status != 200:
                            raise _http_error(response.status)
                        content_type = response.headers.get("Content-Type", "").split(";")[0]
                        if content_type.strip().lower() != "text/event-stream":
                            raise SeedTestVoiceError(
                                "测试语音接口未返回流式音频，请检查 test_voice.endpoint。"
                            )
                        stream = _SSEAudio()
                        async for chunk in response.content.iter_chunked(8192):
                            stream.feed(chunk)
                            if stream.complete:
                                break
                        return stream.finish()
        except TimeoutError as exc:
            raise SeedTestVoiceError("测试语音生成超时，请检查网络、缩短文本后重试。") from exc
        except aiohttp.ClientError as exc:
            raise SeedTestVoiceError(
                "测试语音网络请求失败，请检查网络和 test_voice.endpoint 后重试。"
            ) from exc
