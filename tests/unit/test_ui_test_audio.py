"""Seed and explicit legacy voice assets are bounded, cached, and cancellable."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import wave
from pathlib import Path
from typing import cast

import pytest

from bilisama.config.schema import TestVoiceConfig
from bilisama.ui import test_audio as audio_module
from bilisama.ui.test_audio import TestVoiceAudio as VoiceAudio
from bilisama.ui.test_audio import TestVoiceAudioError as VoiceAudioError
from bilisama.ui.test_seed_tts import SeedTestVoiceClient, SeedTestVoiceError

PCM = b"\x00\x20" * 320


def _wave(path: Path, *, rate: int = 16000, channels: int = 1, width: int = 2) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(PCM)


class _Synthesis:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.texts: list[str] = []
        self.rate = 16000
        self.channels = 1
        self.width = 2

    async def run(self, *args: str) -> None:
        self.commands.append(args)
        source = Path(args[args.index("-f") + 1])
        self.texts.append(await asyncio.to_thread(source.read_text, encoding="utf-8"))
        destination = Path(args[args.index("-o") + 1])
        await asyncio.to_thread(
            _wave, destination, rate=self.rate, channels=self.channels, width=self.width
        )


@pytest.fixture
def synth(monkeypatch: pytest.MonkeyPatch) -> _Synthesis:
    monkeypatch.setattr(audio_module, "_platform_name", lambda: "darwin")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/say")
    return _Synthesis()


async def test_voice_is_cached_as_valid_pcm_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    audio = VoiceAudio(tmp_path / "cache", engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    first = await audio.prepare("Mia，先听我把这句话讲完。")
    assert first.parent == tmp_path / "cache"
    assert first.suffix == ".wav" and len(first.stem) == 64
    assert await audio.pcm("Mia，先听我把这句话讲完。") == PCM
    assert len(synth.commands) == 1
    assert "--file-format=WAVE" in synth.commands[0]
    assert "--data-format=LEI16@16000" in synth.commands[0]
    assert "--channels=1" in synth.commands[0]
    assert list(first.parent.iterdir()) == [first], "temporary scripts must be removed"


async def test_text_never_becomes_a_path_or_shell_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    text = "../别动这个目录；$(echo 错误示例)；--voice=另一人"
    path = await audio.prepare(text)
    assert path.parent == tmp_path and path.stem.isalnum()
    assert synth.texts == [text]
    assert text not in synth.commands[0], "the synthesizer reads a private input file"


async def test_concurrent_requests_reuse_one_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    paths = await asyncio.gather(*(audio.prepare("等我说完。") for _ in range(4)))
    assert len(set(paths)) == 1 and len(synth.commands) == 1


async def test_cache_key_separates_voice_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    first = VoiceAudio(tmp_path, engine="say")
    second = VoiceAudio(tmp_path, engine="say", voice="Meijia")
    monkeypatch.setattr(first, "_execute", synth.run)
    monkeypatch.setattr(second, "_execute", synth.run)
    paths = {
        await first.prepare("你先说。"),
        await first.prepare("我先说。"),
        await second.prepare("你先说。"),
    }
    assert len(paths) == 3


@pytest.mark.parametrize("text", ["", " \n\t", "包含\x00字符", "说" * 2001])
async def test_invalid_text_is_rejected_without_generation(tmp_path: Path, text: str) -> None:
    with pytest.raises(VoiceAudioError, match="测试语音"):
        await VoiceAudio(tmp_path, engine="say").prepare(text)
    assert list(tmp_path.iterdir()) == []


async def test_corrupt_cached_audio_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    path = await audio.prepare("重测这一句。")
    await asyncio.to_thread(path.write_bytes, b"broken")
    assert await audio.pcm("重测这一句。") == PCM
    assert len(synth.commands) == 2


@pytest.mark.parametrize("field,value", [("rate", 24000), ("channels", 2), ("width", 1)])
async def test_wrong_generated_format_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synth: _Synthesis,
    field: str,
    value: int,
) -> None:
    setattr(synth, field, value)
    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    with pytest.raises(VoiceAudioError, match="16kHz"):
        await audio.prepare("这个格式不对。")
    assert list(tmp_path.iterdir()) == []


async def test_symlink_cache_cannot_escape_to_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    outside = tmp_path / "recording.wav"
    _wave(outside)
    audio = VoiceAudio(tmp_path / "cache", engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    path = await audio.prepare("不要跟随链接。")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(VoiceAudioError, match="符号链接"):
        await audio.prepare("不要跟随链接。")
    assert outside.is_file() and len(synth.commands) == 1


async def test_non_macos_reports_clear_unsupported_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audio_module, "_platform_name", lambda: "linux")
    with pytest.raises(VoiceAudioError, match=r"macOS.*录音"):
        await VoiceAudio(tmp_path, engine="say").prepare("暂不支持。")


async def test_prebuilt_valid_cache_works_without_macos_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", synth.run)
    await audio.prepare("已预制的录音。")
    monkeypatch.setattr(audio_module, "_platform_name", lambda: "linux")
    assert await audio.pcm("已预制的录音。") == PCM


async def test_missing_synthesizer_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(VoiceAudioError, match="say"):
        await VoiceAudio(tmp_path, engine="say").prepare("缺少组件。")


async def test_cancelled_preparation_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    entered = asyncio.Event()

    async def blocked(*_args: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", blocked)
    task = asyncio.create_task(audio.prepare("这次取消。"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


class _Process:
    def __init__(self, *, blocked: bool = False, code: int = 0) -> None:
        self.blocked = blocked
        self.returncode: int | None = None if blocked else code
        self.entered = asyncio.Event()
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[None, None]:
        self.entered.set()
        if self.blocked:
            await asyncio.Event().wait()
        return None, None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.reaped = True
        return self.returncode if self.returncode is not None else 0


def _spawn_stub(monkeypatch: pytest.MonkeyPatch, process: _Process) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []

    async def spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        assert "shell" not in kwargs
        commands.append(args)
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return commands


async def test_subprocess_timeout_kills_and_reaps_its_own_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(blocked=True)
    _spawn_stub(monkeypatch, process)
    audio = VoiceAudio(tmp_path, engine="say", process_timeout_s=0.01)
    with pytest.raises(VoiceAudioError, match="超时"):
        await audio._execute("say", "-o", "example.wav")
    assert process.killed and process.reaped


async def test_subprocess_cancellation_kills_and_reaps_its_own_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(blocked=True)
    _spawn_stub(monkeypatch, process)
    audio = VoiceAudio(tmp_path, engine="say")
    task = asyncio.create_task(audio._execute("say", "-o", "example.wav"))
    await asyncio.wait_for(process.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.reaped


async def test_nonzero_exit_does_not_include_command_or_text_in_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawn_stub(monkeypatch, _Process(code=7))
    with pytest.raises(VoiceAudioError, match="失败") as failure:
        await VoiceAudio(tmp_path, engine="say")._execute("say", "私密测试内容")
    assert "私密测试内容" not in str(failure.value)


@pytest.mark.parametrize("invalid", [b"", b"not a wave"])
async def test_missing_or_unreadable_synthesis_never_leaves_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis, invalid: bytes
) -> None:
    async def broken(*args: str) -> None:
        destination = Path(args[args.index("-o") + 1])
        await asyncio.to_thread(destination.write_bytes, invalid)

    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", broken)
    with pytest.raises(VoiceAudioError, match="16kHz"):
        await audio.prepare("不能发布损坏的音频。")
    assert list(tmp_path.iterdir()) == []


async def test_truncated_pcm_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    async def truncated(*args: str) -> None:
        await synth.run(*args)
        destination = Path(args[args.index("-o") + 1])
        content = await asyncio.to_thread(destination.read_bytes)
        await asyncio.to_thread(destination.write_bytes, content[:-2])

    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", truncated)
    with pytest.raises(VoiceAudioError, match="16kHz"):
        await audio.prepare("不能少一帧。")
    assert list(tmp_path.iterdir()) == []


async def test_synthesis_failure_removes_private_text_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    async def broken(*args: str) -> None:
        await synth.run(*args)
        raise VoiceAudioError("生成失败")

    audio = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(audio, "_execute", broken)
    with pytest.raises(VoiceAudioError, match="失败"):
        await audio.prepare("私有临时文件必须清理。")
    assert list(tmp_path.iterdir()) == []


async def test_windows_without_posix_open_flags_reports_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audio_module, "_platform_name", lambda: "win32")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    with pytest.raises(VoiceAudioError, match="macOS"):
        await VoiceAudio(tmp_path, engine="say").prepare("还不支持的平台。")


async def test_process_start_failure_has_user_facing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(VoiceAudioError, match="启动失败"):
        await VoiceAudio(tmp_path, engine="say")._execute("say")


async def test_process_wait_error_reaps_child_and_does_not_leak_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenProcess(_Process):
        async def communicate(self) -> tuple[None, None]:
            raise OSError("private system details")

    process = BrokenProcess(blocked=True)
    _spawn_stub(monkeypatch, process)
    with pytest.raises(VoiceAudioError, match="失败") as failure:
        await VoiceAudio(tmp_path, engine="say")._execute("say")
    assert process.killed and process.reaped
    assert "private" not in str(failure.value)


@pytest.fixture
def seed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[TestVoiceConfig, str]]:
    calls: list[tuple[TestVoiceConfig, str]] = []

    async def synthesize(client: SeedTestVoiceClient, text: str) -> bytes:
        calls.append((client._config, text))
        return PCM

    monkeypatch.setattr(SeedTestVoiceClient, "synthesize", synthesize)
    return calls


async def test_default_engine_uses_seed_and_publishes_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    monkeypatch.setattr(audio_module, "_platform_name", lambda: "linux")
    audio = VoiceAudio(tmp_path)
    path = await audio.prepare("Mia，你听到了吗？")
    assert await audio.pcm("Mia，你听到了吗？") == PCM
    assert path.parent == tmp_path and len(path.stem) == 64
    assert seed == [(TestVoiceConfig(), "Mia，你听到了吗？")]
    assert list(tmp_path.iterdir()) == [path]


async def test_seed_cache_separates_every_audio_identity_but_not_credentials(
    tmp_path: Path, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    config = TestVoiceConfig()
    audio = VoiceAudio(tmp_path, config=config)
    base = await audio.prepare("同一句话。")
    paths = {base}
    for update in (
        {"endpoint": "https://example.com/seed"},
        {"resource_id": "another-resource"},
        {"speaker": "another-voice"},
        {"speech_rate": 30},
    ):
        changed = config.model_copy(update=update)
        paths.add(await VoiceAudio(tmp_path, config=changed).prepare("同一句话。"))
    paths.add(await audio.prepare("不同的话。"))
    assert len(paths) == 6 and len(seed) == 6
    rotated = config.model_copy(update={"api_key_ref": "rotated", "request_timeout_s": 120.0})
    assert await VoiceAudio(tmp_path, config=rotated).prepare("同一句话。") == base
    assert len(seed) == 6


async def test_seed_config_is_snapshotted_and_never_reuses_say_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synth: _Synthesis,
    seed: list[tuple[TestVoiceConfig, str]],
) -> None:
    legacy = VoiceAudio(tmp_path, engine="say")
    monkeypatch.setattr(legacy, "_execute", synth.run)
    say_path = await legacy.prepare("不要串音色。")
    config = TestVoiceConfig(speaker="original-voice")
    audio = VoiceAudio(tmp_path, config=config)
    config.speaker = "changed-after-construction"
    seed_path = await audio.prepare("不要串音色。")
    assert seed_path != say_path and seed[0][0].speaker == "original-voice"
    assert len(synth.commands) == len(seed) == 1


async def test_seed_concurrent_prepare_only_synthesizes_once(
    tmp_path: Path, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    audio = VoiceAudio(tmp_path)
    paths = await asyncio.gather(*(audio.prepare("同一条语音。") for _ in range(4)))
    assert len(set(paths)) == len(seed) == 1


async def test_seed_failure_has_no_fallback_or_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth: _Synthesis
) -> None:
    async def failed(client: SeedTestVoiceClient, text: str) -> bytes:
        raise SeedTestVoiceError("未收到完整结束回执。")

    monkeypatch.setattr(SeedTestVoiceClient, "synthesize", failed)
    audio = VoiceAudio(tmp_path)
    monkeypatch.setattr(audio, "_execute", synth.run)
    with pytest.raises(VoiceAudioError, match="完整"):
        await audio.prepare("不要回退。")
    assert synth.commands == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", [b"", b"x", b"RIFF" + bytes(40)])
async def test_seed_invalid_pcm_never_publishes_a_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: bytes
) -> None:
    async def malformed(client: SeedTestVoiceClient, text: str) -> bytes:
        return invalid

    monkeypatch.setattr(SeedTestVoiceClient, "synthesize", malformed)
    with pytest.raises(VoiceAudioError, match="PCM"):
        await VoiceAudio(tmp_path).prepare("错误格式。")
    assert list(tmp_path.iterdir()) == []


async def test_seed_cancel_does_not_cache_and_next_run_can_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def blocked(client: SeedTestVoiceClient, text: str) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        return PCM

    monkeypatch.setattr(SeedTestVoiceClient, "synthesize", blocked)
    audio = VoiceAudio(tmp_path)
    task = asyncio.create_task(audio.prepare("停止这条语音。"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []

    async def good(client: SeedTestVoiceClient, text: str) -> bytes:
        return PCM

    monkeypatch.setattr(SeedTestVoiceClient, "synthesize", good)
    assert await audio.pcm("停止这条语音。") == PCM


async def test_seed_corrupt_cache_is_rebuilt(
    tmp_path: Path, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    audio = VoiceAudio(tmp_path)
    path = await audio.prepare("重新生成。")
    path.write_bytes(b"corrupt")
    assert await audio.pcm("重新生成。") == PCM and len(seed) == 2


async def test_seed_cache_io_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    loop_thread = threading.get_ident()
    original = audio_module._publish_pcm
    observed: list[int] = []

    def checked(cache_dir: Path, path: Path, pcm: bytes, stopped: threading.Event) -> None:
        observed.append(threading.get_ident())
        assert threading.get_ident() != loop_thread, "cache I/O must not block realtime audio"
        original(cache_dir, path, pcm, stopped)

    monkeypatch.setattr(audio_module, "_publish_pcm", checked)
    assert await VoiceAudio(tmp_path).pcm("语音输入不能被磁盘阻塞。") == PCM
    assert observed and len(seed) == 1


async def test_cancel_during_cache_write_waits_for_cleanup_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    ended = threading.Event()
    original = audio_module._publish_pcm

    def delayed(cache_dir: Path, path: Path, pcm: bytes, stopped: threading.Event) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert stopped.wait(timeout=1), "cancellation must signal the owned worker"
        original(cache_dir, path, pcm, stopped)
        ended.set()

    monkeypatch.setattr(audio_module, "_publish_pcm", delayed)
    task = asyncio.create_task(VoiceAudio(tmp_path).prepare("写缓存时取消。"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and len(seed) == 1 and list(tmp_path.iterdir()) == []


async def test_cache_write_timeout_signals_worker_and_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    ended = threading.Event()
    original = audio_module._publish_pcm
    monkeypatch.setattr(audio_module, "_CACHE_PUBLISH_TIMEOUT_S", 0.01)

    def delayed(cache_dir: Path, path: Path, pcm: bytes, stopped: threading.Event) -> None:
        assert stopped.wait(timeout=1)
        original(cache_dir, path, pcm, stopped)
        ended.set()

    monkeypatch.setattr(audio_module, "_publish_pcm", delayed)
    with pytest.raises(VoiceAudioError, match="写入超时"):
        await VoiceAudio(tmp_path).prepare("写缓存太慢。")
    assert ended.is_set() and len(seed) == 1 and list(tmp_path.iterdir()) == []


async def test_seed_publish_io_failure_is_safe_and_removes_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    original = audio_module._read_pcm

    def failed(path: Path) -> bytes:
        if path.name == "speech.wav":
            raise OSError("private filesystem error")
        return original(path)

    monkeypatch.setattr(audio_module, "_read_pcm", failed)
    with pytest.raises(VoiceAudioError, match="权限") as failure:
        await VoiceAudio(tmp_path).prepare("磁盘写失败。")
    assert "private" not in str(failure.value)
    assert len(seed) == 1 and list(tmp_path.iterdir()) == []


async def test_repeated_stop_waits_for_the_same_owned_cache_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: list[tuple[TestVoiceConfig, str]]
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = threading.Event()
    ended = threading.Event()

    def delayed(cache_dir: Path, path: Path, pcm: bytes, stopped: threading.Event) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert stopped.wait(timeout=1)
        loop.call_soon_threadsafe(cleaning.set)
        assert release.wait(timeout=1)
        ended.set()

    monkeypatch.setattr(audio_module, "_publish_pcm", delayed)
    task = asyncio.create_task(VoiceAudio(tmp_path).prepare("多次点击停止。"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "a repeated stop must still wait for cache cleanup"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set() and len(seed) == 1 and list(tmp_path.iterdir()) == []


async def test_cache_cleanup_timeout_reports_unfinished_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "_CACHE_CLEANUP_TIMEOUT_S", 0.01)
    released = asyncio.Event()
    worker = asyncio.create_task(released.wait())
    owned = cast(asyncio.Task[None], worker)
    try:
        with pytest.raises(VoiceAudioError, match="清理超时"):
            await audio_module._finish_cache_worker(owned)
        assert not worker.done()
    finally:
        released.set()
        await worker


async def test_cancelled_cache_worker_does_not_spin_forever() -> None:
    async def blocked() -> None:
        await asyncio.Event().wait()

    worker = asyncio.create_task(blocked())
    worker.cancel()
    with pytest.raises(VoiceAudioError, match="清理任务被终止"):
        await asyncio.wait_for(audio_module._finish_cache_worker(worker), 1)
