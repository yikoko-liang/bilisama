"""Contract tests against the real Volcengine dialogue endpoint.

Run for the first time on 2026-08-27, all six green. Before that every
assertion here came from the vendor's documentation plus one worked example
frame, and the file said so — that distinction is why it exists: the
alternative is an adapter whose assumptions live only in comments, which is
how a fake kinder than the server certified a broken client twice already.

Nothing here opens an audio device or starts anything. The credential comes
from the environment (path.sh supplies it locally) and every test skips with a
plain reason when it is absent, so the gate stays honest on a machine that has
none. The one test that needs the server's own VAD to fire feeds a WAV
synthesised by `say` — a real speech waveform that never goes near a
microphone or a speaker.

What the six answered, and why each assertion is worded to go red if the
endpoint changes its mind:

1. **The client frame layout is right.** The handshake completes, which it
   could not if an optional field were in the wrong place — the published
   sample is a server frame, so nothing else covers the uplink shape.
2. **A session-level event without a session id IS refused.** So the framing
   rule in volcano_wire is the server's rule, not just ours.
3. **`pcm_s16le` is honoured.** The default is Ogg Opus, and an Ogg stream fed
   to a PCM player is not an error — it is noise.
4. **TTSEnded does follow ChatEnded**, so settling the reply on the audio
   ending (rather than on the model finishing) holds the slot for exactly as
   long as she is still talking.
5. **Barge-in arrives as ASRInfo and nothing else**, and no un-stale audio
   follows it. There is no response.cancel on this protocol; what makes the
   interruption sound clean is the handle going stale.
6. **UpdateConfig takes effect.** This was the one most likely to fail
   silently: the assembly pushes the persona AFTER connect (dev_talk.py:1330),
   so on this provider that is the only channel it has, and an event accepted
   and ignored looks exactly like one that worked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import subprocess
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets

from bilisama.config.enums import ProviderName
from bilisama.config.schema import VolcanoConfig
from bilisama.realtime import link
from bilisama.realtime.providers import PROFILES
from bilisama.realtime.providers.volcano import VolcanoLink
from bilisama.realtime.providers.volcano_wire import ServerEvent, decode

pytestmark = pytest.mark.provider_a

_SILENCE_20MS = b"\x00" * 640  # 16 kHz mono s16le


def _credentials() -> tuple[str, str]:
    """The address and an API Key.

    One key, not the old App ID / Access Token pair: the vendor's API Key page
    says 「在任意接口中，填入 header 即可，不用填写 appid」, and a live probe on
    2026-08-27 confirmed it — x-api-key plus the resource headers completes the
    handshake, while the same value in the X-Api-Access-Key slot draws 401.
    """
    key = os.environ.get("volcano_api_key", "") or os.environ.get(  # noqa: SIM112
        "volcano_access_key", ""  # noqa: SIM112
    )
    if not key:
        pytest.skip(
            "没有火山 API Key（volcano_api_key）。控制台 > API Key 管理拿一个，"
            "export 到 path.sh 里"
        )
    override = os.environ.get("volcano_url", "")  # noqa: SIM112
    return override or PROFILES[ProviderName.VOLCANO].default_url, key


def _config(**over: Any) -> VolcanoConfig:
    return VolcanoConfig(**over)


async def _link(**over: Any) -> AsyncIterator[VolcanoLink]:
    url, key = _credentials()
    volcano = VolcanoLink(url, api_key=key, config=_config(**over))
    await volcano.connect()
    try:
        yield volcano
    finally:
        await volcano.aclose()


async def _drain(
    volcano: VolcanoLink, seconds: float, *, until: type | None = None
) -> list[link.LinkEvent]:
    """Everything that arrives inside the window, or up to `until`."""
    got: list[link.LinkEvent] = []
    events = volcano.events()

    async def pump() -> None:
        async for event in events:
            got.append(event)
            if until is not None and isinstance(event, until):
                return

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(pump(), timeout=seconds)
    return got


async def _collect_into(volcano: VolcanoLink, sink: list[link.LinkEvent]) -> None:
    """Append events until cancelled. A module-level helper rather than a
    closure so the list it writes to is an argument, not a captured name."""
    async for event in volcano.events():
        sink.append(event)


async def test_the_handshake_climbs_both_levels() -> None:
    """The floor under everything else: if this fails, read no further.

    It also answers question 1 by construction — the client frames this sends
    are the ones the adapter builds, so a wrong optional-field order shows up
    as a refusal right here rather than as a mystery three events later.
    """
    async for volcano in _link():
        got = await _drain(volcano, 5.0, until=link.LinkUp)
        assert any(isinstance(e, link.LinkUp) for e in got), f"没建起会话：{got}"


async def test_a_session_event_without_a_session_id_is_refused() -> None:
    """Question 2. The adapter always sends one; this checks that the server
    would have complained if it did not, because a server that quietly accepts
    the frame makes a whole class of framing bug invisible.
    """
    url, key = _credentials()
    headers = {
        "x-api-key": key,
        "X-Api-Resource-Id": "volc.speech.dialog",
        "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
    }
    async with websockets.connect(url, additional_headers=headers) as ws:
        # StartConnection, then a StartSession that omits the id entirely.
        await ws.send(
            bytes((0x11, 0x14, 0x10, 0)) + struct.pack(">i", 1) + struct.pack(">I", 2) + b"{}"
        )
        await asyncio.wait_for(ws.recv(), timeout=5.0)
        body = json.dumps({"tts": {}}).encode()
        await ws.send(
            bytes((0x11, 0x14, 0x10, 0))
            + struct.pack(">i", 100)
            + struct.pack(">I", len(body))
            + body
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        frame = decode(raw if isinstance(raw, bytes) else str(raw).encode())
        assert frame.event != ServerEvent.SESSION_STARTED, (
            "服务端收下了没有 session id 的 StartSession——"
            "那 volcano_wire 里那条「会话级事件必带 id」的规则要重做"
        )


async def test_the_downlink_really_comes_back_as_pcm() -> None:
    """Question 3, and the one that decides whether anything is audible.

    The vendor default is Ogg Opus. If the format request is ignored, this
    catches it here rather than as silence coming out of the speakers — an Ogg
    stream fed to a PCM player is not an error, it is noise.
    """
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.request_reply(link.ReplySpec(instructions="用五个字回答：你好吗"))
        got = await _drain(volcano, 25.0, until=link.ReplyDone)
        audio = [e for e in got if isinstance(e, link.ReplyAudioDelta)]
        assert audio, f"一帧音频都没回来：{[type(e).__name__ for e in got]}"
        head = audio[0].pcm[:4]
        assert head != b"OggS", "回来的是 Ogg，不是 PCM——audio_config 那一段没被采纳"
        assert len(audio[0].pcm) % 2 == 0, "s16le 的字节数应当是偶数"


async def test_a_reply_ends_with_tts_ended_after_chat_ended() -> None:
    """Question 4. The adapter settles on TTSEnded, so if that event can go
    missing the single slot is held until the watchdog fires — 25 seconds of
    her not answering anybody.
    """
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.request_reply(link.ReplySpec(instructions="用五个字回答：你好吗"))
        got = await _drain(volcano, 25.0, until=link.ReplyDone)
        done = [e for e in got if isinstance(e, link.ReplyDone)]
        assert done, "没等到回复结束"
        assert done[0].status is link.ReplyStatus.COMPLETED, (
            f"回复以 {done[0].status} 收场——TIMED_OUT 的话说明 TTSEnded 没来，" "settle 的判据要改"
        )
        assert done[0].text, "回复没有文字，ChatResponse 的字段名可能不是 content"


def _speech_wav(tmp_path: Path) -> Path:
    """A real speech waveform, synthesised to a file. No device is opened.

    Silence cannot answer this question — the server's VAD will not fire on it
    — and a test that feeds silence and then reports "inconclusive" is a test
    that never had a chance. Same trick test_hosted_contract uses.
    """
    out = tmp_path / "speech.wav"
    try:
        subprocess.run(
            [
                "say",
                "-v",
                "Tingting",
                "-o",
                str(out),
                "--data-format=LEI16@16000",
                "等一下等一下，我插一句，先别说了",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"造不出语音素材（macOS say 不可用）：{exc}")
    return out


async def test_what_the_server_does_when_talked_over(tmp_path: Path) -> None:
    """Question 5, and the one the product hangs on.

    There is no response.cancel on this protocol, so `cancel()` is local only.
    What matters is whether the server at least TELLS us to stop playing, and
    whether it keeps sending audio afterwards. Both answers are usable; not
    knowing which is true is not.

    Asks for a long reply, lets her get going, then feeds a real speech
    waveform — the same shape as a streamer cutting in.
    """
    wav = _speech_wav(tmp_path)
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        collected: list[link.LinkEvent] = []
        task = asyncio.create_task(_collect_into(volcano, collected))
        await volcano.request_reply(
            link.ReplySpec(instructions="讲一个五百字左右的长故事，从头讲到尾，中间不要停")
        )
        await asyncio.sleep(3.0)  # let her get going
        assert any(isinstance(e, link.ReplyAudioDelta) for e in collected), "她还没开口，插话不算数"

        with wave.open(str(wav)) as w:
            chunk = w.getframerate() // 50  # 20 ms
            while True:
                block = w.readframes(chunk)
                if not block:
                    break
                await volcano.push_audio(block)
                await asyncio.sleep(0.02)
        cut_at = len(collected)
        await asyncio.sleep(6.0)
        task.cancel()

        started = [e for e in collected if isinstance(e, link.SpeechStarted)]
        assert started, (
            "喂了真人语音，服务端一个打断信号都没给——"
            f"收到的是 {sorted({type(e).__name__ for e in collected})}"
        )
        after = collected[cut_at:]
        leaked = [e for e in after if isinstance(e, link.ReplyAudioDelta) and not e.handle.stale]
        assert not leaked, (
            f"打断之后还有 {len(leaked)} 帧不带作废标记的音频——" "两个消费者会把它继续播出去"
        )


async def test_a_persona_pushed_after_connect_actually_takes_effect() -> None:
    """Question 6, and the one most likely to fail silently.

    Production never puts the persona in StartSession: the assembly connects
    first and pushes context afterwards, so UpdateConfig is the only channel
    that path has. An event that is accepted and ignored looks exactly like
    one that worked.

    Asks her to answer with a word only the persona supplies, so the check is
    about the persona reaching her rather than about instruction-following in
    general.
    """
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.set_context("你叫米娅。有人问你叫什么，你只回答「米娅」两个字。")
        await asyncio.sleep(1.0)
        await volcano.request_reply(link.ReplySpec(instructions="你叫什么？"))
        got = await _drain(volcano, 25.0, until=link.ReplyDone)
        said = "".join(e.text for e in got if isinstance(e, link.ReplyTextDelta))
        assert "米娅" in said, (
            f"人设没生效，她说的是「{said}」——" "UpdateConfig 被收下然后忽略了，人设得改走别的路"
        )
