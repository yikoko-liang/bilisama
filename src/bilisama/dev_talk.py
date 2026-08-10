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
import itertools
import os
import signal
import sys
import wave
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from bilisama.config.enums import ProviderName
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import profile_for


class _AudioIn(Protocol):
    """What the pumps need: anything with push_audio — the raw client in wire
    mode, the fanned-out adapter in director mode."""

    async def push_audio(self, pcm: bytes) -> None: ...


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


async def _pump_wav(client: _AudioIn, path: Path) -> None:
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
    client: _AudioIn, device: int | None, speaker: _Speaker | None, mute: bool
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
    await _consume_events(client.events(), speaker, reply_wav)


async def _consume_events(
    events: AsyncIterator[link.LinkEvent], speaker: _Speaker | None, reply_wav: Path | None
) -> None:
    collected: list[bytes] = []
    async for event in events:
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


class _Fanout:
    """One adapter, several event consumers.

    SpeechLink.events() hands out one stream and the scheduler is its single
    consumer by design. Director mode also needs the frames for playback, so
    this wrapper pumps the real stream once and copies every event into each
    view — every events() call is a fresh view. Dev-tool plumbing, same
    wire-level licence as the rest of this file.
    """

    def __init__(self, inner: link.SpeechLink) -> None:
        self._inner = inner
        self._sinks: list[asyncio.Queue[link.LinkEvent]] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump(), name="dev-talk:fanout")

    async def _pump(self) -> None:
        async for event in self._inner.events():
            for sink in self._sinks:
                sink.put_nowait(event)

    def events(self) -> AsyncIterator[link.LinkEvent]:
        queue: asyncio.Queue[link.LinkEvent] = asyncio.Queue()
        self._sinks.append(queue)

        async def drain() -> AsyncIterator[link.LinkEvent]:
            while True:
                yield await queue.get()

        return drain()

    # ---- passthrough: the SpeechLink surface minus events() ----

    async def connect(self) -> None:
        await self._inner.connect()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._inner.aclose()

    async def set_context(self, instructions: str) -> None:
        await self._inner.set_context(instructions)

    async def push_audio(self, pcm: bytes) -> None:
        await self._inner.push_audio(pcm)

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        await self._inner.add_context_item(text, role=role)

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        return await self._inner.request_reply(spec)

    async def cancel(self, handle: link.ReplyHandle) -> None:
        await self._inner.cancel(handle)


def _console_viewer(name: str) -> Viewer:
    # A stable uid per name, so memory sees 测试观众 as the same person every time.
    return Viewer(uid=10000 + zlib.crc32(name.encode()) % 90000, name=name)


def _parse_console_event(text: str, seq: int) -> LiveEvent | None:
    """One typed line, one simulated live event.

    "你好" — danmaku from 测试观众; "阿强:你好" — danmaku from 阿强;
    "/sc 阿强 30 主播玩什么" — Super Chat; "/gift 阿强 52" — paid gift.
    """
    text = text.strip()
    if not text:
        return None
    event_id = f"console-{seq}"
    if text.startswith("/sc "):
        parts = text[4:].split(maxsplit=2)
        if len(parts) < 3:
            return None
        name, amount, body = parts[0], parts[1], parts[2]
        try:
            value = float(amount)
        except ValueError:
            return None
        return LiveEvent(
            kind=EventKind.SUPER_CHAT,
            viewer=_console_viewer(name),
            text=body,
            value_cny=value,
            event_id=event_id,
        )
    if text.startswith("/gift "):
        parts = text[6:].split()
        if len(parts) < 2:
            return None
        try:
            value = float(parts[1])
        except ValueError:
            return None
        return LiveEvent(
            kind=EventKind.GIFT,
            viewer=_console_viewer(parts[0]),
            gift=Gift(name="礼物", num=1, coin_type="gold", total_coin=int(value * 1000)),
            value_cny=value,
            event_id=event_id,
        )
    if text.startswith("/"):
        return None
    name, sep, body = text.partition(":")
    if not sep:
        name, sep, body = text.partition("：")
    if sep and name.strip() and body.strip():
        return LiveEvent(
            kind=EventKind.DANMAKU,
            viewer=_console_viewer(name.strip()),
            text=body.strip(),
            event_id=event_id,
        )
    return LiveEvent(
        kind=EventKind.DANMAKU,
        viewer=_console_viewer("测试观众"),
        text=text,
        event_id=event_id,
    )


async def run_director(args: argparse.Namespace) -> int:
    """The whole stage-3 assembly behind the mic, before Electron exists.

    Wire mode (the default) talks straight through RealtimeClient and knows
    nothing of L3. This mode stands the real stack up instead: persona,
    memory, distiller, proactive loop, scheduler — the same objects the
    acceptance tests compose, with a microphone on one side and the console
    standing in for the danmaku feed on the other.
    """
    from bilisama.app import Assembly
    from bilisama.cli import DEFAULT_CONFIG
    from bilisama.clock import SystemClock
    from bilisama.config import derive, load
    from bilisama.config.schema import SideModelConfig
    from bilisama.director.floor import SpeakingFloor
    from bilisama.director.scheduler import Scheduler
    from bilisama.ingest.sources import QueueSource
    from bilisama.memory.distill import Distiller
    from bilisama.memory.store import MemoryStore
    from bilisama.obs.outcome import Outcome, Verdict
    from bilisama.persona.loader import PersonaStore, default_data_dir
    from bilisama.proactive import ProactiveTopicLoop
    from bilisama.realtime.providers.s2s import S2SLink
    from bilisama.side import OpenAICompatSideModel, SideModel

    provider = ProviderName(args.provider)
    config_path: Path = args.config or DEFAULT_CONFIG
    overrides = {"persona": {"id": args.persona}} if args.persona else None
    settings = load(config_path, overrides=overrides, strict=False)
    clock = SystemClock()
    thresholds = derive(settings.interaction.chattiness)

    # Memory persists across runs on purpose: streams_seen is the point.
    # Same data home the personas use, one directory up from them.
    room_dir = default_data_dir(settings.persona.id).parent.parent / "rooms" / "dev-talk"
    room_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(room_dir / "memory.db", clock)
    store.prune_events(retain_days=settings.memory.retain_event_days)
    store.begin_stream()

    persona = PersonaStore.from_config(settings.persona, config_dir=config_path.parent)
    variables = {"userName": "主播", "agentName": settings.persona.id}

    side: SideModel | None = None
    side_cfg = settings.speech.side
    if side_cfg.base_url:
        side = OpenAICompatSideModel(side_cfg, api_key=os.environ.get("OPENAI_API_KEY", ""))
    elif os.environ.get("base_url"):  # noqa: SIM112  (path.sh 里的原名)
        side = OpenAICompatSideModel(
            SideModelConfig.model_validate(
                {
                    "base_url": os.environ.get("base_url", ""),  # noqa: SIM112
                    "model": os.environ.get("model_name", ""),  # noqa: SIM112
                }
            ),
            api_key=os.environ.get("api_key", ""),  # noqa: SIM112
        )
    if side is None:
        print("提示：没配侧路模型（[speech.side] 或 source path.sh），主动话题和蒸馏这场不干活。")

    inner: link.SpeechLink
    if provider is ProviderName.S2S:
        inner = S2SLink(args.url)
    elif provider is ProviderName.DASHSCOPE:
        from bilisama.realtime.providers.hosted import HostedLink

        key = os.environ.get("ali_api_key", "")  # noqa: SIM112  (path.sh 里的原名)
        if not key:
            raise SystemExit("缺环境变量 ali_api_key。先 source path.sh 再跑。")
        inner = HostedLink(
            _dashscope_url(args.model),
            ProviderName.DASHSCOPE,
            headers={"Authorization": f"Bearer {key}"},
            turn=settings.speech.dashscope.turn,
        )
    else:
        raise SystemExit("--director 支持 s2s 和 dashscope；openai_ga 不是出货路径，暂时没接。")
    speech = _Fanout(inner)
    await speech.connect()
    speech.start()

    distiller = Distiller(
        side,
        store,
        persona,
        settings.persona.growth,
        clock,
        every_n_events=settings.memory.distill_every_n_events,
    )
    floor = SpeakingFloor(clock)

    def verdict_sink(verdict: Verdict) -> None:
        if verdict.outcome is not Outcome.SPOKEN:
            print(f"[调度] {verdict.source} → {verdict}")

    scheduler = Scheduler(
        speech,
        floor,
        clock,
        cooldown_s=float(thresholds.cooldown_s),
        verdict_sink=verdict_sink,
        spoken_sink=distiller.note_assistant_line,
    )
    proactive = ProactiveTopicLoop(
        side,
        store,
        floor,
        clock,
        submit=scheduler.submit if settings.interaction.speak.proactive else lambda _i: None,
        prompt=persona.proactive_prompt(config_path.parent / "prompts" / "proactive.md", variables),
        idle_threshold_s=float(thresholds.idle_threshold_s),
        wake_interval_s=float(settings.interaction.proactive.wake_interval_s),
        max_per_hour=settings.interaction.proactive.max_per_hour,
        max_tokens=thresholds.max_output_tokens,
    )

    async def push_context(text: str) -> None:
        await speech.set_context(text)
        print(f"[上下文] 已推送（{len(text)} 字）")
        if args.show_context:
            print("─" * 40 + f"\n{text}\n" + "─" * 40)

    assembly = Assembly(
        store=store,
        distiller=distiller,
        proactive=proactive,
        persona=persona,
        growth=settings.persona.growth,
        speak_enabled=lambda s: bool(getattr(settings.interaction.speak, s, False)),
        submit=scheduler.submit,
        push_context=push_context,
        clock=clock,
        max_tokens=thresholds.max_output_tokens,
        variables=variables,
    )

    console = QueueSource("console")
    seq = itertools.count(1)
    speaker = _Speaker(args.output_device)

    async def stdin_pump() -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return  # stdin closed; keep the rest running
            event = _parse_console_event(line, next(seq))
            if event is None:
                print("弹幕：直接打字；指定人：`阿强:内容`；/sc 名字 金额 内容；/gift 名字 金额")
                continue
            await console.push(event)

    async def drain_controls() -> None:
        while True:
            clear = await scheduler.controls.get()
            if speaker is not None:
                speaker.flush()
            print(f"[打断] playback.clear（{clear.reason}）")

    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)

    print(
        f"已连接 {provider.value}（{args.url.split('?')[0]}），director 模式：\n"
        f"  人设 {settings.persona.id}（persona list 可看全部）  "
        f"话痨度 {settings.interaction.chattiness.value}"
        f"（冷场 {thresholds.idle_threshold_s}s 起话题）\n"
        f"  生长层 relationship={settings.persona.growth.relationship.value} "
        f"voice={settings.persona.growth.voice.value}\n"
        "  说话即聊；终端打字＝模拟弹幕；Ctrl-C 下播（触发蒸馏后退出）。"
    )
    if not args.mute_while_speaking:
        print("提示：外放会让 AI 听到自己的声音。戴耳机，或加 --mute-while-speaking。")

    await assembly.refresh_context()
    tasks = [
        asyncio.create_task(scheduler.run(), name="director:scheduler"),
        asyncio.create_task(proactive.run(), name="director:proactive"),
        asyncio.create_task(assembly.run([console]), name="director:assembly"),
        asyncio.create_task(
            _pump_mic(speech, args.input_device, speaker, args.mute_while_speaking),
            name="director:mic",
        ),
        asyncio.create_task(_consume_events(speech.events(), speaker, None), name="director:play"),
        asyncio.create_task(drain_controls(), name="director:controls"),
        asyncio.create_task(stdin_pump(), name="director:stdin"),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("\n下播蒸馏中…")
        report = await distiller.end_of_stream()
        print(f"[蒸馏] ran={report.ran} reason={report.reason}")
        store.end_stream()
        rel, voc = persona.growth_entries("relationship"), persona.growth_entries("voice")
        if rel or voc:
            print(
                f"[生长层] 共同经历 {len(rel)} 条、口癖 {len(voc)} 句。"
                "翻看：bilisama persona review"
            )
        store.close()
        if side is not None:
            await side.aclose()
        await speech.aclose()
        print("再见。")
    return 0


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
    parser.add_argument(
        "--director",
        action="store_true",
        help="全装配模式：人设+记忆+蒸馏+主动话题+调度器全部上线，终端打字模拟弹幕",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="bilisama.toml 路径（director 用）"
    )
    parser.add_argument("--persona", default=None, help="临时换人设（director 用，不改配置文件）")
    parser.add_argument(
        "--show-context", action="store_true", help="每次上下文推送时把全文打出来（director 用）"
    )
    args = parser.parse_args(argv)
    if args.director:
        return asyncio.run(run_director(args))
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n再见。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
