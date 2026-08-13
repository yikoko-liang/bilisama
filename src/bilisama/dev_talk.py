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
import contextlib
import importlib.util
import itertools
import json
import math
import os
import signal
import sys
import threading
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
            for sink in list(self._sinks):
                if sink.full():
                    # A stalled or dead consumer must not grow memory forever
                    # (C10): live streams stay live, oldest frames go.
                    sink.get_nowait()
                sink.put_nowait(event)

    def events(self) -> AsyncIterator[link.LinkEvent]:
        queue: asyncio.Queue[link.LinkEvent] = asyncio.Queue(maxsize=256)
        self._sinks.append(queue)

        async def drain() -> AsyncIterator[link.LinkEvent]:
            try:
                while True:
                    yield await queue.get()
            finally:
                # A consumer that stops iterating unregisters its queue.
                if queue in self._sinks:
                    self._sinks.remove(queue)

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

    async def end_protection(self) -> None:
        await self._inner.end_protection()


def _console_viewer(name: str) -> Viewer:
    # A stable uid per name, so memory sees 测试观众 as the same person every time.
    return Viewer(uid=10000 + zlib.crc32(name.encode()) % 90000, name=name)


def _parse_amount(raw: str) -> float | None:
    """A finite positive yuan amount, or None.

    float() happily accepts "inf" and "nan", which then reach
    `int(value * 1000)` in the gift path as OverflowError/ValueError and kill
    the stdin pump task — one bad typed line, no more console input (C11).
    """
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


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
        value = _parse_amount(amount)
        if value is None:
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
        value = _parse_amount(parts[1])
        if value is None:
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
    from secrets import token_hex

    from bilisama import secrets
    from bilisama.app import Assembly
    from bilisama.cli import DEFAULT_CONFIG
    from bilisama.clock import SystemClock
    from bilisama.config import check, derive, load
    from bilisama.config.schema import SideModelConfig
    from bilisama.director.floor import SpeakingFloor
    from bilisama.director.intent import Intent
    from bilisama.director.scheduler import Scheduler
    from bilisama.ingest.sources import QueueSource
    from bilisama.memory.distill import Distiller
    from bilisama.memory.store import MemoryStore
    from bilisama.obs.health import HealthRegistry
    from bilisama.obs.loop_lag import LoopLagMonitor
    from bilisama.obs.outcome import Outcome, Verdict
    from bilisama.persona.loader import PersonaStore, default_data_dir, template_variables
    from bilisama.proactive import ProactiveTopicLoop
    from bilisama.realtime.providers import turn_type_problems
    from bilisama.realtime.providers.s2s import S2SLink
    from bilisama.side import OpenAICompatSideModel, SideModel
    from bilisama.ui.config_edit import ConfigEditError, apply_config_edit
    from bilisama.ui.events import ClientEvent, ServerEvent, link_frames
    from bilisama.ui.hub import UiHub, VoiceSignals
    from bilisama.ui.poke import PokeResponder
    from bilisama.ui.server import (
        UiServer,
        bind_ui_socket,
        create_ui_app,
        default_endpoint_path,
        write_endpoint_file,
    )

    provider = ProviderName(args.provider)
    config_path: Path = args.config or DEFAULT_CONFIG
    if not config_path.is_file():
        # load() treats a missing path as "all defaults", which silently ignores
        # a typo'd --config — the one case where defaults are a lie (D6).
        raise SystemExit(f"找不到配置文件：{config_path}")
    overrides: dict[str, Any] = {}
    if args.persona:
        overrides["persona"] = {"id": args.persona}
    if args.skin:
        # A run-scoped override, same layer as --persona: nothing touches the
        # tracked toml. "tofu" selects the built-in robot explicitly.
        if args.skin == "tofu":
            overrides["avatar"] = {"renderer": "tofu", "model_id": ""}
        else:
            overrides["avatar"] = {"renderer": "sprite", "model_id": args.skin}
    settings = load(config_path, overrides=overrides or None, strict=False)
    # strict=False keeps a half-configured dev box usable, but the problems
    # still get said out loud instead of silently shaping behaviour (D6).
    for problem in check(settings, config_dir=config_path.parent):
        tag = "错误" if problem.fatal else "提醒"
        fix = f"（{problem.fix}）" if problem.fix else ""
        print(f"[配置{tag}] {problem.field}：{problem.message}{fix}")
    # Without this, background warnings (proactive.refresh_failed and friends)
    # reach the console as bare event names with their error fields dropped.
    from bilisama.obs.logging import setup as logging_setup

    clock = SystemClock()
    # The hub exists before logging so its ring handler catches every line;
    # bind failure later just parks the hub unused (its staging is bounded).
    hub: UiHub | None = UiHub(clock) if not args.no_ui else None
    log_tee = (hub.log_handler,) if hub is not None else ()
    # log_viewer_content rides along on every (re)setup — before this it was
    # a config field nothing read, so the toml flag silently did nothing.
    logging_setup(
        level=settings.runtime.log_level,
        log_viewer_content=settings.runtime.log_viewer_content,
        extra_handlers=log_tee,
    )
    thresholds = derive(settings.interaction.chattiness)

    stop = asyncio.Event()

    def on_sigint() -> None:
        if stop.is_set():
            # Second press: the polite path is stuck somewhere before
            # stop.wait() (a hung connect, a wedged close). BaseExceptions
            # escape loop callbacks by design, so this aborts asyncio.run.
            raise KeyboardInterrupt
        stop.set()

    # Installed before the first await so a Ctrl-C during connect/setup exits
    # cleanly instead of unwinding with a traceback (C7).
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, on_sigint)

    # Memory persists across runs on purpose: streams_seen is the point.
    # Same data home the personas use, one directory up from them.
    room_dir = default_data_dir(settings.persona.id).parent.parent / "rooms" / "dev-talk"
    room_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(
        room_dir / "memory.db", clock, write_batch_ms=settings.memory.write_batch_ms
    )
    store.prune_events(retain_days=settings.memory.retain_event_days)
    store.begin_stream()

    persona = PersonaStore.from_config(settings.persona, config_dir=config_path.parent)
    variables = template_variables(settings.persona)

    # Side-model resolution, most reliable first: explicit config wins, then
    # the aliyun compatible-mode endpoint from path.sh (public network, no VPN
    # — probed live 2026-08-11 with qwen3.7-flash), then the intranet LLM
    # (which needs the whole EasyConnect + no_proxy incantation, runbook §起服务器).
    side: SideModel | None = None
    side_desc = ""
    side_cfg = settings.speech.side
    compat_url = os.environ.get("openai_compatible_url", "")  # noqa: SIM112  (path.sh 里的原名)
    if side_cfg.base_url:
        # The config's own credential reference wins; the env name is the
        # fallback for boxes that never filled it in (D8).
        side_key = secrets.resolve(side_cfg.api_key_ref) or os.environ.get("OPENAI_API_KEY", "")
        side = OpenAICompatSideModel(side_cfg, api_key=side_key)
        side_desc = f"{side_cfg.model} @ {side_cfg.base_url}（来自 [speech.side]）"
    elif compat_url:
        side_model = os.environ.get("side_model_name", "qwen3.7-flash")  # noqa: SIM112
        side = OpenAICompatSideModel(
            SideModelConfig.model_validate({"base_url": compat_url, "model": side_model}),
            api_key=os.environ.get("ali_api_key", ""),  # noqa: SIM112
        )
        side_desc = f"{side_model} @ 阿里 compatible-mode（path.sh，免 VPN）"
    elif os.environ.get("base_url"):  # noqa: SIM112
        side = OpenAICompatSideModel(
            SideModelConfig.model_validate(
                {
                    "base_url": os.environ.get("base_url", ""),  # noqa: SIM112
                    "model": os.environ.get("model_name", ""),  # noqa: SIM112
                }
            ),
            api_key=os.environ.get("api_key", ""),  # noqa: SIM112
        )
        side_desc = "path.sh 的内网端点（要 EasyConnect + no_proxy，连不上会刷 refresh_failed）"
    if side is None:
        print("提示：没配侧路模型（[speech.side] 或 source path.sh），主动话题和蒸馏这场不干活。")
    else:
        print(f"[侧路] {side_desc}")

    inner: link.SpeechLink
    if provider is ProviderName.S2S:
        # Audio replies, not the shipping default: this stands against the
        # zero-patch official pipeline whose own TTS does the speaking. A
        # text-pinned session would mute every reply until stage 4 exists.
        inner = S2SLink(args.url, text_replies=False)
    elif provider is ProviderName.DASHSCOPE:
        from bilisama.realtime.providers.hosted import HostedLink

        # The registry knows which turn types this endpoint really honours;
        # refusing here beats a session.update that gets silently ignored (D14).
        for problem in turn_type_problems(provider, settings.speech.dashscope.turn.type):
            raise SystemExit(f"{problem.message} {problem.fix}")
        env_key = os.environ.get("ali_api_key", "")  # noqa: SIM112  (path.sh 里的原名)
        key = secrets.resolve(settings.speech.dashscope.api_key_ref) or env_key
        if not key:
            raise SystemExit(
                "缺 DashScope 凭据：配 [speech.dashscope] api_key_ref，或先 source path.sh。"
            )
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

    from bilisama.director.output_guard import load_guard

    try:
        guard = load_guard(settings.safety, config_dir=config_path.parent)
    except FileNotFoundError as exc:
        raise SystemExit(f"{exc}。词表是上线硬门槛（计划 §7.6），配好再跑。") from exc
    print(f"[安全] 词表已装载，命中策略 {settings.safety.on_hit}")

    distiller = Distiller(
        side,
        store,
        persona,
        settings.persona.growth,
        clock,
        every_n_events=settings.memory.distill_every_n_events,
        guard=guard.text_blocked,
    )
    floor = SpeakingFloor(clock)
    # The quiet window must cover the WORST turn-grace branch plus margin
    # (plan section 3.3 rule 1); a fixed 1.1 s left a gap into the rule-5
    # pending window (A2/A3). The floor also holds while an implicit reply
    # is generating, so this timer only carries the no-reply case.
    if provider is ProviderName.S2S:
        turn = settings.speech.s2s.turn
        quiet_s = (turn.smart_turn_max_wait_ms + turn.smart_turn_incomplete_delay_ms) / 1000 + 0.3
    else:
        quiet_s = settings.speech.dashscope.turn.silence_duration_ms / 1000 + 0.3

    def verdict_sink(verdict: Verdict) -> None:
        if verdict.outcome is not Outcome.SPOKEN:
            print(f"[调度] {verdict.source} → {verdict}")
        if hub is not None:
            # The panel wants the COMPLETE per-intent record, SPOKEN included;
            # the console keeps printing exceptions only.
            hub.broadcast(
                ServerEvent.EVENT_FEED,
                {
                    "kind": "verdict",
                    "source": verdict.source,
                    "outcome": str(verdict.outcome),
                    "phase": str(verdict.phase),
                    "reason": str(verdict.reason) if verdict.reason else "",
                },
            )

    scheduler = Scheduler(
        speech,
        floor,
        clock,
        cooldown_s=float(thresholds.cooldown_s),
        quiet_after_speech_s=quiet_s,
        verdict_sink=verdict_sink,
        guard=guard,
        on_hit=settings.safety.on_hit,
        spoken_sink=distiller.note_assistant_line,
    )

    def proactive_submit(intent: Intent) -> None:
        # Read the switch at submit time, not at wiring time: panel.set flips
        # it mid-stream, and ui_meta already promises Reload.LIVE for it.
        if settings.interaction.speak.proactive:
            scheduler.submit(intent)

    proactive = ProactiveTopicLoop(
        side,
        store,
        floor,
        clock,
        submit=proactive_submit,
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

    from bilisama.ingest.bilibili.selector import DanmakuSelector, PresenceWelcomer

    selector = DanmakuSelector(
        clock,
        thresholds=lambda: thresholds,
        per_uid_cooldown_s=float(settings.interaction.danmaku.per_uid_cooldown_s),
    )
    presence = PresenceWelcomer(
        uniques=settings.interaction.burst_uniques,
        window_s=float(settings.interaction.burst_window_s),
        cooldown_s=float(settings.interaction.burst_cooldown_s),
    )
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
        protect_ms=settings.interaction.sc_protect_ms,
        variables=variables,
        clock_granularity_min=settings.memory.clock_granularity_min,
        selector=selector,
        presence=presence,
        gift_gold_high=settings.interaction.gift_gold_high,
        gift_gold_medium=settings.interaction.gift_gold_medium,
    )

    # Real danmaku, when a room is named (--room beats [room] room_id). The
    # credential chain: config ref first, then path.sh's BILI_SESSDATA. No
    # credential still connects — Bilibili then masks every uid to 0, which
    # kills regular-viewer memory, so say it out loud.
    bili_source = None
    room_id = args.room if args.room is not None else settings.room.room_id
    if room_id:
        from bilisama.ingest.bilibili import BilibiliEventSource

        # One truth: the config reference (shipped default env:BILI_SESSDATA).
        sessdata = secrets.resolve(settings.room.credential_ref) or ""
        bili_source = BilibiliEventSource(
            room_id, clock, sessdata=sessdata, on_sc_delete=scheduler.revoke
        )
        if sessdata:
            print(f"[弹幕] 连接房间 {room_id}（登录态）")
        else:
            print(
                f"[弹幕] 连接房间 {room_id}（匿名：观众全部打码，认不出常客——"
                "path.sh 加 export BILI_SESSDATA=... 后重跑）"
            )

    # The same probes stage 5's UI server will mount; until then the exit
    # snapshot is their one reader (D3).
    registry = HealthRegistry()
    registry.register("assembly", assembly.status)
    registry.register("proactive", proactive.status)
    registry.register("scheduler", scheduler.status)
    # The runtime half of the blocking-call defence (plan section 16.8 item
    # 25): a synchronous stall anywhere shows up as loop.lag with a number.
    lag_monitor = LoopLagMonitor()
    registry.register("loop", lag_monitor.status)
    registry.register("selector", selector.status)
    if bili_source is not None:
        registry.register("bilibili", bili_source.status)

    console = QueueSource("console")
    seq = itertools.count(1)
    speaker = _Speaker(args.output_device)

    _USAGE = "弹幕：直接打字；指定人：`阿强:内容`；/sc 名字 金额 内容；/gift 名字 金额"
    _FEED_KIND = {EventKind.SUPER_CHAT: "sc", EventKind.GIFT: "gift"}

    async def handle_line(line: str) -> None:
        if not line.strip():
            return  # a bare Enter injects nothing and lectures nobody
        event = _parse_console_event(line, next(seq))
        if event is None:
            print(_USAGE)
            if hub is not None:
                # The panel's inject box needs the same feedback the console gets.
                hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": _USAGE})
            return
        label = {EventKind.SUPER_CHAT: "SC", EventKind.GIFT: "礼物"}.get(event.kind, "弹幕")
        money = f"（¥{event.value_cny:.0f}）" if event.value_cny else ""
        body = f"：{event.text}" if event.text else ""
        print(f"[已注入 {label}] {event.viewer.name}{body}{money}")
        if hub is not None:
            hub.broadcast(
                ServerEvent.EVENT_FEED,
                {
                    "kind": _FEED_KIND.get(event.kind, "danmaku"),
                    "name": event.viewer.name,
                    "text": event.text,
                    "value_cny": event.value_cny,
                    "injected": True,
                },
            )
        await console.push(event)

    # ------------------------------------------------------------ UI server

    ui_server: UiServer | None = None
    ui_url = ""
    endpoint_file: Path | None = None
    if hub is not None:
        poke = PokeResponder(
            clock, submit=scheduler.submit, max_tokens=thresholds.max_output_tokens
        )

        def panel_state() -> dict[str, Any]:
            speak = settings.interaction.speak
            return {
                "panicked": bool(scheduler.status().get("panicked")),
                "speak": {name: bool(getattr(speak, name)) for name in type(speak).model_fields},
            }

        async def on_poke(_data: dict[str, Any]) -> None:
            poke.poke()  # False = cooldown; the page animates either way

        async def on_panel_set(data: dict[str, Any]) -> None:
            if "panic_mute" in data:
                if bool(data["panic_mute"]):
                    scheduler.panic_mute()
                    print("[面板] 紧急闭麦")
                else:
                    scheduler.release_panic()
                    print("[面板] 恢复说话")
            speak_patch = data.get("speak")
            if isinstance(speak_patch, dict):
                speak = settings.interaction.speak
                for name, value in speak_patch.items():
                    # model_fields is the whitelist; unknown keys only warn.
                    if name in type(speak).model_fields:
                        setattr(speak, name, bool(value))
                    else:
                        print(f"[面板] 未知开关 {name}，忽略")
            edit = data.get("config")
            if isinstance(edit, dict):
                # The per-field write channel (stage 5's, lit up early). The
                # gates live in apply_config_edit; here is only the ack, the
                # error line, and the one consumer that needs a re-poke.
                try:
                    meta, applied = apply_config_edit(settings, edit.get("path"), edit.get("value"))
                except ConfigEditError as exc:
                    print(f"[面板] {exc}")
                    if hub is not None:
                        hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": str(exc)})
                else:
                    shown = "开" if applied is True else "关" if applied is False else str(applied)
                    line = f"配置已改：{meta.label} → {shown}（本场生效，重启还原）"
                    print(f"[面板] {line}")
                    if hub is not None:
                        hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": line})
                    if edit.get("path") in ("runtime.log_level", "runtime.log_viewer_content"):
                        # Logging snapshots its config at setup; re-run it so
                        # the edit is live. Stream choice mirrors the prompt
                        # window (use_prompt is bound late, at call time).
                        logging_setup(
                            level=settings.runtime.log_level,
                            log_viewer_content=settings.runtime.log_viewer_content,
                            stream=sys.stdout if use_prompt else None,
                            extra_handlers=log_tee,
                        )
            if hub is not None:
                hub.broadcast(ServerEvent.PANEL_STATE, panel_state())

        async def on_console_line(data: dict[str, Any]) -> None:
            text = data.get("text")
            if isinstance(text, str):
                await handle_line(text)

        def hello() -> dict[str, Any]:
            return {
                "protocol": 1,
                "persona": {
                    "id": settings.persona.id,
                    "name": settings.persona.display_name or settings.persona.id,
                },
                "provider": provider.value,
                "room_connected": bool(room_id),
                "avatar": {
                    "renderer": settings.avatar.renderer,
                    "model_id": settings.avatar.model_id,
                },
                "panel": panel_state(),
            }

        try:
            ui_sock = bind_ui_socket(settings.runtime.ui_port)
        except OSError as exc:
            # The UI is a passenger; the voice loop must survive a taken port.
            print(
                f"[界面] 端口 {settings.runtime.ui_port} 绑不上（{exc}），本场没有界面；"
                "改 [runtime] ui_port 或空出端口。"
            )
            hub = None
        else:
            ui_port = ui_sock.getsockname()[1]
            ui_origin = f"http://127.0.0.1:{ui_port}"
            ui_token = token_hex(24)
            ui_url = f"{ui_origin}/{ui_token}/"
            app = create_ui_app(
                hub=hub,
                registry=registry,
                settings=settings,
                token=ui_token,
                origin=ui_origin,
                handlers={
                    ClientEvent.PET_POKE: on_poke,
                    ClientEvent.PANEL_SET: on_panel_set,
                    ClientEvent.CONSOLE_LINE: on_console_line,
                },
                hello=hello,
                # <data home>/bilisama/skins — user-imported packs, shadowing
                # the packaged ones. Endpoint file and skins share the roof.
                user_skins_root=default_endpoint_path().parent.parent / "skins",
            )
            ui_server = UiServer(app, ui_sock)
            ui_server.start()
            endpoint_file = default_endpoint_path()
            try:
                write_endpoint_file(endpoint_file, url=ui_url, pid=os.getpid())
            except OSError as exc:
                print(f"[界面] 端点文件写不了（{exc}）；壳这场要用 BILISAMA_UI_URL={ui_url}")
                endpoint_file = None
            hub.broadcast(ServerEvent.PANEL_STATE, panel_state())  # seed the sticky state
            if args.open:
                import webbrowser

                webbrowser.open(ui_url)

    # --pet: best-effort convenience spawn of the desktop shell. This is NOT
    # supervision — stage 7 inverts the relationship (Electron launches P2).
    # The shell finds the endpoint file on its own; all this saves is a second
    # terminal. Missing electron never touches the voice loop.
    pet_proc: asyncio.subprocess.Process | None = None
    if args.pet and ui_server is not None:
        pet_dir = Path(__file__).resolve().parents[2] / "desktop" / "preview"
        electron = pet_dir / "node_modules" / ".bin" / "electron"
        if electron.is_file():
            try:
                pet_proc = await asyncio.create_subprocess_exec(
                    str(electron),
                    ".",
                    cwd=pet_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                # The shell is a passenger of a passenger; failing to spawn it
                # must not touch the voice loop.
                print(f"[桌宠] 壳拉不起来（{exc}）；手动 npm start 也行")
            else:
                print("[桌宠] 壳已拉起（关掉 dev-talk 会一并带走）")
        else:
            print(f"[桌宠] 壳还没装：cd {pet_dir} && npm install，之后 --pet 才有东西可拉")
    elif args.pet and not args.no_ui:
        # The user wanted a UI; the port bind above is what failed.
        print("[桌宠] 界面服务器没起来（端口问题，见上面），这场壳没有可显示的东西")
    elif args.pet:
        print("[桌宠] --pet 需要界面服务器；--no-ui 下没有可显示的东西")

    # A bottom-pinned input line when prompt_toolkit is installed: everything
    # the session prints (replies, verdicts, log lines) lands ABOVE the line
    # being typed instead of tearing through it. Optional exactly like
    # sounddevice; --plain-console or piped stdin falls back to the raw reader.
    use_prompt = (
        not args.plain_console
        and sys.stdin.isatty()
        and importlib.util.find_spec("prompt_toolkit") is not None
    )
    if not args.plain_console and sys.stdin.isatty() and not use_prompt:
        print("提示：装上 prompt_toolkit 后，打弹幕不再被输出打乱（pip install prompt_toolkit）。")

    if use_prompt:

        async def stdin_pump() -> None:
            from prompt_toolkit import PromptSession

            session: PromptSession[str] = PromptSession("弹幕> ")
            while True:
                try:
                    line = await session.prompt_async()
                except KeyboardInterrupt:
                    # Raw mode turns Ctrl-C into a key press on the prompt, so
                    # the loop-level SIGINT handler never sees it — route it to
                    # the same graceful stop.
                    stop.set()
                    return
                except EOFError:
                    return  # Ctrl-D: console closed; the rest keeps running
                await handle_line(line)

    else:
        # Stdin on a daemon thread, not run_in_executor: asyncio.run joins the
        # default executor on shutdown, and a worker parked in readline() holds
        # that join until one more Enter — Ctrl-C would hang the exit (C7). A
        # daemon thread just dies with the process.
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_stdin() -> None:
            while True:
                raw_line = sys.stdin.readline()
                try:
                    loop.call_soon_threadsafe(lines.put_nowait, raw_line or None)
                except RuntimeError:
                    return  # loop already closed; we are exiting
                if not raw_line:
                    return

        threading.Thread(target=read_stdin, name="dev-talk:stdin", daemon=True).start()

        async def stdin_pump() -> None:
            while True:
                line = await lines.get()
                if line is None:
                    return  # stdin closed; keep the rest running
                await handle_line(line)

    async def drain_controls() -> None:
        while True:
            clear = await scheduler.controls.get()
            if speaker is not None:
                speaker.flush()
            print(f"[打断] playback.clear（{clear.reason}）")
            if hub is not None:
                # The bubble shatters on this; the queue stays single-consumer
                # here and the hub only gets a copy.
                hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": clear.reason})

    console_patch = contextlib.ExitStack()
    if use_prompt:
        from prompt_toolkit.patch_stdout import patch_stdout

        # While the prompt is live, print() and the log stream both write
        # through a proxy that repaints the input line beneath them. Logging
        # re-targets onto the patched stdout for the window; the shutdown
        # chain restores it.
        console_patch.enter_context(patch_stdout(raw=True))
        logging_setup(
            level=settings.runtime.log_level,
            log_viewer_content=settings.runtime.log_viewer_content,
            stream=sys.stdout,
            extra_handlers=log_tee,
        )

    print(
        f"已连接 {provider.value}（{args.url.split('?')[0]}），director 模式：\n"
        f"  人设 {settings.persona.id}（persona list 可看全部）  "
        f"话痨度 {settings.interaction.chattiness.value}"
        f"（冷场 {thresholds.idle_threshold_s}s 起话题）\n"
        f"  生长层 relationship={settings.persona.growth.relationship.value} "
        f"voice={settings.persona.growth.voice.value}\n"
        "  说话即聊；终端打字＝模拟弹幕；Ctrl-C 下播（触发蒸馏后退出）。"
    )
    if ui_url:
        print(f"  界面 {ui_url}\n  （浏览器打开看桌宠和面板；desktop/preview 的壳会自己找到它）")
    if not args.mute_while_speaking:
        print("提示：外放会让 AI 听到自己的声音。戴耳机，或加 --mute-while-speaking。")

    # Everything below runs under the finally: the UI server, the endpoint
    # file and the pet shell already exist, and refresh_context is a real
    # network send — a failure past this point must still tear them down.
    tasks: list[asyncio.Task[None]] = []
    try:
        await assembly.refresh_context()
        tasks = [
            asyncio.create_task(scheduler.run(), name="director:scheduler"),
            asyncio.create_task(proactive.run(), name="director:proactive"),
            asyncio.create_task(
                assembly.run([console] + ([bili_source] if bili_source is not None else [])),
                name="director:assembly",
            ),
            asyncio.create_task(
                _pump_mic(speech, args.input_device, speaker, args.mute_while_speaking),
                name="director:mic",
            ),
            asyncio.create_task(
                _consume_events(speech.events(), speaker, None), name="director:play"
            ),
            asyncio.create_task(drain_controls(), name="director:controls"),
            asyncio.create_task(stdin_pump(), name="director:stdin"),
            asyncio.create_task(lag_monitor.run(), name="director:loop-lag"),
        ]
        if hub is not None:
            live_hub = hub

            def read_signals() -> VoiceSignals:
                status = scheduler.status()
                return VoiceSignals(
                    streamer_speaking=floor.streamer_speaking,
                    dispatching=bool(status.get("dispatching")),
                    active=status.get("active_source") is not None,
                    implicit=floor.implicit_active,
                    audio_busy=speaker.busy,
                )

            async def ui_feed() -> None:
                # The fanout's third view (scheduler and director:play hold the
                # other two); link_frames drops PCM before it can reach a browser.
                async for ev in speech.events():
                    for name, data in link_frames(ev):
                        live_hub.broadcast(name, data)

            tasks += [
                asyncio.create_task(ui_feed(), name="director:ui-feed"),
                asyncio.create_task(live_hub.run(read_signals), name="director:ui-state"),
            ]
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # The prompt died with its task; unpatch stdout before the shutdown
        # chain prints, and point logging back at stderr.
        console_patch.close()
        if use_prompt:
            logging_setup(
                level=settings.runtime.log_level,
                log_viewer_content=settings.runtime.log_viewer_content,
                extra_handlers=log_tee,
            )
        # The UI goes first: aclose() chases the browsers off their sockets so
        # uvicorn's graceful stop is not stuck waiting on an open WebSocket.
        if ui_server is not None:
            try:
                if hub is not None:
                    await hub.aclose()
                await ui_server.stop()
            except Exception as exc:
                print(f"[收尾] 界面服务器没关上：{exc}", file=sys.stderr)
        if endpoint_file is not None:
            with contextlib.suppress(OSError):
                endpoint_file.unlink(missing_ok=True)
        if pet_proc is not None and pet_proc.returncode is None:
            # A viewer, not a dependent: terminate politely, then the axe — a
            # shell that shrugs off SIGTERM must not outlive its dev-talk.
            with contextlib.suppress(ProcessLookupError):
                pet_proc.terminate()
            try:
                await asyncio.wait_for(pet_proc.wait(), timeout=3.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    pet_proc.kill()
        # Every step below gets its own try: this is a chain of unrelated
        # resources, and one refusing to close must not leak the rest — a
        # failed distillation would otherwise leave the WS and the DB open
        # (C12). Broad excepts are the point here; each one reports.
        snapshot = registry.snapshot()
        print(f"\n[状态] {json.dumps(snapshot['components'], ensure_ascii=False, default=str)}")
        print("下播蒸馏中…")
        try:
            report = await distiller.end_of_stream()
            print(f"[蒸馏] ran={report.ran} reason={report.reason}")
        except Exception as exc:
            print(f"[蒸馏] 失败：{exc}", file=sys.stderr)
        try:
            store.end_stream()
            rel, voc = persona.growth_entries("relationship"), persona.growth_entries("voice")
            if rel or voc:
                print(
                    f"[生长层] 共同经历 {len(rel)} 条、口癖 {len(voc)} 句。"
                    "翻看：bilisama persona review"
                )
        except Exception as exc:
            print(f"[收尾] 记忆收口失败：{exc}", file=sys.stderr)
        try:
            store.close()
        except Exception as exc:
            print(f"[收尾] 记忆库没关上：{exc}", file=sys.stderr)
        if side is not None:
            try:
                await side.aclose()
            except Exception as exc:
                print(f"[收尾] 侧路连接没关上：{exc}", file=sys.stderr)
        try:
            await speech.aclose()
        except Exception as exc:
            print(f"[收尾] 语音连接没关上：{exc}", file=sys.stderr)
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
    parser.add_argument(
        "--room",
        type=int,
        default=None,
        help="连真实直播间（房间号，短号可）；不给则读 [room] room_id",
    )
    parser.add_argument(
        "--plain-console",
        action="store_true",
        help="不用底部输入行，退回逐行读 stdin（终端行为怪异时的逃生口）",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="不起桌宠界面服务器（director 模式默认起，浏览器/壳都从它看）",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="界面起来后自动在浏览器打开（director 用）",
    )
    parser.add_argument(
        "--pet",
        action="store_true",
        help="顺手拉起桌面桌宠壳（要先 cd desktop/preview && npm install 装过一次）",
    )
    parser.add_argument(
        "--skin",
        default=None,
        help="本次运行的形象：皮肤包目录名（如 kirby），或 tofu 用内置豆腐机器人；不改配置文件",
    )
    args = parser.parse_args(argv)
    if args.director:
        try:
            return asyncio.run(run_director(args))
        except KeyboardInterrupt:
            # Only the second Ctrl-C lands here (see on_sigint); the first one
            # goes through the graceful path.
            print("\n强退。")
            return 130
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n再见。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
