"""`bilisama dev-talk`: a human voice through OUR stack, before Electron exists.

Stage 2's acceptance line says a real person must be able to talk to it. The
audio front end is stage 6, so this is the development stand-in: microphone
(or a WAV file) in, replies out — through RealtimeClient and the dialect
codecs, not around them. Which makes it the only way to voice-test DashScope
at all: upstream's own talk client speaks GA event names exclusively, and the
DashScope endpoint answers in beta, so that client connects and then plays
silence (probed live, 2026-08-10).

Wire-level frames are allowed here on purpose. This is a dev tool standing in
for the P1 front end; the guarded layers (director/ and friends) still know
nothing below SpeechLink.

Microphone mode needs sounddevice (`uv pip install sounddevice`); WAV mode
runs on the standard library alone and writes the reply audio next to the
input.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import wave
from pathlib import Path
from typing import Any

from bilisama.config.enums import ProviderName
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import profile_for

_INPUT_RATE = 16000  # both providers take 16 kHz mono s16 uplink
_OUTPUT_RATE = 24000  # and answer at 24 kHz (plan section 3.1 table)
_FRAME_MS = 32


def _dashscope_url(model: str) -> str:
    base = os.environ.get("dashscope_url", "")  # noqa: SIM112  (path.sh 里的原名)
    if not base:
        raise SystemExit("缺环境变量 dashscope_url。先 source path.sh 再跑。")
    host = base.replace("https://", "").split("/")[0]
    return f"wss://{host}/api-ws/v1/realtime?model={model}"


def _session_frame(provider: ProviderName) -> dict[str, Any] | None:
    """The audio session setup, in the dialect the provider actually speaks.

    s2s gets nothing: its VAD runs unconditionally, and its session.update
    REQUIRES session.type="realtime" — a bare session draws "Unknown or
    invalid event" (probed live; plan section 3.1's table says the opposite
    and is wrong for v0.2.12-40). The adapter's set_context goes through
    Codec.session_patch, which writes the field, so only hand-rolled frames
    can trip on this.
    """
    if provider is ProviderName.DASHSCOPE:
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {"type": "server_vad"},
            },
        }
    return None


async def _pump_wav(client: RealtimeClient, path: Path) -> None:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != _INPUT_RATE or w.getnchannels() != 1:
            raise SystemExit(
                f"要 16kHz 单声道 WAV，拿到 {w.getframerate()}Hz {w.getnchannels()} 声道"
            )
        pcm = w.readframes(w.getnframes())
    step = 2 * _INPUT_RATE * _FRAME_MS // 1000
    for i in range(0, len(pcm), step):
        await client.push_audio(pcm[i : i + step])
    # Keep the audio clock alive with silence — stopping the stream freezes the
    # provider's sense of time (plan section 3.3 rule 7).
    silence = b"\x00\x00" * (_INPUT_RATE * _FRAME_MS // 1000)
    while True:
        await client.push_audio(silence)
        await asyncio.sleep(_FRAME_MS / 1000)


async def _pump_mic(
    client: RealtimeClient, device: int | None, speaker: _Speaker | None, mute: bool
) -> None:
    try:
        import sounddevice
    except ImportError as exc:
        raise SystemExit(
            "麦克风模式要 sounddevice：.venv/bin/python -m pip install sounddevice\n"
            "或者用 --wav 喂一段 16kHz 单声道录音。"
        ) from exc
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    def enqueue(block: bytes) -> None:
        # Runs on the loop. Drop the oldest on overflow: live audio must stay
        # live, and a raised QueueFull inside a loop callback would be logged
        # as an unhandled exception once per block — the console spam bug.
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(block)

    def on_block(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        loop.call_soon_threadsafe(enqueue, bytes(indata))

    stream = sounddevice.RawInputStream(
        samplerate=_INPUT_RATE,
        channels=1,
        dtype="int16",
        blocksize=_INPUT_RATE * _FRAME_MS // 1000,
        device=device,
        callback=on_block,
    )
    silence = b"\x00\x00" * (_INPUT_RATE * _FRAME_MS // 1000)
    with stream:
        while True:
            block = await queue.get()
            if mute and speaker is not None and speaker.busy:
                # Echo shield for open speakers: the mic goes silent while the
                # reply plays. Costs barge-in during playback — headphones keep
                # it. Silence rather than nothing: stopping the append stream
                # freezes the provider's audio clock (plan section 3.3 rule 7).
                block = silence
            await client.push_audio(block)


class _Speaker:
    """Callback-fed playback so the event loop never blocks on audio.

    The first version called RawOutputStream.write() straight from the event
    loop. Replies arrive as a burst far faster than they play, so the writes
    filled PortAudio's buffer and started BLOCKING — SpeechStarted then queued
    behind dozens of audio events, the flush ran after playback had already
    finished, and the mic queue backed up until every block logged QueueFull.
    One blocking call, three symptoms: barge-in that "does not stop playback",
    an error-spamming console, and a starved uplink. The server had cancelled
    correctly all along (probed live: status=cancelled, two late chunks).

    Now the loop appends to a ring buffer and returns; the audio thread pulls.
    flush() empties the buffer, which silences the speaker within one block.
    """

    def __init__(self, device: int | None) -> None:
        import threading

        self._buffer = bytearray()
        self._lock = threading.Lock()
        try:
            import sounddevice
        except ImportError:
            self._stream = None
            return

        def feed(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            need = len(outdata)
            with self._lock:
                chunk = bytes(self._buffer[:need])
                del self._buffer[: len(chunk)]
            outdata[: len(chunk)] = chunk
            if len(chunk) < need:
                outdata[len(chunk) :] = b"\x00" * (need - len(chunk))

        self._stream = sounddevice.RawOutputStream(
            samplerate=_OUTPUT_RATE,
            channels=1,
            dtype="int16",
            device=device,
            callback=feed,
        )
        self._stream.start()

    def play(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)

    def flush(self) -> None:
        with self._lock:
            self._buffer.clear()

    @property
    def busy(self) -> bool:
        with self._lock:
            return len(self._buffer) > 0


async def _consume(
    client: RealtimeClient, speaker: _Speaker | None, reply_wav: Path | None
) -> None:
    collected: list[bytes] = []
    async for event in client.events():
        if isinstance(event, link.UserTranscriptDone):
            print(f"你说：{event.text}")
        elif isinstance(event, link.ReplyTextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, link.ReplyAudioDelta):
            collected.append(event.pcm)
            if speaker is not None:
                speaker.play(event.pcm)
        elif isinstance(event, link.SpeechStarted):
            if speaker is not None:
                speaker.flush()  # the local half of playback.clear
        elif isinstance(event, link.ReplyDone):
            print(f"\n—— 回复结束（{event.status}）——")
            if reply_wav is not None and collected:
                with wave.open(str(reply_wav), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(_OUTPUT_RATE)
                    w.writeframes(b"".join(collected))
                print(f"回复音频已存：{reply_wav}")
                return
            collected.clear()
        elif isinstance(event, link.LinkError):
            print(f"[错误] {event.code}: {event.detail}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    provider = ProviderName(args.provider)
    profile = profile_for(provider)
    headers: dict[str, str] = {}
    if provider is ProviderName.DASHSCOPE:
        key = os.environ.get("ali_api_key", "")  # noqa: SIM112  (path.sh 里的原名)
        if not key:
            raise SystemExit("缺环境变量 ali_api_key。先 source path.sh 再跑。")
        url = _dashscope_url(args.model)
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url = args.url

    client = RealtimeClient(url, caps=profile.caps, codec=profile.codec, headers=headers)
    await client.connect()
    print(f"已连接 {provider.value}（{url.split("?")[0]}），说话即可，Ctrl-C 退出。")
    if args.wav is None and not args.mute_while_speaking:
        print("提示：外放会让 AI 听到自己的声音。戴耳机，或加 --mute-while-speaking。")
    try:
        session_frame = _session_frame(provider)
        if session_frame is not None:
            await client.send_command(session_frame)
        speaker: _Speaker | None = None
        reply_wav: Path | None = None
        if args.wav is not None:
            reply_wav = args.wav.with_name(args.wav.stem + ".reply.wav")
            pump = asyncio.create_task(_pump_wav(client, args.wav))
        else:
            speaker = _Speaker(args.output_device)
            pump = asyncio.create_task(
                _pump_mic(client, args.input_device, speaker, args.mute_while_speaking)
            )
        consume = asyncio.create_task(_consume(client, speaker, reply_wav))
        done, pending = await asyncio.wait({pump, consume}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # surface pump/consume failures instead of swallowing
        return 0
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bilisama dev-talk", description="拿真人声音测我们自己的语音链路"
    )
    parser.add_argument("--provider", choices=["s2s", "dashscope"], default="s2s")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/v1/realtime", help="s2s 服务地址")
    parser.add_argument(
        "--model", default="qwen-audio-3.0-realtime-flash", help="DashScope 的 realtime 模型名"
    )
    parser.add_argument(
        "--wav", type=Path, default=None, help="不用麦克风，喂一段 16kHz 单声道 WAV"
    )
    parser.add_argument(
        "--mute-while-speaking",
        action="store_true",
        help="播放期间闭麦（外放不戴耳机时防回声误打断；代价是播放中插不了话）",
    )
    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n再见。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
