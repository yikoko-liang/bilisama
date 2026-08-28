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
import dataclasses
import importlib.util
import itertools
import json
import logging
import math
import os
import signal
import sys
import threading
import traceback
import wave
import zlib
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl

import websockets

from bilisama.config.derive import DerivedThresholds, effective_thresholds
from bilisama.config.enums import Chattiness, ProviderName
from bilisama.event_pacing import EventPacingSnapshot
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer
from bilisama.obs.health import LinkHealth
from bilisama.obs.logging import bind, get_logger
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient, SessionRefused
from bilisama.realtime.providers import (
    ProviderProfile,
    codec_for,
    profile_for,
    resolve_endpoint,
    with_model,
)
from bilisama.realtime.providers.factory import LinkRequest, build_link, hosted_key


class _AudioIn(Protocol):
    """What the pumps need: anything with push_audio — the raw client in wire
    mode, the fanned-out adapter in director mode."""

    async def push_audio(self, pcm: bytes) -> None: ...


class _Connects(Protocol):
    """What _connect_or_exit needs: the raw client in wire mode, the
    fanned-out adapter in director mode."""

    async def connect(self) -> None: ...


log = get_logger(__name__)

_INPUT_RATE = 16000  # what this file's mic pump and WAV reader produce
_OUTPUT_RATE = 24000  # and answer at 24 kHz (plan section 3.1 table)
_FRAME_MS = 32

# Where a reply may be broken into a printable line while it is still arriving.
# s2s hands us one fragment per LLM chunk (roughly a sentence); DashScope
# streams tokens, so the cap keeps a punctuation-free run from waiting forever.
# How far playback may fall behind before the speaker skips forward. Ten
# seconds is roughly two replies: enough that a burst plays out intact, far
# short of the minutes of drift measured before the floor gate was fed.
_MAX_BACKLOG_BYTES = 10 * 2 * _OUTPUT_RATE
# DashScope audio deltas arrive over the network in bursts. Starting PortAudio
# on the first short packet makes the callback consume it before the next
# packet arrives, then fill the gap with silence — the audible stop-start bug.
# Hold a small jitter cushion first; 120 ms stays below a conversational beat
# while covering several ordinary websocket scheduling gaps.
_PLAYBACK_PREROLL_MS = 120
_PLAYBACK_PREROLL_BYTES = _OUTPUT_RATE * 2 * _PLAYBACK_PREROLL_MS // 1000

# How long the uplink keeps forwarding into a dead socket before giving up.
# Has to outlast the client's whole reconnect budget — six tries with backoff
# capped at 30s (client.py:375) — or we would quit on an outage it was about
# to recover from. Blocks arrive one per _FRAME_MS, so counting them counts
# time closely enough for a patience threshold.
_UPLINK_PATIENCE_S = 300.0

_SENTENCE_END = "。！？…；\n.!?"
_LINE_SOFT_CAP = 32

# Live config paths whose consumer snapshots its value at setup and therefore
# needs a re-poke after an edit. Out in the open on purpose: a field marked
# Reload.LIVE must either be read at call time or be listed here, and a hook
# hidden inside a closure is how "the panel says 配置已改 and nothing changes"
# gets reintroduced.
_RELOG_ON_EDIT = frozenset({"runtime.log_level", "runtime.log_viewer_content"})

# Panel edits that are SESSION state, not durable preferences: applied live,
# never written to disk, and forced back to their session defaults on every
# director start for the AUDIO pair only (_director_session_overrides): the
# room and the streamer name are legitimate TOML configuration, so those two
# are session-only in the panel yet still start from the file.
_SESSION_ONLY_PANEL_PATHS = frozenset(
    {
        "audio.input_enabled",
        "audio.output_enabled",
        "room.room_id",
        "persona.streamer_name",
    }
)


def _director_session_overrides() -> dict[str, Any]:
    """The session-state fields every director run starts from.

    Only the audio pair: a stream must always begin able to hear and speak,
    whatever a hand-edited TOML claims. room_id and streamer_name are real
    configuration and stay file-driven (panel edits to them are session-only
    instead — see _SESSION_ONLY_PANEL_PATHS).
    """
    return {
        "audio": {"input_enabled": True, "output_enabled": True},
    }


# Where an "as live" console injection claims to be from when no real room is
# connected. Any positive id engages the crowd funnel; this one is obviously
# fake in every log line.
_MANUAL_MOCK_ROOM_ID = 1


def _as_live_mock_event(event: LiveEvent, room_id: int) -> LiveEvent:
    """Mark a panel injection so Assembly applies the real-room funnel."""
    return dataclasses.replace(
        event,
        room_id=room_id if room_id > 0 else _MANUAL_MOCK_ROOM_ID,
        raw={**(event.raw or {}), "manual_mock": True},
    )


def _watch(task: asyncio.Task[None]) -> asyncio.Task[None]:
    """Make a background task's death audible.

    run_director starts eleven of these and then waits on a stop event, so
    nothing ever retrieves their results. A task that raises therefore just
    stops existing: the scheduler quits dispatching, or the mic stops
    forwarding, and the console says nothing at all. CPython only mentions it
    at GC time, as "Task exception was never retrieved", long after the
    session it broke.

    Cancellation is not a fault — shutdown cancels all eleven on purpose.
    """

    def done(finished: asyncio.Task[None]) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            # NOT log.exception here: it reads sys.exc_info(), and a done
            # callback runs with no active exception — so the formatter's exc
            # field (_JsonFormatter.format in obs/logging.py) came out as the
            # string "NoneType: None", and the one line about a task's death
            # said nothing about the death. The traceback comes off the
            # exception object instead, which this callback definitely has.
            log.error(
                "dev_talk.task_died",
                task=finished.get_name(),
                error_type=type(exc).__name__,
                error_text=str(exc),
                traceback="".join(traceback.format_exception(exc)),
            )
            # And once on the terminal, because the log is not a channel to
            # the person in front of the stream. Ten of the eleven tasks have
            # no other way to reach them at all: a scheduler that stopped
            # dispatching, or an assembly that stopped assembling, looks
            # exactly like a quiet chat.
            print(
                f"[后台] {finished.get_name()} 挂了（{type(exc).__name__}: {exc}）。"
                "这部分这场不会再工作了，重启 dev-talk 才能恢复。",
                file=sys.stderr,
            )

    task.add_done_callback(done)
    return task


def _endpoint_line(url: str) -> str:
    """Where we dial and which model, for the start banner.

    The query never goes on screen — an address can carry a credential — and
    on a hosted provider the model lives in exactly that query, so it is named
    on its own instead. Read out of the finished address rather than off
    Endpoint.model: those two can disagree (an address may name a model no
    layer of config mentions), and a banner naming the loser is what let a
    swallowed --model go unnoticed.

    Args:
        url: The address as it will be dialed, model already joined on.
    """
    where, _, query = url.partition("?")
    model = dict(parse_qsl(query, keep_blank_values=True)).get("model", "")
    return f"{where}，模型 {model}" if model else where


def _session_frame(provider: ProviderName, voice: str = "") -> dict[str, Any] | None:
    """The audio session setup, in the dialect the provider actually speaks.

    s2s gets nothing: its VAD runs unconditionally, and its session.update
    REQUIRES session.type="realtime" — a bare session draws "Unknown or
    invalid event" (probed live; plan section 3.1's table says the opposite
    and is wrong for v0.2.12-40). The adapter's set_context goes through
    Codec.session_patch, which writes the field, so only hand-rolled frames
    can trip on this.
    """
    if provider is ProviderName.DASHSCOPE:
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {"type": "server_vad"},
        }
        if voice:
            session["voice"] = voice
        return {"type": "session.update", "session": session}
    return None


def _connect_advice(exc: BaseException, provider: ProviderName, url: str) -> str:
    """One line of Chinese with a fix action (CLAUDE.md's error-wording rule).

    The two connect failures a dev box actually hits each get their recipe;
    anything else keeps the endpoint and the underlying reason.
    """
    where = url.split("?")[0]
    if isinstance(exc, SessionRefused) and exc.code == "session_limit_reached":
        return "s2s 只有一个会话槽，被别的客户端占着：关掉其他 dev-talk，或重启 serve 终端。"
    if isinstance(exc, ConnectionRefusedError) and provider is ProviderName.S2S:
        return f"{where} 上没有 s2s 服务：先按 runbook 起 serve。"
    # str(exc) is empty for an exception instance built with no arguments, and
    # asyncio raises exactly one of those on a path this hits: a TLS handshake
    # that gets EOF instead of a record ends as a bare ConnectionResetError()
    # (asyncio/sslproto.py:467,571) — which is what wss:// against a plaintext
    # port produces. "连不上 X：" with nothing after the colon told the streamer
    # strictly less than the class name would have.
    reason = str(exc) or type(exc).__name__
    return (
        f"连不上 {where}：{reason}。"
        "核对 bilisama.toml 里的这行地址：协议（ws/wss）、端口、路径要和对面服务对得上。"
    )


async def _connect_or_exit(target: _Connects, provider: ProviderName, url: str) -> None:
    """Connect, or exit through the file's established fatal path: SystemExit
    prints its message without a traceback and exits non-zero."""
    try:
        await target.connect()
    except (OSError, websockets.WebSocketException) as exc:
        raise SystemExit(_connect_advice(exc, provider, url)) from exc


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


def _sane_terminal(saved: Any) -> None:
    """Put the tty back the way we found it. Idempotent, never raises.

    prompt_toolkit runs stdin in raw mode, where Ctrl-C is a KEY, not a signal.
    If the prompt goes away without restoring (cancelled mid-read, or torn down
    while the shutdown chain runs), every later Ctrl-C is just an echoed ^C and
    the promised force-quit can never fire — the terminal, not the handler, was
    what swallowed it.
    """
    if saved is None or not sys.stdin.isatty():
        return
    # Windows has no termios, and no cooked mode to put back. Testing the
    # platform rather than suppressing an ImportError is what lets
    # `mypy --platform win32` read this branch at all.
    if sys.platform != "win32":
        with contextlib.suppress(Exception):
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


def _close_audio_stream(stream: Any) -> None:
    """Discard-then-release a PortAudio stream. BLOCKING — run off-loop.

    close() on its own drains pending buffers, and the device release behind
    it (worst on Bluetooth) can take whole seconds. Done on the event loop it
    freezes everything — including the SIGINT handler, which is exactly the
    "Ctrl-C again does nothing" exit hang. abort() first so close() has no
    drain left to wait for; both wrapped because a half-dead stream raising
    must not block the shutdown chain.
    """
    with contextlib.suppress(Exception):
        stream.abort()
    with contextlib.suppress(Exception):
        stream.close()


async def _uplink(
    client: _AudioIn,
    queue: asyncio.Queue[bytes],
    speaker: _Speaker | None,
    mute: bool,
    silence: bytes,
) -> None:
    """Forward mic blocks forever, including across a link outage.

    Split out of _pump_mic so it can be tested without a sound card, but the
    reason it exists is the try below. push_audio raises while the socket is
    gone (client.py:270), nobody awaits this loop, and an exception in it ends
    the uplink for the rest of the session in complete silence. That turned
    into the worst kind of failure the day the client learned to reconnect:
    the socket returns, the persona replays, the panel goes green, and the
    streamer's voice never arrives again. A send that fails while the link is
    down costs one block, not the session.

    Dropping those blocks is right, not a compromise — there is no socket to
    take them, and the reopened session starts its audio clock from scratch,
    so rule 7's "never stop the stream" has nothing to protect here.

    Patience is bounded, though. A link that never returns must still end the
    session: wire mode reports the failure and exits (its asyncio.wait watches
    this task), and swallowing forever would replace that with a process that
    looks alive and hears nothing. So after _UPLINK_PATIENCE_S of unbroken
    failure the error goes back up.
    """
    lost = 0
    limit = int(_UPLINK_PATIENCE_S * 1000 / _FRAME_MS)
    while True:
        block = await queue.get()
        if mute and speaker is not None and speaker.busy:
            # Echo shield for open speakers: the mic goes silent while the
            # reply plays. Costs barge-in during playback — headphones keep
            # it. Silence rather than nothing: stopping the append stream
            # freezes the provider's audio clock (plan section 3.3 rule 7).
            block = silence
        try:
            await client.push_audio(block)
        except (ConnectionError, websockets.WebSocketException):
            lost += 1
            if lost > limit:
                raise
            continue
        if lost:
            log.warning("dev_talk.uplink_resumed", lost_blocks=lost)
            lost = 0


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
    try:
        # start() inside the try: it can fail on its own (device pulled or
        # claimed between open and start), and the stream is already OPEN by
        # then — left unreleased it becomes the blocking atexit stall this
        # module fixes everywhere else.
        stream.start()
        await _uplink(client, queue, speaker, mute, silence)
    finally:
        # NOT `with stream:` — its __exit__ closes on the loop, which is the
        # freeze described on _close_audio_stream. The release runs on a worker
        # thread; asyncio joins that pool during shutdown, so it always
        # completes (it does NOT make a second Ctrl-C instant — that path exits
        # the process outright, see on_sigint).
        await asyncio.to_thread(_close_audio_stream, stream)


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
        self._dropped_s = 0.0
        self._device = device
        self._lock = threading.Lock()
        self._stream: Any = None
        self._sounddevice: Any = None
        # Jitter-cushion state (see _PLAYBACK_PREROLL_MS): primed means the
        # callback may drain; a sub-preroll remainder waits for finish_reply.
        self._primed = False
        self._reply_finished = False
        self._rebuffer_count = 0
        self._underrun_blocks = 0
        self._callback_status_count = 0
        self._enabled = True
        self._suspended = False
        self._opened_device: int | None = None
        self._last_output_error = ""
        self._open()

    def _feed(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio callback: drain jitter-buffered PCM without touching asyncio."""
        need = len(outdata)
        with self._lock:
            if status:
                self._callback_status_count += 1
            was_primed = self._primed
            if not self._primed and (
                len(self._buffer) >= _PLAYBACK_PREROLL_BYTES
                or (self._reply_finished and bool(self._buffer))
            ):
                self._primed = True
            if not self._primed:
                chunk = b""
            elif len(self._buffer) >= need:
                chunk = bytes(self._buffer[:need])
                del self._buffer[:need]
            elif self._reply_finished:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                self._primed = False
                self._reply_finished = False
            else:
                # Preserve the short packet and wait for a fresh cushion. The
                # old path played this fragment and padded the rest of the same
                # word with zeroes, producing the reported chopped speech.
                chunk = b""
                self._primed = False
                if was_primed:
                    self._rebuffer_count += 1
                    self._underrun_blocks += 1
        outdata[: len(chunk)] = chunk
        if len(chunk) < need:
            outdata[len(chunk) :] = b"\x00" * (need - len(chunk))

    def _start_stream(self, device: int | None) -> Any:
        stream = self._sounddevice.RawOutputStream(
            samplerate=_OUTPUT_RATE,
            channels=1,
            dtype="int16",
            device=device,
            callback=self._feed,
        )
        try:
            stream.start()
        except Exception:
            _close_audio_stream(stream)
            raise
        return stream

    def _default_output_device(self) -> int | None:
        if self._sounddevice is None:
            return None
        pair = self._sounddevice.default.device
        try:
            value = int(pair[1])
        except (IndexError, TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def _open(self) -> None:
        """Take the output device. Leaves _stream None if it cannot be had."""
        device = self._device
        try:
            import sounddevice
        except ImportError:
            self._stream = None
            return
        self._sounddevice = sounddevice
        try:
            stream = self._start_stream(device)
        except Exception as exc:
            # An opened-but-unstartable device (Bluetooth pulled mid-setup)
            # would otherwise leave the stream unreachable — the constructor
            # raises, no caller ever holds the object, and close() can never
            # run. _start_stream released it; carry on mute: the voice loop
            # is worth more than the speaker.
            self._stream = None
            print(f"[音频] 扬声器起不来（{exc}），这场没有声音输出。", file=sys.stderr)
            # Once per open, never per block — this is the device handshake,
            # not the playback path. It answers the report that arrives as
            # 「她不说话」 and is really 「没有输出设备」.
            log.warning(
                "dev_talk.speaker_unavailable",
                device=self._device,
                error_type=type(exc).__name__,
                error_text=str(exc),
            )
            return
        self._stream = stream
        self._opened_device = device if device is not None else self._default_output_device()

    def suspend(self) -> None:
        """Give the output device up while a page holds it. BLOCKING.

        Not close(): that one is final and idempotent for the shutdown chain.
        This keeps the device remembered so resume() can take it back, and
        drops whatever was queued — those samples belong to the moment that
        just ended, and playing them on the way back would be the backlog all
        over again.
        """
        self.flush()
        self._suspended = True
        stream, self._stream = self._stream, None
        if stream is not None:
            _close_audio_stream(stream)

    def resume(self) -> None:
        """Take the device back after a page let go. BLOCKING."""
        self._suspended = False
        if self._stream is None:
            self._open()

    def refresh_default_device(self) -> str | None:
        """Follow a macOS default-output change without restarting BiliSama.

        Explicit ``--output-device`` selections stay pinned, and a suspended
        speaker stays suspended — while a page holds the devices, reopening
        here would steal them back mid-session.
        """
        if self._device is not None or self._suspended or self._sounddevice is None:
            return None
        target = self._default_output_device()
        if target is None:
            return None
        old = self._stream
        try:
            active = bool(old is not None and old.active)
        except Exception:
            active = False
        if target == self._opened_device and active:
            return None
        # Stop the old callback before starting the new one. Both callbacks
        # share the same PCM ring; overlapping them, even briefly, lets two
        # devices race to consume different halves of one sentence.
        if old is not None:
            _close_audio_stream(old)
        self._stream = None
        try:
            replacement = self._start_stream(target)
        except Exception as exc:
            self.flush()
            message = f"默认扬声器重连失败（{exc}），会继续重试"
            if message == self._last_output_error:
                return None
            self._last_output_error = message
            return message
        self._stream = replacement
        previous = self._opened_device
        self._opened_device = target
        self._last_output_error = ""
        if previous == target:
            return f"默认扬声器设备 {target} 已重新连接"
        return f"默认扬声器已切换到设备 {target}"

    def play(self, pcm: bytes) -> None:
        if self._stream is None:
            # Muted run (no sounddevice, or the device refused to start).
            # Buffering here would be worse than useless: nothing drains it,
            # so `busy` would latch True and the playback gate would hold the
            # floor shut for the rest of the stream.
            return
        with self._lock:
            if not self._enabled:
                return
            self._buffer.extend(pcm)
            over = len(self._buffer) - _MAX_BACKLOG_BYTES
            if over > 0:
                # Skip forward rather than fall further behind. A co-host that
                # is a minute late is worse than one that dropped a sentence:
                # the audience hears an answer to a comment nobody remembers.
                # Only reachable when the floor gate is bypassed; loud on
                # purpose, because silent drift is what this replaces.
                # Whole samples only. Dropping an odd byte count shifts every
                # later sample by one byte, and 16-bit audio read half a
                # sample out of phase is not quiet damage — it is white noise
                # at full scale for the rest of the run.
                over -= over % 2
                del self._buffer[:over]
                self._dropped_s += over / 2.0 / _OUTPUT_RATE

    @property
    def backlog_s(self) -> float:
        """Seconds of audio waiting to be heard."""
        with self._lock:
            return len(self._buffer) / 2.0 / _OUTPUT_RATE

    @property
    def dropped_s(self) -> float:
        """Seconds skipped to stay current. Nonzero means the gate leaked."""
        return self._dropped_s

    def flush(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._primed = False
            self._reply_finished = False

    def finish_reply(self) -> None:
        """Release a sub-preroll final packet once generation has completed."""
        with self._lock:
            self._reply_finished = bool(self._buffer)
            if not self._buffer:
                self._primed = False

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable playback; disabling also stops already-buffered speech."""
        with self._lock:
            self._enabled = enabled
            if not enabled:
                self._buffer.clear()
                self._primed = False
                self._reply_finished = False

    def status(self) -> dict[str, object]:
        """Expose playback continuity counters for the health panel."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "opened_device": self._opened_device,
                "backlog_s": round(len(self._buffer) / 2.0 / _OUTPUT_RATE, 3),
                "dropped_s": round(self._dropped_s, 3),
                "rebuffer_count": self._rebuffer_count,
                "underrun_blocks": self._underrun_blocks,
                "callback_status_count": self._callback_status_count,
                "last_error": self._last_output_error,
            }

    def close(self) -> None:
        """Release the output stream. BLOCKING — call via asyncio.to_thread.

        Without this the stream lived until interpreter teardown, where
        PortAudio's atexit release ran AFTER the farewell print — the exit
        that "hangs after 再见". Idempotent so both exit paths may call it.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            _close_audio_stream(stream)

    @property
    def busy(self) -> bool:
        with self._lock:
            return len(self._buffer) > 0


async def _watch_default_output(speaker: _Speaker) -> None:
    """Keep desktop playback on the current macOS default output.

    A Bluetooth headset that connects after startup becomes the system default
    without any notification PortAudio can hear, so poll once a second; the
    speaker itself refuses to move while pinned (--output-device) or while a
    page holds the devices.
    """
    while True:
        await asyncio.sleep(1.0)
        message = await asyncio.to_thread(speaker.refresh_default_device)
        if message:
            print(f"[音频] {message}")


async def _consume(
    client: RealtimeClient, speaker: _Speaker | None, reply_wav: Path | None
) -> None:
    await _consume_events(client.events(), speaker, reply_wav)


async def _consume_events(
    events: AsyncIterator[link.LinkEvent],
    speaker: _Speaker | None,
    reply_wav: Path | None,
    *,
    stream_text: bool = True,
    link_health: LinkHealth | None = None,
    on_transcript: Callable[[str], None] | None = None,
    output_enabled: Callable[[], bool] | None = None,
) -> None:
    """Print what she says and play what she sends.

    Args:
        events: One view of the link's event stream.
        speaker: Playback sink, None in WAV mode.
        reply_wav: Where to save the reply audio, None to skip.
        link_health: Health probe to keep current, None to skip. This coroutine
            already reads every link event, and those events are the only place
            "is the provider connected" exists (plan §4.12).
        stream_text: Print reply text character by character. MUST be False
            while a prompt_toolkit prompt owns the terminal: a line without its
            newline has not scrolled yet, so the prompt's next repaint
            (`ESC[J`) erases it — the reply vanished and left only the end
            marker behind. Under the prompt the text still arrives live, but
            one finished sentence at a time (see _SENTENCE_END): whole lines
            survive the repaint, and waiting for the WHOLE reply put the
            terminal visibly behind the browser bubble.
    """
    collected: list[bytes] = []
    said: list[str] = []
    buffered: list[str] = []  # printed a sentence at a time when not streaming
    # Recognition runs BESIDE generation, not before it, so the line saying what
    # the streamer said can land after the reply answering it — printed in
    # arrival order the log reads as if she answered first, and the reader's
    # first suspicion falls on the part that is working. So her reply waits for
    # the transcript of the turn it belongs to.
    #
    # Whether this provider transcribes at all is not knowable up front — we
    # never ask for it (no session field sets it) and the client does not read
    # session.updated — so the first turn guesses yes and finds out. Guessing
    # yes costs a provider that never transcribes (s2s with `--stt none`) one
    # turn of buffering per connection, after which it is never made to wait
    # again; guessing no would cost the first turn's ORDER on every provider
    # that does, and order is the thing being fixed.
    transcribes: bool | None = None
    awaiting = False
    held: list[str] = []
    # One correlation id per turn (plan §4.12). `bind` had no caller anywhere in
    # src until this one, so turn_id reached zero log lines and the log could be
    # grouped by event name and by nothing else. Held open across iterations
    # rather than around a block, because a turn IS several iterations.
    turn = contextlib.ExitStack()
    turn_id = ""
    turn_seq = itertools.count(1)

    def open_turn() -> None:
        nonlocal turn_id
        if turn_id:
            return
        turn_id = f"t{next(turn_seq)}-{os.getpid():x}"
        turn.enter_context(bind(turn_id=turn_id))

    def close_turn(status: str) -> None:
        nonlocal turn_id
        if not turn_id:
            return
        # Inside the binding, so the one line that summarises a turn carries the
        # id that ties the rest of it together.
        log.info("dev_talk.turn_done", status=status, reply_chars=sum(len(s) for s in said))
        turn.close()
        turn_id = ""

    def say(text: str, *, end: str = "\n") -> None:
        if awaiting:
            held.append(text + end)
            return
        print(text, end=end, flush=end == "")

    def release() -> None:
        nonlocal awaiting
        awaiting = False
        if held:
            print("".join(held), end="", flush=True)
            held.clear()

    def flush_sentences(*, final: bool = False) -> None:
        text = "".join(buffered)
        if not final:
            cut = max((text.rfind(mark) for mark in _SENTENCE_END), default=-1)
            if cut < 0 and len(text) < _LINE_SOFT_CAP:
                return
            if cut < 0:
                cut = len(text) - 1  # no punctuation in a long run: break anyway
            text, rest = text[: cut + 1], text[cut + 1 :]
            buffered[:] = [rest]
        else:
            buffered.clear()
        if text:
            say(text)

    try:
        async for event in events:
            if isinstance(event, link.UserTranscriptDone):
                transcribes = True
                # Out from under the hold first, so this line lands ahead of the
                # reply it explains rather than joining the queue behind it.
                awaiting = False
                open_turn()
                print(f"你说：{event.text}")
                if on_transcript is not None and event.text:
                    on_transcript(event.text)
                release()
            elif isinstance(event, link.ReplyTextDelta):
                open_turn()  # a proactive reply starts a turn with no speech before it
                said.append(event.text)
                if stream_text:
                    say(event.text, end="")
                else:
                    buffered.append(event.text)
                    flush_sentences()
            elif isinstance(event, link.ReplyAudioDelta):
                if event.handle.stale:
                    # Same tail the browser path drops: flush() empties what was
                    # buffered, and this is what arrives after it.
                    continue
                open_turn()
                collected.append(event.pcm)
                if speaker is not None and (output_enabled is None or output_enabled()):
                    speaker.play(event.pcm)
            elif isinstance(event, link.SpeechStopped):
                awaiting = transcribes is not False
            elif isinstance(event, link.SpeechStarted):
                # A new turn starting settles the last one either way: whatever it
                # was holding belongs on screen now, transcript or no transcript.
                release()
                open_turn()
                if speaker is not None:
                    speaker.flush()  # the local half of playback.clear
            elif isinstance(event, link.ReplyDone):
                if speaker is not None:
                    # Generation is over: release a sub-preroll tail instead of
                    # holding it hostage to a cushion that will never fill.
                    speaker.finish_reply()
                marker = f"—— 回复结束（{event.status}）——"
                if stream_text:
                    say(f"\n{marker}")
                else:
                    flush_sentences(final=True)  # whatever the last sentence left
                    say(marker)
                # The transcript lost the race for good; nothing else will release.
                if transcribes is None:
                    transcribes = False  # asked once, answered: never wait again
                release()
                # Before said.clear(), which is what the summary counts.
                open_turn()
                close_turn(event.status)
                said.clear()
                if reply_wav is not None and collected:
                    with wave.open(str(reply_wav), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(_OUTPUT_RATE)
                        w.writeframes(b"".join(collected))
                    print(f"回复音频已存：{reply_wav}")
                    return
                collected.clear()
            elif isinstance(event, link.LinkDown):
                release()  # a dropped link owes the reader whatever it was holding
                if link_health is not None:
                    link_health.mark_down(event.reason, retrying=event.retrying)
                tail = "，正在自动重连…" if event.retrying else "，已放弃重连。"
                print(f"[链路] 断了（{event.reason}）{tail}", file=sys.stderr)
            elif isinstance(event, link.LinkUp):
                if link_health is not None:
                    link_health.mark_up(attempts=event.attempts)
                print(f"[链路] 已恢复（第 {event.attempts} 次尝试成功），人设已重新推送。")
            elif isinstance(event, link.LinkError):
                if link_health is not None:
                    link_health.mark_error(event.code, event.detail)
                print(f"[错误] {event.code}: {event.detail}", file=sys.stderr)
                # The provider refusing something is the one link event with no
                # log of its own: the client logs the drops and the reconnects
                # (link.reconnected / link.fatal, client.py:367-453) and emits
                # this one without a word. A wrong voice name arrives here and
                # nowhere else, and what the streamer sees is a silent session.
                log.warning("dev_talk.link_error", code=event.code, error_text=event.detail)
    finally:
        # A cancelled consumer must not leave the contextvar set: whatever logs
        # next on this task would inherit a turn that is over, which reads as
        # evidence rather than as a leftover.
        turn.close()


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
        # Forwarded, not recomputed: this wrapper stands in for the link
        # everywhere the scheduler looks, so it has to answer the same.
        self.quiet_window_s = inner.quiet_window_s
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

    async def suspend(self) -> None:
        # The pump stays up: the adapter's events() queue never terminates
        # across a suspend, so every view upstairs just goes quiet with it.
        await self._inner.suspend()

    async def resume(self) -> None:
        await self._inner.resume()

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


def _paced_thresholds(
    chattiness: Chattiness, reply_length: Chattiness, pacing: EventPacingSnapshot
) -> DerivedThresholds:
    """The selector's rules for the NEXT window: chattiness × room load.

    Both halves of the pacer's verdict ride along — the window length AND the
    score bar (quiet rooms lower it, busy rooms raise it). Forwarding only the
    window was this function's founding bug: the dynamic bar had a producer
    and a status line but no consumer.
    """
    return effective_thresholds(
        chattiness,
        reply_length=reply_length,
        danmaku_window_s=max(1, round(pacing.danmaku_window_s)),
    ).model_copy(update={"score_threshold": pacing.score_threshold})


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
    "/sc 阿强 30 主播玩什么" — Super Chat (yuan, like the platform);
    "/gift 阿强 52" — a 52-battery gift (the unit the gift panel shows,
    and the unit the tier thresholds read).
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
        if value is None or not value.is_integer():
            return None
        # Batteries, not yuan: tiering reads Gift.total_battery, so a console
        # gift priced only in gold coins never reached the paid lanes.
        batteries = int(value)
        return LiveEvent(
            kind=EventKind.GIFT,
            viewer=_console_viewer(parts[0]),
            gift=Gift(name="礼物", num=1, coin_type="gold", unit_battery=batteries),
            value_cny=batteries / 10.0,
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


class _Busy(Protocol):
    """Anything that knows whether sound is coming out of it."""

    @property
    def busy(self) -> bool: ...


def _audio_busy(speaker: _Busy, page: _Busy) -> bool:
    """Whether sound is being made, wherever it is coming out.

    Two producers, never both live: the local sounddevice speaker answers for a
    run with no page, and PlaybackTally answers while a page holds the devices
    — where the local speaker has no stream at all and reads False for the
    whole reply (ui/audio.py PlaybackTally.busy, ui/hub.py VoiceSignals).
    Asking only the local one is what left the pet showing 「空闲」 from the
    moment she started talking.
    """
    return speaker.busy or page.busy


class _Credentialed(Protocol):
    """The two questions a connected danmaku source can answer about its login."""

    @property
    def logged_in(self) -> bool: ...

    @property
    def credential_stale(self) -> bool: ...


async def _watch_credential(source: _Credentialed, *, poll_s: float = 1.0) -> None:
    """Correct the startup banner once the platform has answered.

    The 「登录态」 line goes out before the connection exists, so all it can see
    is that a credential string resolved. An expired SESSDATA still gets a
    successful init_room with uid 0 (ingest/bilibili/source.py `_resolved_uid`),
    and every viewer arrives masked — which reads as the memory层 being broken,
    not as a login that quietly expired.

    The source logs this too; this is the console's copy, because the banner it
    corrects was read on the console.

    Args:
        source: The live danmaku source.
        poll_s: How often to re-ask while the answer is still unknown.
    """
    while not (source.logged_in or source.credential_stale):
        await asyncio.sleep(poll_s)
    if source.credential_stale:
        print(
            "[弹幕] 更正：SESSDATA 已失效，这一场连的其实是匿名——观众全部打码，认不出常客。\n"
            "        重新登录 B 站取一份新的 SESSDATA，写进 path.sh 的 BILI_SESSDATA 后重开。"
        )


class _Panics(Protocol):
    """The Scheduler's two panic controls, and nothing else."""

    def panic_mute(self) -> None: ...

    def release_panic(self) -> None: ...


# Whole lines, not prefixes: the console is a danmaku box first, and a prefix
# match would swallow 「/mute now」 as if it had been understood.
# 「闭麦」两个字在这个程序里曾经指两件相反的事：这个开关停的是**她**，而
# --mute-while-speaking 停的是**麦克风**。按钮和提示都改成「叫停」了，旧的两个
# 中文别名留着不删——直播中途手一抖打了旧的，不该什么都不发生。
_PANIC_COMMANDS = {
    "/mute": True,
    "/叫停": True,
    "/闭麦": True,
    "/unmute": False,
    "/恢复": False,
    "/开麦": False,
}


def _panic_command(text: str) -> bool | None:
    """Whether a typed line is the panic switch.

    Args:
        text: One console line as typed.

    Returns:
        True to mute, False to release, None if this is not a command at all.
    """
    return _PANIC_COMMANDS.get(text.strip().lower())


def _apply_panic(mute: bool, panics: _Panics, say: Callable[[str], None]) -> None:
    """Flip the panic switch and announce it (backlog #47).

    The web panel had the only trigger, so `--no-ui` — or a taken port, which
    parks the hub silently — left the one 「出事了一键闭嘴」 mechanism with no
    way to reach it. `say` gets the same sentence the panel prints, so the
    terminal and the page read alike.
    """
    if mute:
        panics.panic_mute()
        say("紧急叫停（终端）：她不再开口，直到 /unmute")
    else:
        panics.release_panic()
        say("恢复说话（终端）")


async def run_director(args: argparse.Namespace) -> int:
    """The whole stage-3 assembly behind the mic, before Electron exists.

    Wire mode (the default) talks straight through RealtimeClient and knows
    nothing of L3. This mode stands the real stack up instead: persona,
    memory, distiller, proactive loop, scheduler — the same objects the
    acceptance tests compose, with a microphone on one side and the console
    standing in for the danmaku feed on the other.
    """
    from secrets import token_hex

    from pydantic import ValidationError

    from bilisama import secrets
    from bilisama.app import Assembly
    from bilisama.cli import DEFAULT_CONFIG, report_problems, report_validation
    from bilisama.clock import SystemClock
    from bilisama.config import ConfigError, check, derive, load
    from bilisama.config._ui import Reload
    from bilisama.config.persist import ConfigWriteError, TomlConfigWriter
    from bilisama.config.schema import SideModelConfig
    from bilisama.director.floor import SpeakingFloor
    from bilisama.director.intent import Intent
    from bilisama.director.scheduler import Scheduler
    from bilisama.ingest.sources import QueueSource
    from bilisama.memory.distill import Distiller
    from bilisama.memory.store import MemoryStore
    from bilisama.obs.health import HealthRegistry
    from bilisama.obs.loop_lag import LoopLagMonitor
    from bilisama.obs.outcome import Outcome, OutcomeWindow, Verdict
    from bilisama.persona.loader import (
        PersonaStore,
        default_data_dir,
        live_event_rules,
        live_voice_rules,
        template_variables,
    )
    from bilisama.proactive import ProactiveTopicLoop
    from bilisama.realtime.providers.volcano import VolcanoLink
    from bilisama.side import OpenAICompatSideModel, SideModel
    from bilisama.ui.audio import AudioBroker, EchoProbe, PlaybackTally
    from bilisama.ui.config_edit import (
        ConfigEditError,
        apply_runtime_config_edit,
        speak_paths,
    )
    from bilisama.ui.events import ClientEvent, ServerEvent, link_frames, live_event_payload
    from bilisama.ui.hub import UiHub, VoiceSignals
    from bilisama.ui.poke import PokeResponder
    from bilisama.ui.server import (
        UiServer,
        bind_ui_socket,
        create_ui_app,
        default_endpoint_path,
        write_endpoint_file,
    )

    config_path: Path = args.config or DEFAULT_CONFIG
    if not config_path.is_file():
        # load() treats a missing path as "all defaults", which silently ignores
        # a typo'd --config — the one case where defaults are a lie (D6).
        raise SystemExit(f"找不到配置文件：{config_path}")
    overrides: dict[str, Any] = _director_session_overrides()
    if args.persona:
        overrides["persona"] = {"id": args.persona}
    if args.skin:
        # A run-scoped override, same layer as --persona: nothing touches the
        # tracked toml. "tofu" selects the built-in robot explicitly.
        if args.skin == "tofu":
            overrides["avatar"] = {"renderer": "tofu", "model_id": ""}
        else:
            overrides["avatar"] = {"renderer": "sprite", "model_id": args.skin}
    try:
        settings = load(config_path, overrides=overrides, strict=False)
    except ConfigError as exc:
        # A broken TOML, or a file a newer build wrote. strict=False does not
        # cover either — the file cannot be read at all — and letting it out of
        # here means a traceback where a sentence belongs (§7.6).
        print("配置读不了：", file=sys.stderr)
        report_problems(exc.problems, stream=sys.stderr)
        raise SystemExit(2) from exc
    except ValidationError as exc:
        print("配置读不了：", file=sys.stderr)
        report_validation(exc, stream=sys.stderr)
        raise SystemExit(2) from exc
    # strict=False keeps a half-configured dev box usable, but the problems
    # still get said out loud instead of silently shaping behaviour (D6).
    # Kept as a list: the same problems are logged once logging exists, a few
    # lines below. They cannot be logged here — nothing is configured yet.
    config_problems = list(check(settings, config_dir=config_path.parent))
    for problem in config_problems:
        tag = "错误" if problem.fatal else "提醒"
        fix = f"（{problem.fix}）" if problem.fix else ""
        print(f"[配置{tag}] {problem.field}：{problem.message}{fix}")
    # Without this, background warnings (proactive.refresh_failed and friends)
    # reach the console as bare event names with their error fields dropped.
    from bilisama.obs.logging import rolling_file_handler
    from bilisama.obs.logging import setup as logging_setup
    from bilisama.paths import log_dir

    clock = SystemClock()
    # The hub exists before logging so its ring handler catches every line;
    # bind failure later just parks the hub unused (its staging is bounded).
    hub: UiHub | None = UiHub(clock) if not args.no_ui else None
    # Built once and reused by every relog(): setup() clears the root handlers,
    # so a handler created per call would reopen the file and leak the old one.
    log_path = log_dir() / "dev-talk.jsonl"
    file_tee: tuple[logging.Handler, ...] = ()
    try:
        file_tee = (rolling_file_handler(log_path),)
    except OSError as exc:
        # No disk to write to is not a reason to refuse a stream; the panel and
        # the console still work, and the streamer is told what they lost.
        print(f"[日志] 落盘用不了（{exc}）；这一场只有终端和面板", file=sys.stderr)
    log_tee: tuple[logging.Handler, ...] = (
        (hub.log_handler, *file_tee) if hub is not None else file_tee
    )
    # Bound here, set for real once the console mode is known: relog() reads it
    # at call time, and a panel edit can arrive before that point.
    use_prompt = False
    # Captured before anything can put the tty in raw mode, restored at the top
    # of the shutdown chain (see _sane_terminal).
    saved_tty: Any = None
    if sys.stdin.isatty() and sys.platform != "win32":
        with contextlib.suppress(Exception):
            import termios

            saved_tty = termios.tcgetattr(sys.stdin.fileno())

    def relog() -> None:
        """(Re)install logging from the CURRENT settings.

        Both runtime.log_* fields are live-editable, and logging snapshots them
        at setup — so every path that changes one calls this. The stream
        follows the prompt window; log_viewer_content rides along, which before
        this was a config field nothing read at all.
        """
        logging_setup(
            level=settings.runtime.log_level,
            # The panel and the file take the whole record; the terminal keeps
            # its own Chinese narration and only wants to hear about trouble.
            # Without a panel there is nowhere else to read info lines, so the
            # console goes back to showing everything.
            console_level="warning" if hub is not None else None,
            log_viewer_content=settings.runtime.log_viewer_content,
            stream=sys.stdout if use_prompt else None,
            extra_handlers=log_tee,
        )

    relog()
    # Now that there is somewhere to write. The console said all of this
    # already, in Chinese, before the file and the panel existed; this is the
    # copy that survives the session. `detail` and `advice` are diagnostic
    # field names, so the scrubber leaves them whole (obs/logging.py).
    for problem in config_problems:
        # Name kept clear of `report`: the distillation report at the bottom of
        # this function owns that one, and the two would be the same variable.
        say_problem = log.warning if problem.fatal else log.info
        say_problem(
            "dev_talk.config_problem",
            field=problem.field,
            fatal=problem.fatal,
            detail=problem.message,
            advice=problem.fix or "",
        )
    thresholds = derive(settings.interaction.chattiness)

    stop = asyncio.Event()

    def on_sigint() -> None:
        if stop.is_set():
            # Second press means "out, now". Raising KeyboardInterrupt here
            # only unwinds asyncio.run, which still gathers every finally and
            # then JOINS the to_thread pool — including the multi-second
            # PortAudio release that made the user press twice in the first
            # place. os._exit skips atexit and that join: this is the promised
            # escape hatch, and the polite path already had its turn.
            print("\n[退出] 强退（本场蒸馏未做）。", file=sys.stderr)
            sys.stderr.flush()
            sys.stdout.flush()
            os._exit(130)  # 128 + SIGINT, the shell's own convention
        stop.set()
        # Feedback beats patience: without this line the polite teardown
        # (distill, socket farewells) looks like a hang.
        print("\n[退出] 正在收尾（蒸馏、断连）…再按一次 Ctrl-C 强退。", file=sys.stderr)

    def arm_sigint() -> None:
        """(Re)install the SIGINT handler. Call it whenever the prompt lets go.

        prompt_toolkit REMOVES the loop's SIGINT handler when its session ends
        (verified against the installed version) — it owns Ctrl-C as a key
        while it runs, and takes ours with it on the way out. Nothing then
        catches the second press, so the "再按一次 Ctrl-C 强退" promise died
        exactly where it was needed: during the shutdown chain, with
        distillation still on the wire.
        """
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, on_sigint)

    # Installed before the first await so a Ctrl-C during connect/setup exits
    # cleanly instead of unwinding with a traceback (C7).
    arm_sigint()

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
    variables = template_variables(settings.persona, reply_length=settings.interaction.reply_length)
    # The two turn contracts (config/personas/live/). Loaded once here; a
    # missing file is a packaging error and refuses to start rather than
    # running a stream that cannot tell the streamer's voice from a danmaku.
    voice_rules = live_voice_rules(config_path.parent, variables)
    event_rules = live_event_rules(config_path.parent, variables)

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
        print(
            "提示：没配侧路模型（[speech.side] 或 source path.sh），"
            "主动话题改由 Realtime 直接生成；这场不做记忆蒸馏。"
        )
    else:
        print(f"[侧路] {side_desc}")

    # Command line, then config, then environment — the order the key and the
    # voice already follow. Until this call the flags' own defaults decided,
    # so [speech] never got a vote.
    endpoint = resolve_endpoint(
        settings, provider=args.provider, url=args.url, model=args.model, env=os.environ
    )
    provider = endpoint.provider
    # Which credential to look for, which turn types the endpoint honours, how
    # the model joins the address: all of it is the provider's business, and it
    # used to be spelled out right here as an if/elif whose `else` refused
    # anything new. The factory owns that now (realtime/providers/factory.py).
    built = build_link(
        LinkRequest(
            endpoint=endpoint,
            settings=settings,
            voice=args.voice,
            # {{agentName}}, the same expression the persona templates resolve,
            # so she cannot introduce herself as one name and be addressed as
            # another. Only volcano reads it — see LinkRequest.bot_name.
            bot_name=variables["agentName"],
            model_explicit=bool(args.model),
            env=os.environ,
            # Audio replies, not the shipping default: this stands against the
            # zero-patch official pipeline whose own TTS does the speaking. A
            # text-pinned session would mute every reply until stage 4 exists.
            text_replies=False,
        )
    )
    from bilisama.realtime.providers.hosted import HostedLink
    from bilisama.ui.audio import AudioInputSwitch

    inner: link.SpeechLink = built.link
    hosted_link = inner if isinstance(inner, HostedLink) else None
    connect_url: str = built.url
    # After the build, not before it: only here is connect_url the address that
    # will actually be dialed, model and all. Printed earlier the line could
    # only guess which model wins — and a banner that guesses is how a
    # swallowed --model stayed invisible. Every refusal inside the factory
    # carries its own fix action, so none of them needed this line.
    print(f"[语音] {provider.value} @ {_endpoint_line(connect_url)}（来自{endpoint.source}）")
    speech = _Fanout(inner)
    await _connect_or_exit(speech, provider, connect_url)
    # Every uplink frame — page socket or local sounddevice — passes this one
    # switch: pause gate, input toggle (silence-substituted), noise gate, and
    # the live-mock source election all live here (ui/audio.py).
    audio_input = AudioInputSwitch(
        speech.push_audio, noise_sensitivity=settings.audio.noise_sensitivity
    )

    # The pause total-gate's state, shared by the panel handler and hello().
    runtime_paused = False
    # Which panic the PAUSE triggered, so resume releases only its own: a
    # streamer who hit 紧急叫停 first keeps her red button held through a
    # pause/resume cycle.
    panic_by_pause: set[str] = set()

    def output_enabled() -> bool:
        """The downlink gate, read at both sinks (page broker and local
        speaker). Off keeps the text flowing — the chat page still shows the
        reply — only the sound stays home."""
        return settings.audio.output_enabled

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
    # Plan section 3.3 rule 1, asked of the LINK. Which numbers make up the
    # window is the provider's business, and every previous arrangement had
    # this file doing arithmetic over provider config — first a two-armed
    # branch whose `else` handed volcano DashScope's endpointing, then a
    # registry call that still left two readers of the same fields at two
    # altitudes. The floor also holds while an implicit reply is generating,
    # so this timer only carries the no-reply case.
    quiet_s = speech.quiet_window_s

    # The window health reports from (plan §4.12). Fed here rather than inside
    # the scheduler: this is already the one place every Verdict passes through.
    outcomes = OutcomeWindow()

    def verdict_sink(verdict: Verdict) -> None:
        outcomes.note(verdict)
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

    # The last few lines actually said on air — streamer transcript and her
    # own completed replies — so the selector's zero-model relevance check can
    # hard-accept an audience line that picks up the current conversation.
    # 300s freshness at read time, one normalised line each, capped at 8.
    recent_dialogue: deque[tuple[float, str]] = deque(maxlen=8)

    def note_recent_line(text: str) -> None:
        line = " ".join(text.split())[:240]
        if line:
            recent_dialogue.append((clock.monotonic(), line))

    def spoken_line(text: str) -> None:
        distiller.note_assistant_line(text)
        proactive.note_dialogue("assistant", text)
        note_recent_line(text)

    scheduler = Scheduler(
        speech,
        floor,
        clock,
        # No global cooldown: the pacer's token bucket owns the rate of
        # ordinary speech now, and a fixed post-reply wait on top of it is
        # exactly the dead air the dynamic pacing exists to remove.
        cooldown_s=0.0,
        quiet_after_speech_s=quiet_s,
        verdict_sink=verdict_sink,
        guard=guard,
        on_hit=settings.safety.on_hit,
        # One clean spoken line, two readers: the distiller's voice-exemplar
        # buffer and the proactive loop's dialogue material.
        spoken_sink=spoken_line,
    )

    from bilisama.event_pacing import EventPacer
    from bilisama.ingest.bilibili.selector import DanmakuSelector, EntryCoalescer

    event_pacer = EventPacer(clock, chattiness=lambda: settings.interaction.chattiness)

    def live_thresholds() -> DerivedThresholds:
        # Read per window-open; the merge itself is module-level and tested.
        return _paced_thresholds(
            settings.interaction.chattiness,
            settings.interaction.reply_length,
            event_pacer.snapshot(),
        )

    def selector_context_lines() -> tuple[str, ...]:
        # What the stream is about plus what was just said on air, for the
        # relevance hard-accept.
        now = clock.monotonic()
        recent = tuple(line for stamp, line in recent_dialogue if now - stamp <= 300.0)
        return tuple(line for line in (settings.room.stream_intro, *recent) if line.strip())

    def selector_mention_terms() -> tuple[str, ...]:
        cfg = settings.persona
        return tuple(
            term
            for term in (cfg.display_name.strip(), cfg.id.strip(), cfg.streamer_name.strip())
            if term
        )

    selector = DanmakuSelector(
        clock,
        thresholds=live_thresholds,
        context_lines=selector_context_lines,
        mention_terms=selector_mention_terms,
        # Window winners wait out the streamer AND an empty bucket: no intent
        # exists while blocked, so no TTL burns during a monologue.
        delivery_blocked=lambda: floor.streamer_speaking or not event_pacer.can_consume("danmaku"),
    )
    entries = EntryCoalescer(clock, policy=event_pacer.snapshot)

    async def observe_context_item(text: str) -> None:
        await speech.add_context_item(text)

    def publish_room_event(event: LiveEvent) -> None:
        """Mirror each adopted platform event to the control-centre timeline —
        the wiring half of ledger #49: real room events finally reach the
        panel, not just console injections."""
        if hub is not None:
            hub.broadcast(ServerEvent.EVENT_FEED, live_event_payload(event))

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
        assistant_label=settings.persona.display_name or settings.persona.id,
        event_pacer=event_pacer,
        # Real interaction about to speak outranks an icebreaker before either
        # queues: an open danmaku window, a held deferred winner, or a pending
        # welcome batch all wave the topic off this tick.
        ordinary_pending=lambda: bool(
            selector.status()["window_open"]
            or selector.status()["deferred_count"]
            or entries.status()["pending"]
        ),
        # Late-bound on purpose: `assembly` is built a page below, and the
        # loop only calls this once it is running.
        reply_base_instructions=lambda: assembly.build_public_context(),
    )

    def on_streamer_transcript(text: str) -> None:
        # Two readers of one transcript line: topic material for the proactive
        # loop, relevance material for the danmaku selector.
        proactive.note_dialogue("streamer", text)
        note_recent_line(text)

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
        max_tokens=live_thresholds().max_output_tokens,
        protect_ms=settings.interaction.sc_protect_ms,
        variables=variables,
        clock_granularity_min=settings.memory.clock_granularity_min,
        selector=selector,
        entries=entries,
        event_pacer=event_pacer,
        entry_group_enabled=lambda group: bool(
            getattr(settings.interaction.entry_welcome, group, False)
        ),
        observe_context_item=observe_context_item,
        event_observer=publish_room_event,
        voice_rules=voice_rules,
        event_rules=event_rules,
        stream_intro=lambda: settings.room.stream_intro,
        per_reply_scope=profile_for(provider).caps.per_reply_base_instructions,
        gift_battery_high=settings.interaction.gift_battery_high,
        gift_battery_medium=settings.interaction.gift_battery_medium,
    )

    # ------------------------------------------------------------ panel edits
    # The write channel the control centre uses: validate -> apply in memory
    # -> run the reload hook -> persist (unless session-only) -> roll back on
    # any failure. apply_runtime_config_edit widened the gate, so every path
    # it admits MUST have a hook here — a missing branch is the "面板说改好了
    # 但没生效" bug.
    config_writer = TomlConfigWriter(config_path, settings)

    def refresh_interaction_settings() -> None:
        """Re-poke every consumer that snapshotted [interaction] at build."""
        live = live_thresholds()
        assembly.configure_interaction(
            max_tokens=live.max_output_tokens,
            protect_ms=settings.interaction.sc_protect_ms,
            gift_battery_high=settings.interaction.gift_battery_high,
            gift_battery_medium=settings.interaction.gift_battery_medium,
        )
        proactive.configure(
            max_per_hour=settings.interaction.proactive.max_per_hour,
            wake_interval_s=float(settings.interaction.proactive.wake_interval_s),
            max_tokens=live.max_output_tokens,
        )
        # The pacer's bucket owns ordinary rate; the scheduler cooldown stays 0.
        scheduler.set_cooldown(0.0)

    async def refresh_persona_settings() -> None:
        """Rebuild the persona stack after a persona.* edit, live."""
        nonlocal persona, variables, voice_rules, event_rules
        persona = PersonaStore.from_config(settings.persona, config_dir=config_path.parent)
        variables = template_variables(
            settings.persona, reply_length=settings.interaction.reply_length
        )
        voice_rules = live_voice_rules(config_path.parent, variables)
        event_rules = live_event_rules(config_path.parent, variables)
        assembly.replace_persona(
            persona,
            settings.persona.growth,
            variables,
            voice_rules=voice_rules,
            event_rules=event_rules,
        )
        distiller.replace_persona(persona, settings.persona.growth)
        proactive.configure(
            prompt=persona.proactive_prompt(
                config_path.parent / "prompts" / "proactive.md", variables
            ),
            assistant_label=settings.persona.display_name or settings.persona.id,
        )
        if isinstance(inner, VolcanoLink):
            await inner.set_bot_name(settings.persona.display_name or settings.persona.id)
        await assembly.refresh_context()

    async def run_reload_hook(path: str) -> bool:
        """The hook table apply_runtime_config_edit's contract points at.

        Returns whether a hook ran. A non-LIVE path nobody handles here must
        be REFUSED by the caller — applying it in memory with no consumer
        re-poked is the lie the widened gate makes possible.
        """
        if path in _RELOG_ON_EDIT:
            relog()
            return True
        if path == "room.stream_intro":
            await assembly.refresh_context()
            return True
        if path.startswith("persona.") or path == "interaction.reply_length":
            await refresh_persona_settings()
            if path == "interaction.reply_length":
                refresh_interaction_settings()
            return True
        if path.startswith("interaction."):
            refresh_interaction_settings()
            return True
        if path == "room.room_id":
            wanted = settings.room.room_id
            if wanted > 0:
                await room_source.connect(wanted)
            else:
                await room_source.disconnect()
            return True
        if path == "audio.noise_sensitivity":
            audio_input.set_noise_sensitivity(settings.audio.noise_sensitivity)
            return True
        if path == "audio.input_enabled":
            if not audio_input.paused:
                audio_input.set_enabled(settings.audio.input_enabled)
            return True
        if path == "audio.output_enabled":
            # Mirror the switch into the local sink so a play() call that
            # slips past the closure gate still stays silent; disabling also
            # stops the sentence in the air, not just the next one. Both
            # sinks may not exist yet this early in startup; a missing one
            # simply has nothing to flush.
            with contextlib.suppress(NameError):
                speaker.set_enabled(settings.audio.output_enabled)
            if not settings.audio.output_enabled:
                with contextlib.suppress(NameError):
                    if broker is not None:
                        broker.flush()
            return True
        if path.startswith("speech.dashscope.") and hosted_link is not None:
            await hosted_link.reconfigure_session(
                voice=settings.speech.dashscope.voice,
                turn=settings.speech.dashscope.turn,
            )
            return True
        return False

    async def apply_runtime_edit(path: str, value: Any) -> str:
        """One panel edit, end to end. Returns the receipt line to announce.

        Raises ConfigEditError with a human sentence on any refusal; a hook
        or persist failure rolls the in-memory value back and re-runs the
        hook so consumers see the old value again.
        """
        resolved_parent = settings
        for part in path.split(".")[:-1]:
            resolved_parent = getattr(resolved_parent, part)
        leaf = path.split(".")[-1]
        old = getattr(resolved_parent, leaf)
        meta, applied = apply_runtime_config_edit(settings, path, value)
        problems = [p for p in check(settings, config_dir=config_path.parent) if p.fatal]
        if problems:
            setattr(resolved_parent, leaf, old)
            raise ConfigEditError(problems[0].message + (problems[0].fix or ""))
        try:
            handled = await run_reload_hook(path)
            if not handled and meta.reload is not Reload.LIVE:
                setattr(resolved_parent, leaf, old)
                raise ConfigEditError(
                    f"「{meta.label}」这一项还没有对应的热生效通道，先在配置文件里改"
                )
            if path not in _SESSION_ONLY_PANEL_PATHS:
                config_writer.write(path, applied)
        except (ConfigWriteError, OSError, FileNotFoundError) as exc:
            setattr(resolved_parent, leaf, old)
            with contextlib.suppress(Exception):
                await run_reload_hook(path)
            raise ConfigEditError(f"「{meta.label}」没改成：{exc}") from exc
        shown_old = old.value if hasattr(old, "value") else old
        shown_new = applied.value if hasattr(applied, "value") else applied
        if path in _SESSION_ONLY_PANEL_PATHS:
            return f"{meta.label}：{shown_old} → {shown_new}（本场已生效，重启恢复默认）"
        return f"{meta.label}：{shown_old} → {shown_new}（已保存并生效）"

    # Real danmaku, when a room is named (--room beats [room] room_id). The
    # credential chain: config ref first, then path.sh's BILI_SESSDATA. No
    # credential still connects — Bilibili then masks every uid to 0, which
    # kills regular-viewer memory, so say it out loud.
    from bilisama.ingest.bilibili import BilibiliEventSource
    from bilisama.ui.room_control import SwitchableRoomSource

    room_id = args.room if args.room is not None else settings.room.room_id
    # One truth: the config reference (shipped default env:BILI_SESSDATA).
    sessdata = secrets.resolve(settings.room.credential_ref) or ""
    # Whether a credential STRING was found, which is all anyone knows this
    # early — _watch_credential corrects it once the platform has answered.
    room_credentialed = bool(sessdata)

    def live_room_source(wanted: int) -> BilibiliEventSource:
        return BilibiliEventSource(wanted, clock, sessdata=sessdata, on_sc_delete=scheduler.revoke)

    # Always built, even with no room named: the panel connects and switches
    # rooms IN PROCESS through it. room_id > 0 (from --room or the config)
    # keeps the old auto-connect behaviour as the starting target.
    room_source = SwitchableRoomSource(live_room_source, room_id)
    bili_source: SwitchableRoomSource | None = room_source
    if room_id:
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
    # The two §4.12 asks for that nothing answered: is the provider connected,
    # and what happened to the last N attempts to speak. Connected already —
    # _connect_or_exit is above and exits on failure — and kept current from the
    # playback consumer, which reads every link event anyway.
    link_health = LinkHealth(clock, provider=provider.value)
    link_health.mark_up(attempts=1)
    registry.register("link", link_health.status)
    registry.register("outcomes", outcomes.status)
    # The runtime half of the blocking-call defence (plan section 16.8 item
    # 25): a synchronous stall anywhere shows up as loop.lag with a number.
    lag_monitor = LoopLagMonitor()
    registry.register("loop", lag_monitor.status)
    registry.register("selector", selector.status)
    registry.register("pacing", event_pacer.status)
    registry.register("audio_input", audio_input.status)
    registry.register("entries", lambda: dict(entries.status()))
    if bili_source is not None:
        registry.register("bilibili", bili_source.status)

    console = QueueSource("console")
    # The acceptance console: JSON test cards injected through their own
    # QueueSource into the SAME assembly the live room feeds. A broken
    # catalog refuses to start — a card that cannot load is a card that would
    # silently not run on the night someone relies on it.
    from bilisama.ui.test_runner import MockTestRunner, load_test_catalog

    try:
        test_catalog = load_test_catalog(config_path.parent / "testsets")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"测试集读不了：{exc}") from exc

    def test_notify(state: dict[str, object]) -> None:
        if hub is not None:
            # Rides event.feed with kind="test" rather than a new event type:
            # the chat page ignores it, the test page filters for it.
            hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "test", **state})

    tests_source = QueueSource("ui-tests")
    test_runner = MockTestRunner(
        test_catalog, tests_source, clock, notify=test_notify, revoke=scheduler.revoke
    )

    from bilisama.ui.live_mock import LiveMockController

    def mock_state_sink(state: dict[str, Any]) -> None:
        if hub is not None:
            hub.broadcast(ServerEvent.LIVE_MOCK_STATE, state)

    def mock_event_sink(preview: dict[str, object]) -> None:
        if hub is not None:
            hub.broadcast(ServerEvent.LIVE_MOCK_EVENT, dict(preview))

    live_mock = LiveMockController(
        source_factory=live_room_source,
        event_sink=assembly.on_event,
        audio=audio_input,
        publish_state=mock_state_sink,
        publish_event=mock_event_sink,
    )
    seq = itertools.count(1)
    speaker = _Speaker(args.output_device)
    registry.register("audio_output", speaker.status)

    class _LocalPair:
        """The sounddevice ends, parked while a page holds the devices.

        Both halves go off the loop: taking and releasing a PortAudio stream
        blocks, and doing either here would freeze the loop that has to keep
        the page's socket alive — the hazard _close_audio_stream exists for.
        The microphone task is cancelled rather than muted, so the OS
        indicator goes out and the page's capture is the only one on the
        device.
        """

        def __init__(self) -> None:
            self.mic: asyncio.Task[None] | None = None
            self.start_mic: Any = None
            self._retired = False

        def stop_reviving(self) -> None:
            """The session is ending: whoever lets go now, stay let go.

            release() runs from uvicorn's own task (the audio endpoint's
            finally, ui/server.py:422), which keeps being scheduled while
            teardown waits on its gather — so a page still holding the
            devices at Ctrl-C hands them back INSIDE
            the shutdown, and resume() would take a fresh microphone and a
            fresh output stream that nothing left cancels or closes. What the
            streamer sees is the recording indicator coming back on after
            下播 and staying on through distillation (待验证: reasoned from
            the RawInputStream path, not observed on a device).
            """
            self._retired = True

        async def suspend(self) -> None:
            task, self.mic = self.mic, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(speaker.suspend)

        async def resume(self) -> None:
            if self._retired:
                # Before the speaker, not after: resume() reopens the output
                # device first, and that one would then stay open until the
                # very last step of teardown.
                return
            await asyncio.to_thread(speaker.resume)
            if self.start_mic is not None and self.mic is None:
                self.mic = self.start_mic()

    local_pair = _LocalPair()
    # Set once the UI server exists; None means the sounddevice pair is the
    # only audio there is, which is every run without a page open.
    broker: AudioBroker | None = None

    _USAGE = (
        "弹幕：直接打字；指定人：`阿强:内容`；/sc 名字 金额 内容；/gift 名字 电池数；"
        "/mute 紧急叫停（让她住嘴，不碰麦克风），/unmute 恢复"
    )
    _FEED_KIND = {EventKind.SUPER_CHAT: "sc", EventKind.GIFT: "gift"}

    def _no_panel() -> None:
        """Nothing to keep in step when there is no page (--no-ui, or a taken
        port), which is exactly the run the terminal switch exists for."""

    # Reassigned once the panel exists. Named rather than late-bound through the
    # UI branch's own closures: handle_line is defined before that branch, and a
    # NameError there would take the console down with it.
    sync_panel: Callable[[], None] = _no_panel

    async def handle_line(line: str, *, as_live: bool = False) -> None:
        if not line.strip():
            return  # a bare Enter injects nothing and lectures nobody
        panic = _panic_command(line)
        if panic is not None:
            # Backlog #47: this is the only entrance without a browser.
            _apply_panic(panic, scheduler, lambda said: print(f"[叫停] {said}"))
            sync_panel()  # the page's red button must not disagree with the terminal
            return
        event = _parse_console_event(line, next(seq))
        if event is None:
            print(_USAGE)
            if hub is not None:
                # The panel's inject box needs the same feedback the console gets.
                hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": _USAGE})
            return
        if as_live:
            # The system page's mock box: run the injection through the same
            # crowd funnel a real room event takes, instead of the console's
            # direct lane — that is the whole point of testing with it.
            active_room_id = int(room_source.status().get("active_room_id") or 0)
            event = _as_live_mock_event(event, active_room_id)
        label = {EventKind.SUPER_CHAT: "SC", EventKind.GIFT: "礼物"}.get(event.kind, "弹幕")
        money = f"（¥{event.value_cny:.0f}）" if event.value_cny else ""
        body = f"：{event.text}" if event.text else ""
        print(f"[已注入 {label}] {event.viewer.name}{body}{money}")
        # The head of the funnel, and the only entry point where the operator
        # is the author. event_id is what ties this to whatever the selector
        # and the scheduler decide about it further down; `text` is a viewer
        # field name on purpose, so the body is folded to a length.
        log.info(
            "dev_talk.console_injected",
            kind=str(event.kind),
            event_id=event.event_id,
            text=event.text,
            value_cny=event.value_cny,
        )
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

    # Resources acquired below (UI server, endpoint file, the pet shell) must
    # be torn down even when the console setup that follows them raises — a
    # half-installed prompt_toolkit used to orphan the shell and leave a stale
    # endpoint.json behind. Pre-bound here so the finally can name them.
    ui_server: UiServer | None = None
    ui_url = ""
    endpoint_file: Path | None = None
    pet_proc: asyncio.subprocess.Process | None = None
    console_patch = contextlib.ExitStack()
    tasks: list[asyncio.Task[None]] = []
    try:
        # ------------------------------------------------------------ UI server

        if hub is not None:
            poke = PokeResponder(
                clock, submit=scheduler.submit, max_tokens=thresholds.max_output_tokens
            )
            # The one question Chromium cannot answer: is she hearing herself?
            # Its canceller only removes what the shell played, so a reply
            # routed back out by OBS — the very setup this product's config
            # page recommends — echoes with nothing complaining anywhere.
            echo = EchoProbe()
            registry.register("echo", echo.status)

            async def uplink(pcm: bytes) -> None:
                echo.note_captured(pcm)
                await audio_input.push_audio(pcm)

            # The gate SpeakingFloor has always had and nothing ever fed with
            # real receipts. Counting segments rather than watching the last
            # one is the whole point — see PlaybackTally (ledger #41).
            async def ui_audio_level() -> None:
                """~5 Hz, change-driven: the system page's live meter reads
                what the PROVIDER actually hears (post noise gate metering),
                not the page's own analyser."""
                last = -1
                while True:
                    await clock.sleep(0.2)
                    level = audio_input.signal_level
                    if level != last:
                        last = level
                        live_hub.broadcast(
                            ServerEvent.AUDIO_LEVEL, {"source": "uplink", "level": level}
                        )

            tally = PlaybackTally(on_playback=floor.on_playback, notify=scheduler.notify)
            # Built after the tally, not before, so on_handoff can name it
            # outright: a late-bound closure would work at runtime and hide the
            # dependency, and this file already has enough of those.
            #
            # create_ui_app wires the announcement and the panel relay; the
            # only thing this end owns is which devices get parked — and that
            # the count goes with them. Every receipt the tally lives on is
            # sent by the page (ui/web/js/audio.js:248): a page that leaves
            # mid-sentence never sends the `ended` half — that one goes out on
            # a socket already closed and is dropped silently (audio.js:24) —
            # and the count keeps the offset for the rest of the session.
            # First the floor gate is stuck shut and she stops talking; then,
            # once something else happens to clear queued_audio, the same
            # offset stops the gate CLOSING, and the backlog PlaybackTally
            # exists to prevent comes back. Devices changing hands is exactly
            # "whatever was queued no longer counts".
            broker = AudioBroker(local=local_pair, on_handoff=tally.cancelled)

            async def on_playback_started(_data: dict[str, Any]) -> None:
                tally.started()

            async def on_playback_ended(_data: dict[str, Any]) -> None:
                tally.ended()

            async def on_playback_cancelled(_data: dict[str, Any]) -> None:
                tally.cancelled()

            def panel_state() -> dict[str, Any]:
                speak = settings.interaction.speak
                interaction = settings.interaction
                return {
                    "panicked": bool(scheduler.status().get("panicked")),
                    "paused": runtime_paused,
                    "speak": {
                        name: bool(getattr(speak, name)) for name in type(speak).model_fields
                    },
                    "audio": {
                        "input_enabled": settings.audio.input_enabled,
                        "output_enabled": settings.audio.output_enabled,
                        "noise_sensitivity": settings.audio.noise_sensitivity,
                        "signal_level": audio_input.signal_level,
                        "output": speaker.status(),
                    },
                    "interaction": {
                        "chattiness": interaction.chattiness.value,
                        "reply_length": interaction.reply_length.value,
                        "gift_battery_high": interaction.gift_battery_high,
                        "gift_battery_medium": interaction.gift_battery_medium,
                        "entry_welcome": {
                            "ordinary": interaction.entry_welcome.ordinary,
                            "naval": interaction.entry_welcome.naval,
                            "ranking": interaction.entry_welcome.ranking,
                        },
                        "event_pacing": event_pacer.status()["activity"],
                    },
                    "room": {
                        "stream_intro": settings.room.stream_intro,
                        "streamer_name": settings.persona.streamer_name,
                        "room_id": settings.room.room_id,
                        "connected": bool(room_source.status().get("connected")),
                        "requested_room_id": room_source.status().get("requested_room_id", 0),
                        "active_room_id": room_source.status().get("active_room_id", 0),
                        "error": room_source.status().get("error", ""),
                    },
                    "persona": {
                        "id": settings.persona.id,
                        "name": settings.persona.display_name or settings.persona.id,
                    },
                    "assistants": assistant_cards(),
                }

            def assistant_cards() -> list[dict[str, Any]]:
                from bilisama.ui import assistants as assistants_mod

                try:
                    return assistants_mod.assistant_snapshot(settings, config_path.parent)
                except OSError as exc:
                    log.warning("dev_talk.assistants_unreadable", error_text=str(exc)[:200])
                    return []

            def push_panel_state() -> None:
                """Re-send the sticky state after something OTHER than the page
                changed it — /mute typed in the terminal, for one."""
                if hub is not None:
                    hub.broadcast(ServerEvent.PANEL_STATE, panel_state())

            sync_panel = push_panel_state

            async def on_poke(_data: dict[str, Any]) -> None:
                poke.poke()  # False = cooldown; the page animates either way

            async def on_panel_set(data: dict[str, Any]) -> None:
                nonlocal runtime_paused
                if "paused" in data:
                    want = bool(data["paused"])
                    if want and not runtime_paused:
                        # The order is the design (plan A2): gate events, gate
                        # the microphone, silence the mouth, then hang up.
                        runtime_paused = True
                        assembly.set_event_input_enabled(False)
                        audio_input.set_paused(True)
                        if not scheduler.status().get("panicked"):
                            panic_by_pause.add("pause")
                            scheduler.panic_mute()
                        await speech.suspend()
                        print("[面板] 已暂停：事件、麦克风、播报和语音连接都停了")
                    elif not want and runtime_paused:
                        try:
                            await speech.resume()
                        except Exception as exc:
                            print(f"[面板] 恢复失败，保持暂停：{str(exc)[:120]}")
                            if hub is not None:
                                hub.broadcast(
                                    ServerEvent.EVENT_FEED,
                                    {
                                        "kind": "error",
                                        "code": "resume_failed",
                                        "detail": "语音连接没能恢复，先保持暂停；稍后再试一次",
                                    },
                                )
                            hub_state = panel_state()
                            if hub is not None:
                                hub.broadcast(ServerEvent.PANEL_STATE, hub_state)
                            return
                        audio_input.set_paused(False)
                        if "pause" in panic_by_pause:
                            panic_by_pause.discard("pause")
                            scheduler.release_panic()
                        assembly.set_event_input_enabled(True)
                        runtime_paused = False
                        print("[面板] 已恢复")
                if "panic_mute" in data:
                    if bool(data["panic_mute"]):
                        scheduler.panic_mute()
                        print("[面板] 紧急叫停")
                    else:
                        scheduler.release_panic()
                        print("[面板] 恢复说话")

                def announce(line: str) -> None:
                    print(f"[面板] {line}")
                    if hub is not None:
                        hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "system", "text": line})

                # Both write shapes (config rows, speak matrix) become (path,
                # value) pairs through the one validated runtime channel:
                # apply -> hook -> persist -> roll back on failure.
                edits: list[tuple[str, Any]] = []
                config = data.get("config")
                if isinstance(config, dict) and isinstance(config.get("path"), str):
                    edits.append((config["path"], config.get("value")))
                speak = data.get("speak")
                if isinstance(speak, dict):
                    paths = speak_paths(settings)
                    for name, value in speak.items():
                        if isinstance(name, str) and name in paths:
                            edits.append((paths[name], value))
                        else:
                            announce(f"未知直播事件开关 {name}，忽略")
                audio = data.get("audio")
                if isinstance(audio, dict):
                    # The system page's three audio controls arrive on this
                    # shape; dropping them silently is how the mic switch
                    # became a placebo once already.
                    audio_paths = {
                        "input_enabled": "audio.input_enabled",
                        "output_enabled": "audio.output_enabled",
                        "noise_sensitivity": "audio.noise_sensitivity",
                    }
                    for name, value in audio.items():
                        if isinstance(name, str) and name in audio_paths:
                            edits.append((audio_paths[name], value))
                        else:
                            announce(f"未知音频配置 {name}，忽略")
                room = data.get("room")
                if isinstance(room, dict):
                    action = room.get("action")
                    try:
                        if action == "disconnect":
                            await room_source.disconnect()
                            announce("已断开直播间")
                        elif action == "connect":
                            wanted = int(room.get("room_id") or 0)
                            if wanted <= 0:
                                announce("直播间号要是正整数")
                            elif wanted == settings.room.room_id and not room_source.status().get(
                                "connected"
                            ):
                                # Same id, not connected: a plain retry, no edit.
                                await room_source.connect(wanted)
                                announce(f"已连接直播间 {wanted}")
                            else:
                                edits.append(("room.room_id", wanted))
                        elif action == "save_info":
                            if "streamer_name" in room:
                                edits.append(
                                    ("persona.streamer_name", str(room.get("streamer_name") or ""))
                                )
                            if "stream_intro" in room:
                                edits.append(
                                    ("room.stream_intro", str(room.get("stream_intro") or ""))
                                )
                    except ConnectionError as exc:  # RoomConnectionError included
                        announce(f"直播间操作没成：{str(exc)[:120]}")
                assistant = data.get("assistant")
                if isinstance(assistant, dict):
                    await handle_assistant(assistant, announce)
                for path, value in edits:
                    try:
                        announce(await apply_runtime_edit(path, value))
                    except ConfigEditError as exc:
                        announce(str(exc))
                if hub is not None:
                    hub.broadcast(ServerEvent.PANEL_STATE, panel_state())

            async def handle_assistant(
                assistant: dict[str, Any], announce: Callable[[str], None]
            ) -> None:
                from bilisama.ui import assistants as assistants_mod

                action = assistant.get("action")
                if action == "select":
                    wanted = str(assistant.get("id") or "")
                    if wanted == settings.persona.id:
                        return
                    try:
                        # One batch: the id, and the display name reset so the
                        # new persona does not keep introducing itself by the
                        # old one's name (ui_meta warned about exactly this).
                        announce(await apply_runtime_edit("persona.id", wanted))
                        announce(await apply_runtime_edit("persona.display_name", ""))
                    except ConfigEditError as exc:
                        announce(str(exc))
                    return
                if action == "save":
                    persona_id = str(assistant.get("id") or "")
                    anchor = str(assistant.get("anchor") or "")
                    text = str(assistant.get("text") or "")
                    try:
                        if anchor not in ("identity", "personality"):
                            raise ValueError(f"没有「{anchor}」这个人设文件")
                        assistants_mod.save_anchor(
                            settings,
                            config_path.parent,
                            persona_id,
                            anchor,  # type: ignore[arg-type]
                            text,
                        )
                    except ValueError as exc:
                        announce(str(exc))
                        return
                    if persona_id == settings.persona.id:
                        # The active persona changed under the session: rebuild
                        # the prompt stack so the edit is heard this stream.
                        await refresh_persona_settings()
                    announce(f"人设「{persona_id}」的 {anchor} 已保存并生效")

            async def on_app_quit(_data: dict[str, Any]) -> None:
                """The right-click exit: acknowledge first, tear down after.

                Broadcast app.exiting, give the local WS pumps one delivery
                window, then trip the same stop event Ctrl-C uses — every
                window closes itself while distillation and closing run on
                with no UI to wait for.
                """
                if hub is not None:
                    hub.broadcast(ServerEvent.APP_EXITING, {})
                await asyncio.sleep(0.05)
                print("[面板] 收到退出请求，开始收尾")
                stop.set()

            # A mock run must be the ONLY room feeding the assembly. The mock
            # controller connects its own source, so a previously connected
            # real room would double every event stream; park it for the run
            # and bring it back when the run ends. If the mock's room dies
            # mid-run the restore waits for the operator's stop/re-check —
            # silently reattaching a room under an error banner would be worse.
            live_mock_parked_room = 0

            async def _park_room_for_mock() -> None:
                nonlocal live_mock_parked_room
                status = room_source.status()
                if bool(status.get("connected")) and not live_mock_parked_room:
                    live_mock_parked_room = int(status.get("room_id") or 0)
                    await room_source.disconnect()
                    print("[直播Mock] 已暂挂真实直播间连接，Mock 结束后自动恢复")

            async def _restore_room_after_mock() -> None:
                nonlocal live_mock_parked_room
                room_id, live_mock_parked_room = live_mock_parked_room, 0
                if room_id > 0:
                    try:
                        await room_source.connect(room_id)
                        print(f"[直播Mock] 已恢复真实直播间 {room_id}")
                    except ConnectionError as exc:
                        print(f"[直播Mock] 直播间 {room_id} 没能恢复：{str(exc)[:120]}")

            async def on_live_mock_check(data: dict[str, Any]) -> None:
                capture = data.get("capture")
                await live_mock.check(
                    room_id=int(data.get("room_id") or 0),
                    capture=capture if isinstance(capture, dict) else {},
                )

            async def on_live_mock_start(_data: dict[str, Any]) -> None:
                await _park_room_for_mock()
                await live_mock.start()

            async def on_live_mock_stop(_data: dict[str, Any]) -> None:
                await live_mock.stop()
                await _restore_room_after_mock()

            async def on_live_mock_capture_stop(_data: dict[str, Any]) -> None:
                await live_mock.capture_stopped()
                await _restore_room_after_mock()

            async def on_test_run(data: dict[str, Any]) -> None:
                case_id = str(data.get("case_id") or "")
                try:
                    await test_runner.start(case_id)
                except ValueError as exc:
                    test_notify({"status": "error", "case_id": case_id, "text": str(exc)})

            async def on_test_stop(_data: dict[str, Any]) -> None:
                await test_runner.stop()

            async def on_console_line(data: dict[str, Any]) -> None:
                text = data.get("text")
                if isinstance(text, str):
                    await handle_line(text, as_live=bool(data.get("as_live")))

            def hello() -> dict[str, Any]:
                return {
                    "protocol": 1,
                    # Where the record outlives this window: the panel keeps
                    # 500 lines and dies with the tab.
                    "log_path": str(log_path) if file_tee else "",
                    "persona": {
                        "id": settings.persona.id,
                        "name": settings.persona.display_name or settings.persona.id,
                    },
                    "provider": provider.value,
                    "room_connected": bool(room_source.status().get("connected")),
                    "avatar": {
                        "renderer": settings.avatar.renderer,
                        "model_id": settings.avatar.model_id,
                    },
                    "panel": panel_state(),
                    "tests": test_catalog.public(),
                    "test_state": test_runner.state(),
                    "live_mock": live_mock.state(),
                }

            try:
                ui_sock = bind_ui_socket(settings.runtime.ui_port)
            except OSError as exc:
                # The UI is a passenger; the voice loop must survive a taken port.
                print(
                    f"[界面] 端口 {settings.runtime.ui_port} 绑不上（{exc}），本场没有界面；"
                    "改 [runtime] ui_port 或空出端口。"
                )
                # Everything downstream reads "no panel" as a choice (--no-ui).
                # This is the one line that says it was not one — and it is why
                # the terminal switch /mute has to exist (backlog #47).
                log.warning(
                    "dev_talk.ui_port_unavailable",
                    port=settings.runtime.ui_port,
                    error_text=str(exc),
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
                        ClientEvent.PLAYBACK_STARTED: on_playback_started,
                        ClientEvent.PLAYBACK_ENDED: on_playback_ended,
                        ClientEvent.PLAYBACK_CANCELLED: on_playback_cancelled,
                        ClientEvent.APP_QUIT: on_app_quit,
                        ClientEvent.TEST_RUN: on_test_run,
                        ClientEvent.TEST_STOP: on_test_stop,
                        ClientEvent.LIVE_MOCK_CHECK: on_live_mock_check,
                        ClientEvent.LIVE_MOCK_START: on_live_mock_start,
                        ClientEvent.LIVE_MOCK_STOP: on_live_mock_stop,
                        ClientEvent.LIVE_MOCK_CAPTURE_STOP: on_live_mock_capture_stop,
                    },
                    broker=broker,
                    on_audio=uplink,
                    on_mock_audio=live_mock.push_audio,
                    hello=hello,
                    # <data home>/bilisama/skins — user-imported packs, shadowing
                    # the packaged ones. Endpoint file and skins share the roof.
                    user_skins_root=default_endpoint_path().parent.parent / "skins",
                )
                ui_server = UiServer(app, ui_sock)
                ui_server.start()
                # Port only. The URL carries the session token, and a token in
                # a log file is the token in every bug report attached to it.
                log.info("dev_talk.ui_started", port=ui_port)
                endpoint_file = default_endpoint_path()
                try:
                    write_endpoint_file(endpoint_file, url=ui_url, pid=os.getpid())
                except OSError as exc:
                    print(f"[界面] 端点文件写不了（{exc}）；壳这场要用 BILISAMA_UI_URL={ui_url}")
                    # The shell finds the panel through this file and nothing
                    # else, so this is the whole answer to 「壳一直停在等待卡片」.
                    log.warning(
                        "dev_talk.ui_endpoint_file_unwritable",
                        path=str(endpoint_file),
                        error_text=str(exc),
                    )
                    endpoint_file = None
                hub.broadcast(ServerEvent.PANEL_STATE, panel_state())  # seed the sticky state
                if args.open:
                    import webbrowser

                    webbrowser.open(ui_url)

        # The desktop shell comes up on its own whenever it is installed — this
        # is the desktop-pet preview, and having to remember a flag for the pet
        # was a surprise every single time. Best-effort, never supervision:
        # stage 7 inverts the relationship (Electron launches P2), the shell
        # finds the endpoint file by itself, and a missing or broken electron
        # never touches the voice loop. --no-pet opts out.
        if not args.no_pet and ui_server is not None:
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
                    log.warning("dev_talk.pet_spawn_failed", error_text=str(exc))
                else:
                    print("[桌宠] 壳已拉起（关掉 dev-talk 会一并带走；不想要加 --no-pet）")
                    log.info("dev_talk.pet_started", pid=pet_proc.pid)
            else:
                print(
                    f"[桌宠] 桌面悬浮窗没装，这场只有浏览器界面。装一次就好："
                    f"cd {pet_dir} && npm install"
                )
                # Not a failure — but 「桌宠没出来」 is asked often enough that
                # the answer belongs in the log rather than only in a line that
                # scrolled past twenty minutes ago.
                log.info("dev_talk.pet_not_installed", pet_dir=str(pet_dir))

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
            print(
                "提示：装上 prompt_toolkit 后，打弹幕不再被输出打乱（pip install prompt_toolkit）。"
            )

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
                        print(
                            "\n[退出] 正在收尾（蒸馏、断连）…再按一次 Ctrl-C 强退。",
                            file=sys.stderr,
                        )
                        # Leaving this session tears our SIGINT handler down with
                        # it, and the escalation just promised above would have
                        # nothing to land on. Put it back before returning.
                        _sane_terminal(saved_tty)
                        arm_sigint()
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

        async def report_playback() -> None:
            """Tell the floor whether the audience is still listening.

            This is the producer plan section 4.3 assumes for queued_audio and
            that nothing had ever supplied. Without it the scheduler dispatches
            the next reply the moment the SERVER finishes generating, while the
            speaker is still far from playing the last one out: measured against
            the real endpoint, fourteen back-to-back replies built 106 seconds of
            unplayed audio in 48 seconds of wall clock and never recovered. What
            the listener gets is answers to danmaku from two minutes ago.

            A poll rather than a callback because the buffer drains on
            PortAudio's thread, and hopping threads to announce silence would
            cost more than looking. 100 ms is well inside one audio block.
            """
            last = False
            while True:
                if broker is not None and not broker.local_is_live:
                    # A page holds the devices, so PlaybackTally is feeding the
                    # gate and this poll must keep its hands off it. Without
                    # this the handover itself opens the floor: suspending the
                    # speaker takes `busy` from true to false at the exact
                    # moment the page starts playing, and the gate would read
                    # that as her having finished.
                    last = False
                    await asyncio.sleep(0.1)
                    continue
                busy = speaker.busy
                if busy != last:
                    floor.on_playback(busy)
                    last = busy
                    if not busy:
                        # State gates release on events, and playback finishing
                        # is not one the link ever sends.
                        scheduler.notify()
                await asyncio.sleep(0.1)

        async def drain_controls() -> None:
            while True:
                clear = await scheduler.controls.get()
                if speaker is not None:
                    speaker.flush()
                if broker is not None:
                    # The page's half of the same flush. Its audio sits in a
                    # queue and a schedule this call cannot reach otherwise, so
                    # without it the voice runs on past the text.
                    broker.flush()
                print(f"[打断] playback.clear（{clear.reason}）")
                # The scheduler decides to interrupt; this is the receipt that
                # both speakers were actually emptied. The pair is what tells a
                # barge-in that worked from one that only got as far as the
                # decision — the two ends are different processes' buffers.
                log.info("dev_talk.playback_cleared", reason=clear.reason, page=broker is not None)
                if hub is not None:
                    # The bubble shatters on this; the queue stays single-consumer
                    # here and the hub only gets a copy.
                    hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": clear.reason})

        if use_prompt:
            from prompt_toolkit.patch_stdout import patch_stdout

            # While the prompt is live, print() and the log stream both write
            # through a proxy that repaints the input line beneath them. Logging
            # re-targets onto the patched stdout for the window; the shutdown
            # chain restores it.
            console_patch.enter_context(patch_stdout(raw=True))
            relog()

        print(
            # connect_url, not args.url: on DashScope the dial goes to the
            # wss endpoint while args.url still holds the s2s default, and a
            # banner naming a host nobody dialled sends debugging the wrong way.
            f"已连接 {provider.value}（{connect_url.split('?')[0]}），director 模式：\n"
            f"  人设 {settings.persona.id}（persona list 可看全部）  "
            f"话痨度 {settings.interaction.chattiness.value}"
            f"（冷场阈值随房间活跃度浮动，当前 {event_pacer.snapshot().proactive_idle_s:.0f}s）\n"
            f"  生长层 relationship={settings.persona.growth.relationship.value} "
            f"voice={settings.persona.growth.voice.value}\n"
            "  说话即聊；终端打字＝模拟弹幕；Ctrl-C 下播（触发蒸馏后退出）。"
        )
        # The banner, structured. Every field here has been guessed wrong by
        # somebody reading a bug report: which provider, whose address won
        # (endpoint_source is the layer — flag, config or registry), which
        # persona, which room, and whether a credential was found for it. The
        # query is stripped for the same reason the banner strips it — an
        # address can carry a credential — and SESSDATA becomes a yes/no.
        log.info(
            "dev_talk.session_started",
            provider=provider.value,
            endpoint=connect_url.split("?")[0],
            endpoint_source=endpoint.source,
            llm_model=endpoint.model,
            persona=settings.persona.id,
            chattiness=settings.interaction.chattiness.value,
            room_id=room_id,
            room_credentialed=room_credentialed,
            console="prompt" if use_prompt else "plain",
        )
        if ui_url:
            print(
                f"  界面 {ui_url}\n  （浏览器打开看桌宠和面板；desktop/preview 的壳会自己找到它）"
            )
        if not args.mute_while_speaking:
            print("提示：外放会让 AI 听到自己的声音。戴耳机，或加 --mute-while-speaking。")

        def start_mic() -> asyncio.Task[None]:
            # Records itself, because the broker swaps this task out and back
            # while the list below keeps whichever one it was handed first.
            # The shutdown chain cancels both, so a restarted microphone cannot
            # outlive the session.
            task = _watch(
                asyncio.create_task(
                    _pump_mic(audio_input, args.input_device, speaker, args.mute_while_speaking),
                    name="director:mic",
                )
            )
            local_pair.mic = task
            return task

        local_pair.start_mic = start_mic

        await assembly.refresh_context()
        # _watch on every one: this list is started and then left alone while
        # the main flow waits on `stop`, so without it a task that raises just
        # vanishes. See _watch.
        tasks = [
            asyncio.create_task(scheduler.run(), name="director:scheduler"),
            asyncio.create_task(proactive.run(), name="director:proactive"),
            asyncio.create_task(
                assembly.run(
                    [console, tests_source] + ([bili_source] if bili_source is not None else [])
                ),
                name="director:assembly",
            ),
            local_pair.start_mic(),
            asyncio.create_task(
                # stream_text off under the prompt: see _consume_events.
                _consume_events(
                    speech.events(),
                    speaker,
                    None,
                    stream_text=not use_prompt,
                    link_health=link_health,
                    # Completed streamer turns are topic material — never a
                    # reset of the live-event dead-air clock (proactive.py).
                    on_transcript=on_streamer_transcript,
                    output_enabled=output_enabled,
                ),
                name="director:play",
            ),
            asyncio.create_task(drain_controls(), name="director:controls"),
            asyncio.create_task(_watch_default_output(speaker), name="director:default-output"),
            asyncio.create_task(stdin_pump(), name="director:stdin"),
            asyncio.create_task(lag_monitor.run(), name="director:loop-lag"),
            asyncio.create_task(report_playback(), name="director:playback"),
            # report_playback stays: it is the producer for the local half.
            # While a page holds the devices the speaker has no stream, `busy`
            # is False, and the gate is fed by PlaybackTally instead — the two
            # producers never overlap because the devices only have one owner.
        ]
        if bili_source is not None:
            # The 「登录态」 line above went out before the connection existed.
            # This one is allowed to be right (see _watch_credential).
            tasks.append(
                asyncio.create_task(_watch_credential(bili_source), name="director:credential")
            )
        for task in tasks:
            _watch(task)
        if hub is not None:
            live_hub = hub

            def read_signals() -> VoiceSignals:
                status = scheduler.status()
                return VoiceSignals(
                    streamer_speaking=floor.streamer_speaking,
                    dispatching=bool(status.get("dispatching")),
                    active=status.get("active_source") is not None,
                    implicit=floor.implicit_active,
                    # Both ends: the page is the one playing whenever it holds
                    # the devices, and the local speaker reads False all through
                    # her reply in that case. See _audio_busy.
                    audio_busy=_audio_busy(speaker, tally),
                )

            live_broker = broker
            assert live_broker is not None  # built in this branch, two lines up

            async def ui_feed() -> None:
                """The fanout's third view; the other two are the scheduler and
                director:play.

                Two wires leave here. Words and state go through link_frames
                onto the control socket. PCM goes to whoever holds the devices
                — nowhere at all when that is nobody, which is exactly right:
                the local speaker is already playing it in that case, and
                sending it twice is how you get an echo of your own making.
                """
                async for ev in speech.events():
                    if isinstance(ev, link.ReplyAudioDelta):
                        if not ev.handle.stale:
                            # Only what actually goes out: a stale chunk is
                            # dropped below and never reaches a speaker, so
                            # counting it would invent an echo of audio nobody
                            # played.
                            echo.note_played(ev.pcm)
                        if ev.handle.stale:
                            # Cancelling marks the handle, but audio already
                            # decoded and sitting in this fanout view keeps
                            # arriving behind it. Measured against a live
                            # interruption: 1.08 more seconds after the queue
                            # had been emptied, which is a whole second of her
                            # talking over the streamer.
                            continue
                        if output_enabled():
                            live_broker.play(ev.pcm)
                        continue
                    if isinstance(ev, link.SpeechStarted):
                        # The same instant the local speaker flushes on
                        # (_consume_events). Waiting for the scheduler's
                        # playback.clear to come round would add a whole
                        # dispatch hop to how long she talks over the streamer.
                        live_broker.flush()
                    reply_context: dict[str, Any] | None = None
                    if isinstance(ev, link.ReplyTextDelta | link.ReplyDone):
                        intent = scheduler.reply_intent(ev.handle.handle_id)
                        if intent is not None:
                            reference = (
                                live_event_payload(intent.event)
                                if intent.event is not None
                                else {"kind": intent.source}
                            )
                            reply_context = {"source": intent.source, "reference": reference}
                    for name, data in link_frames(ev, reply_context=reply_context):
                        live_hub.broadcast(name, data)

            tasks += [
                _watch(asyncio.create_task(ui_feed(), name="director:ui-feed")),
                _watch(asyncio.create_task(live_hub.run(read_signals), name="director:ui-state")),
                _watch(asyncio.create_task(ui_audio_level(), name="director:ui-level")),
            ]
        await stop.wait()
    finally:
        # First of all, and before anything is cancelled: from here on nobody
        # gets the devices back. The page's release lands during the gather
        # below, and a microphone started there is in no list and is cancelled
        # by nothing.
        local_pair.stop_reviving()
        # local_pair.mic separately: the broker may have replaced the microphone
        # task after this list was built, and the replacement is not in it.
        current_mic = local_pair.mic
        if current_mic is not None and current_mic not in tasks:
            tasks.append(current_mic)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # The prompt died with its task; unpatch stdout before the shutdown
        # chain prints, and point logging back at stderr.
        console_patch.close()
        # Two things the prompt took with it, both needed for the rest of this
        # chain to stay interruptible: the tty's cooked mode (raw mode turns
        # Ctrl-C into an echoed ^C instead of a signal) and our SIGINT handler.
        _sane_terminal(saved_tty)
        arm_sigint()
        if use_prompt:
            use_prompt = False  # the patched stdout is gone; relog to stderr
            relog()
        # The UI goes first: aclose() chases the browsers off their sockets so
        # uvicorn's graceful stop is not stuck waiting on an open WebSocket.
        if ui_server is not None:
            try:
                if hub is not None:
                    await hub.aclose()
                with contextlib.suppress(Exception):
                    await live_mock.aclose()
                await ui_server.stop()
            except Exception as exc:
                print(f"[收尾] 界面服务器没关上：{exc}", file=sys.stderr)
                # log.exception, not log.warning: all seven teardown steps
                # below used to end at that print, which keeps the sentence
                # and throws the traceback away. A shutdown that
                # leaves a socket or the database open is exactly the failure
                # nobody can reproduce afterwards, and stderr scrolled past.
                log.exception("dev_talk.ui_close_failed", error_text=str(exc))
        if endpoint_file is not None:
            # Only if it is still OURS. Two --director sessions share this path
            # (ui_port=0 gives each its own port but one endpoint file), the
            # later one overwrites it, and deleting another live session's
            # endpoint sends its shell back to the waiting card for good.
            with contextlib.suppress(OSError, ValueError):
                published = json.loads(endpoint_file.read_text(encoding="utf-8"))
                if published.get("pid") == os.getpid():
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
            # Whether the session left anything behind. ran=False is the normal
            # answer for a short test run and the alarming one after four hours
            # of stream, and only the reason tells them apart.
            log.info("dev_talk.distill_done", ran=report.ran, reason=report.reason)
        except Exception as exc:
            print(f"[蒸馏] 失败：{exc}", file=sys.stderr)
            log.exception("dev_talk.distill_failed", error_text=str(exc))
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
            log.exception("dev_talk.memory_finalize_failed", error_text=str(exc))
        try:
            store.close()
        except Exception as exc:
            print(f"[收尾] 记忆库没关上：{exc}", file=sys.stderr)
            log.exception("dev_talk.memory_close_failed", error_text=str(exc))
        if side is not None:
            try:
                await side.aclose()
            except Exception as exc:
                print(f"[收尾] 侧路连接没关上：{exc}", file=sys.stderr)
                log.exception("dev_talk.side_close_failed", error_text=str(exc))
        try:
            await speech.aclose()
        except Exception as exc:
            print(f"[收尾] 语音连接没关上：{exc}", file=sys.stderr)
            log.exception("dev_talk.speech_close_failed", error_text=str(exc))
        try:
            # Off-loop: the PortAudio release blocks (see _close_audio_stream),
            # and left open it would stall interpreter teardown after 再见.
            await asyncio.to_thread(speaker.close)
        except Exception as exc:
            print(f"[收尾] 扬声器没关上：{exc}", file=sys.stderr)
            log.exception("dev_talk.speaker_close_failed", error_text=str(exc))
        print("再见。")
    return 0


async def run(args: argparse.Namespace) -> int:
    from bilisama.cli import DEFAULT_CONFIG
    from bilisama.config import load

    # The bare link is the only way to test a hosted endpoint with your own
    # voice (see the module docstring), so it reads the config too — otherwise
    # "config decides where we dial" would only be half true. strict=False:
    # this path must survive a machine with no config file at all, which is
    # exactly the packaged case.
    settings = load(args.config or DEFAULT_CONFIG, strict=False)
    endpoint = resolve_endpoint(
        settings, provider=args.provider, url=args.url, model=args.model, env=os.environ
    )
    provider = endpoint.provider
    profile = profile_for(provider)
    _refuse_if_wire_mode_cannot_drive(provider, profile)
    headers: dict[str, str] = {}
    url = endpoint.url
    if provider is ProviderName.DASHSCOPE:
        # The factory's resolver, not a second copy of it. This block used to
        # restate the rule — config reference first, path.sh second — along
        # with its own refusal text, which is two places for one answer to
        # drift.
        key = hosted_key(settings, provider, os.environ)
        url = with_model(url, endpoint.model, explicit=bool(args.model))
        headers = {"Authorization": f"Bearer {key}"}
    print(f"[语音] {provider.value} @ {_endpoint_line(url)}（来自{endpoint.source}）")

    client = RealtimeClient(url, caps=profile.caps, codec=codec_for(provider), headers=headers)
    await _connect_or_exit(client, provider, url)
    print(f"已连接 {provider.value}（{url.split("?")[0]}），说话即可，Ctrl-C 退出。")
    if args.wav is None and not args.mute_while_speaking:
        print("提示：外放会让 AI 听到自己的声音。戴耳机，或加 --mute-while-speaking。")
    speaker: _Speaker | None = None
    try:
        session_frame = _session_frame(provider, args.voice)
        if session_frame is not None:
            await client.send_command(session_frame)
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
        watcher = (
            asyncio.create_task(_watch_default_output(speaker), name="wire:default-output")
            if speaker is not None
            else None
        )
        done, pending = await asyncio.wait({pump, consume}, return_when=asyncio.FIRST_COMPLETED)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # surface pump/consume failures instead of swallowing
        return 0
    finally:
        # One try per resource, same rule as director mode's teardown chain:
        # a raise from the socket goodbye must not skip the audio release.
        #
        # No log.exception beside these two prints, unlike run_director's copy
        # of the same failures. Wire mode never calls logging setup() at all,
        # so there is no file and no panel to write to — the record would go
        # to logging.lastResort, which prints the event name and a raw
        # traceback onto the terminal the streamer is reading. All cost, no
        # reader. Give this mode a log destination and the calls belong here.
        try:
            await client.aclose()
        except Exception as exc:
            print(f"[收尾] 语音连接没关上：{exc}", file=sys.stderr)
        if speaker is not None:
            try:
                await asyncio.to_thread(speaker.close)
            except Exception as exc:
                print(f"[收尾] 扬声器没关上：{exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """Every dev-talk flag, in one callable.

    Split out of `main` so a test can drive `run_director` through the REAL
    flags instead of a hand-built Namespace: the hand-built one silently
    diverges the day a flag is added, and this file's one end-to-end test is
    exactly where that would go unnoticed.
    """
    parser = argparse.ArgumentParser(
        prog="bilisama dev-talk", description="拿真人声音测我们自己的语音链路"
    )
    # default=None on all three, deliberately: a flag that always carries a
    # value can never lose to the config, which is how [speech] ended up
    # decorative. None is the sentinel for "not given" — the same trick
    # --voice already uses with "". Not argparse.SUPPRESS: that removes the
    # attribute entirely and every hand-built Namespace in the tests breaks.
    parser.add_argument(
        # From the enum, not a hand-written pair. The list was a fourth place
        # that had to learn about a new provider, and the one place a streamer
        # meets first: argparse refuses an unlisted name before any of the
        # factory's messages get a chance to say what is really missing.
        "--provider",
        choices=[p.value for p in ProviderName],
        default=None,
        help="不给就读配置",
    )
    parser.add_argument("--url", default=None, help="语音服务地址；不给就读配置")
    parser.add_argument("--model", default=None, help="托管服务的模型名；不给就读配置")
    parser.add_argument(
        "--voice",
        default="",
        help="换音色，覆盖 [speech.dashscope] voice。留空用服务端默认（偏尖）；"
        "名字写错时服务端会把可用清单报回来",
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
        "--no-pet",
        action="store_true",
        help="这场不要桌面悬浮窗（装了壳就默认拉起，只想要浏览器界面时加它）",
    )
    parser.add_argument(
        # Kept so the muscle memory and the older runbook lines still work; the
        # shell is the default now, so it has nothing left to turn on.
        "--pet",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skin",
        default=None,
        help="本次运行的形象：皮肤包目录名（如 kirby），或 tofu 用内置豆腐机器人；不改配置文件",
    )
    return parser


def _refuse_if_wire_mode_cannot_drive(provider: ProviderName, profile: ProviderProfile) -> None:
    """Say no in Chinese, before the banner, to a backend this path cannot dial.

    Bare-link mode drives RealtimeClient directly: one OpenAI dialect, one
    uplink rate, no adapter in between. Two of the four providers do not fit
    through it, and until now argparse was what said so — `--provider` listed
    exactly the two that worked. Widening it to the whole enum (so it stops
    being a fourth place that has to learn a new provider's name) turned that
    clean refusal into a raw ValueError out of `codec_for`, printed after the
    banner, past a main() that only catches KeyboardInterrupt.

    Both conditions come from the registry rather than a list, so the fifth
    provider is covered on the day it is added.

    Raises:
        SystemExit: This provider needs `--director`.
    """
    if profile.codec is None:
        raise SystemExit(
            f"{provider.value} 不说 OpenAI Realtime 方言，裸链路模式驱动不了它。"
            "加 --director 走完整装配。"
        )
    if profile.uplink_rate != _INPUT_RATE:
        raise SystemExit(
            f"{provider.value} 的上行要 {profile.uplink_rate} Hz，而裸链路模式的麦克风固定 "
            f"{_INPUT_RATE} Hz——重采样挂在托管适配器上，这条路上没有。"
            "加 --director 走完整装配。"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.director:
        try:
            return asyncio.run(run_director(args))
        except KeyboardInterrupt:
            # The second Ctrl-C exits inside on_sigint; this catches a Ctrl-C
            # arriving before that handler is installed.
            print("\n强退。")
            return 130
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n再见。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
