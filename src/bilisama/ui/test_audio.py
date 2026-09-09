"""Prepare cached test speech and safely retire its audible monitor."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import threading
import wave
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

from bilisama.config.schema import TestVoiceConfig
from bilisama.obs.logging import get_logger
from bilisama.ui.audio import AudioBroker
from bilisama.ui.test_seed_tts import SeedTestVoiceClient, SeedTestVoiceError, validate_pcm

log = get_logger(__name__)

_SAMPLE_RATE = 16_000
_MAX_FRAMES = _SAMPLE_RATE * 600
_MAX_FILE_BYTES = _MAX_FRAMES * 2 + 65_536
_CACHE_PUBLISH_TIMEOUT_S = 10.0
_CACHE_CLEANUP_TIMEOUT_S = 2.0


async def stop_test_voice_monitor(
    broker: AudioBroker | None, on_failure: Callable[[], None]
) -> None:
    """Keep live inputs gated if the page cannot confirm monitoring stopped."""
    if broker is None:
        return
    stopped = False
    try:
        broker.clear_test_voice()
        await broker.wait_test_voice_clear()
        stopped = True
    finally:
        if not stopped:
            on_failure()


class TestVoiceAudioError(RuntimeError):
    """A user-actionable asset preparation failure."""

    __test__ = False


class _InvalidWave(ValueError):
    """A cache entry or synthesis result does not meet the PCM contract."""


def _platform_name() -> str:
    return sys.platform


def _read_pcm(path: Path) -> bytes:
    """Reject links, special files, truncated data, and unbounded WAV headers."""
    if path.is_symlink():
        raise TestVoiceAudioError("测试语音缓存不能使用符号链接，请换一个缓存目录。")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TestVoiceAudioError("测试语音缓存不能使用符号链接，请换一个缓存目录。") from exc
        raise
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise _InvalidWave("invalid WAV file size or type")
        try:
            with wave.open(source, "rb") as recording:
                frames = recording.getnframes()
                if (
                    recording.getframerate() != _SAMPLE_RATE
                    or recording.getnchannels() != 1
                    or recording.getsampwidth() != 2
                    or recording.getcomptype() != "NONE"
                    or not 0 < frames <= _MAX_FRAMES
                ):
                    raise _InvalidWave("expected mono PCM16 at 16kHz")
                pcm = recording.readframes(frames)
                if len(pcm) != frames * 2:
                    raise _InvalidWave("truncated PCM data")
                return pcm
        except (wave.Error, EOFError) as exc:
            raise _InvalidWave("unreadable WAV data") from exc


def _cached_pcm(path: Path) -> bytes | None:
    try:
        return _read_pcm(path)
    except (FileNotFoundError, _InvalidWave):
        return None


def _publish_pcm(cache_dir: Path, path: Path, pcm: bytes, stopped: threading.Event) -> None:
    """Publish a complete WAV atomically without exposing a partial cache entry."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".voice-", dir=cache_dir) as directory:
        output = Path(directory) / "speech.wav"
        with wave.open(str(output), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(_SAMPLE_RATE)
            for offset in range(0, len(pcm), 65_536):
                if stopped.is_set():
                    return
                recording.writeframesraw(pcm[offset : offset + 65_536])
        if stopped.is_set():
            return
        _read_pcm(output)
        if not stopped.is_set():
            output.replace(path)


def _report_late_publish_failure(task: asyncio.Task[None]) -> None:
    """Observe an owned worker even if an unresponsive filesystem outlives cleanup."""
    if not task.cancelled() and (failure := task.exception()) is not None:
        log.error("ui.test_voice_cache_cleanup_failed", error_type=type(failure).__name__)


async def _finish_cache_worker(task: asyncio.Task[None]) -> None:
    """Keep cancellation bounded while allowing the worker to remove private files."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CACHE_CLEANUP_TIMEOUT_S
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
            return
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise TestVoiceAudioError(
                    "测试语音缓存清理任务被终止，请检查缓存目录后重试。"
                ) from exc
            # Repeated stop clicks must not abandon an already-cancelling worker.
            continue
        except TimeoutError as exc:
            task.add_done_callback(_report_late_publish_failure)
            raise TestVoiceAudioError(
                "测试语音缓存清理超时，后台仍在等待磁盘操作结束，请检查缓存目录后重试。"
            ) from exc
        except (wave.Error, _InvalidWave) as exc:
            raise TestVoiceAudioError("测试语音缓存清理失败，请检查录音格式后重试。") from exc


class TestVoiceAudio:
    """Cache validated 16kHz mono PCM16 WAV files from Seed TTS 2.

    macOS say remains an explicit legacy option, never a fallback on API errors.
    The cache key covers all sound-affecting settings but never credentials.
    This class does not play or stream audio. The runner owns real-time pacing.
    """

    __test__ = False

    def __init__(
        self,
        cache_dir: Path,
        *,
        config: TestVoiceConfig | None = None,
        engine: Literal["seed", "say"] = "seed",
        voice: str = "Tingting",
        process_timeout_s: float = 30.0,
    ) -> None:
        if not voice.strip() or len(voice) > 64 or any(ord(char) < 32 for char in voice):
            raise TestVoiceAudioError("测试语音的本机音色名称无效。")
        if not math.isfinite(process_timeout_s) or process_timeout_s <= 0:
            raise TestVoiceAudioError("测试语音生成超时必须是正数。")
        if engine not in ("seed", "say"):
            raise TestVoiceAudioError("测试语音生成器无效，请选择 Seed TTS 2 或显式本机 say。")
        self._cache_dir = cache_dir.resolve()
        self._config = (config or TestVoiceConfig()).model_copy(deep=True)
        self._engine = engine
        self._voice = voice
        self._process_timeout_s = process_timeout_s
        self._lock = asyncio.Lock()

    def _path(self, text: str) -> Path:
        if not text.strip() or len(text) > 2000 or "\x00" in text:
            raise TestVoiceAudioError("测试语音需要 1—2000 个字符，且不能包含空字符。")
        if self._engine == "say":
            key = f"say-pcm16-mono-16000-v1\x00{self._voice}\x00{text}"
        else:
            key = json.dumps(
                {
                    "engine": "seed-sse-pcm16-mono-v1",
                    "endpoint": self._config.endpoint,
                    "resource_id": self._config.resource_id,
                    "speaker": self._config.speaker,
                    "sample_rate": _SAMPLE_RATE,
                    "speech_rate": self._config.speech_rate,
                    "text": text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return self._cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.wav"

    async def prepare(self, text: str) -> Path:
        """Return a valid local WAV, preparing it once if no valid cache exists."""
        path = self._path(text)
        async with self._lock:
            try:
                if await asyncio.to_thread(_cached_pcm, path) is not None:
                    return path
                if self._engine == "seed":
                    await self._synthesize_seed(text, path)
                    return path
                if _platform_name() != "darwin":
                    raise TestVoiceAudioError("自动预制测试语音目前仅支持 macOS，请使用预制录音。")
                executable = shutil.which("say")
                if executable is None:
                    raise TestVoiceAudioError("未找到 macOS say 组件，请检查系统语音功能后重试。")
                await self._synthesize(text, path, executable)
                return path
            except OSError as exc:
                raise TestVoiceAudioError(
                    "无法准备测试语音文件，请检查缓存目录的读写权限后重试。"
                ) from exc

    async def pcm(self, text: str) -> bytes:
        """Return validated signed little-endian PCM16 without a WAV header."""
        path = await self.prepare(text)
        try:
            return await asyncio.to_thread(_read_pcm, path)
        except (OSError, _InvalidWave) as exc:
            raise TestVoiceAudioError(
                "测试语音文件读取失败，请重新准备 16kHz 单声道 PCM16 录音。"
            ) from exc

    async def _synthesize_seed(self, text: str, path: Path) -> None:
        try:
            pcm = await SeedTestVoiceClient(self._config).synthesize(text)
            validate_pcm(pcm)
        except SeedTestVoiceError as exc:
            raise TestVoiceAudioError(str(exc)) from exc
        stopped = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(_publish_pcm, self._cache_dir, path, pcm, stopped)
        )
        try:
            async with asyncio.timeout(_CACHE_PUBLISH_TIMEOUT_S):
                await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            await _finish_cache_worker(worker)
            raise
        except TimeoutError as exc:
            stopped.set()
            await _finish_cache_worker(worker)
            raise TestVoiceAudioError("测试语音缓存写入超时，请检查缓存目录后重试。") from exc
        except (wave.Error, _InvalidWave) as exc:
            raise TestVoiceAudioError("测试语音缓存写入失败，请检查录音格式后重试。") from exc

    async def _synthesize(self, text: str, path: Path, executable: str) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix=".voice-", dir=self._cache_dir)
        try:
            work = Path(temporary.name)
            source = work / "input.txt"
            output = work / "speech.wav"
            await asyncio.to_thread(source.write_text, text, encoding="utf-8")
            await self._execute(
                executable,
                "-v",
                self._voice,
                "-f",
                str(source),
                "-o",
                str(output),
                "--file-format=WAVE",
                "--data-format=LEI16@16000",
                "--channels=1",
            )
            try:
                await asyncio.to_thread(_read_pcm, output)
            except (OSError, _InvalidWave) as exc:
                raise TestVoiceAudioError(
                    "本机生成的测试语音不是有效的 16kHz 单声道 PCM16 WAV。"
                ) from exc
            # Atomic publication prevents a second runner from seeing partial audio.
            await asyncio.to_thread(output.replace, path)
        finally:
            await asyncio.to_thread(temporary.cleanup)

    async def _execute(self, *args: str) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(self._process_timeout_s):
                try:
                    process = await asyncio.create_subprocess_exec(
                        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                except OSError as exc:
                    raise TestVoiceAudioError(
                        "本机测试语音生成器启动失败，请检查系统语音组件。"
                    ) from exc
                await process.communicate()
        except TimeoutError as exc:
            if process is not None:
                await self._kill_and_reap(process)
            raise TestVoiceAudioError("本机测试语音生成超时，请缩短文本后重试。") from exc
        except asyncio.CancelledError:
            if process is not None:
                await self._kill_and_reap(process)
            raise
        except OSError as exc:
            if process is not None:
                await self._kill_and_reap(process)
            raise TestVoiceAudioError("本机测试语音生成失败，请检查系统语音组件后重试。") from exc
        if process.returncode != 0:
            raise TestVoiceAudioError("本机测试语音生成失败，请确认已安装所选系统音色。")

    @staticmethod
    async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            # The owned child can exit between the return-code check and kill.
            with suppress(ProcessLookupError):
                process.kill()
        try:
            async with asyncio.timeout(2.0):
                await process.wait()
        except TimeoutError:
            log.warning("ui.test_voice_reap_timeout")
