"""Contract tests against the real Volcengine dialogue endpoint.

Run for the first time on 2026-08-27; ten green as of 2026-08-28. Before that every
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

Two more were added on 2026-08-28, after a report that the persona 「好像没传
进去」. It had been transmitted and was working — a session with it describes
herself as a co-host and admits what she cannot read, one without it lectures
and denies knowing the streamer. Only the NAME was wrong, and the two tests
below are the shape of what that took to find:

7. **She answers to the name the persona gives her.** dialog.bot_name defaults
   to 豆包 and beats a 557-character structured persona; a 31-character one
   saying the same thing wins on its own, which is why small tests missed it.
8. **A voice from the wrong generation fails loudly.** SC2.0 with a catalogue
   voice answers ClientError:InvalidSpeaker on a frame with no event number,
   which the adapter used to drop into its ignore branch — the streamer got
   silence while the server had been explaining itself the whole time.
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


async def test_client_interrupt_really_stops_the_server() -> None:
    """The one that turns a local-only cancel into a real one.

    The docs qualify ClientInterrupt with 「在麦克风按键输入模式下即
    push_to_talk 模式」, which reads like a restriction. It is not: this runs in
    plain server_vad and the audio stops. Without the frame the model keeps
    generating and the tokens are spent regardless of what the audience hears.
    """
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        collected: list[link.LinkEvent] = []
        task = asyncio.create_task(_collect_into(volcano, collected))
        handle = await volcano.request_reply(
            link.ReplySpec(instructions="讲一个五百字左右的长故事，从头讲到尾，中间不要停")
        )
        await asyncio.sleep(5.0)
        before = sum(1 for e in collected if isinstance(e, link.ReplyAudioDelta))
        assert before > 10, f"她还没说开，打断了也证明不了什么（{before} 帧）"

        await volcano.cancel(handle)
        cut = len(collected)
        await asyncio.sleep(6.0)
        task.cancel()

        after = sum(1 for e in collected[cut:] if isinstance(e, link.ReplyAudioDelta))
        assert after <= 5, (
            f"打断之后还收到 {after} 帧音频（打断前 {before} 帧）——"
            "ClientInterrupt 没让服务端停下来"
        )


async def test_a_new_connection_resumes_the_conversation_by_dialog_id() -> None:
    """What makes a reconnect resume rather than restart.

    Sets a passphrase on one connection and asks for it back on a brand new
    one. Without the dialog_id she has no idea; with it the server reloads the
    last twenty rounds, which is the difference between a dropped socket
    costing a beat and costing the whole stream's memory.
    """
    url, key = _credentials()
    first = VolcanoLink(url, api_key=key, config=_config())
    await first.connect()
    try:
        await _drain(first, 5.0, until=link.LinkUp)
        await first.request_reply(
            link.ReplySpec(instructions="记住一个暗号：紫色兔子。回一个「好」字就行。")
        )
        await _drain(first, 25.0, until=link.ReplyDone)
        dialog_id = first.dialog_id
        assert dialog_id, "SessionStarted 没给 dialog_id，接续就无从谈起"
    finally:
        await first.aclose()

    second = VolcanoLink(url, api_key=key, config=_config(), dialog_id=dialog_id)
    await second.connect()
    try:
        await _drain(second, 5.0, until=link.LinkUp)
        await second.request_reply(link.ReplySpec(instructions="刚才那个暗号是什么？"))
        got = await _drain(second, 25.0, until=link.ReplyDone)
        said = "".join(e.text for e in got if isinstance(e, link.ReplyTextDelta))
        assert "紫" in said or "兔" in said, f"新连接没接上上一场，她说的是「{said}」"
    finally:
        await second.aclose()


async def test_she_answers_to_the_name_the_persona_gives_her() -> None:
    """The one that catches 「sent but not effective」, which is the failure
    mode this whole file exists for.

    The persona is delivered — a session with it describes herself as a
    co-host and admits what she cannot read, a session without it lectures and
    says she has nothing to do with the streamer. But the NAME comes from
    dialog.bot_name, which defaults to 豆包, and against a 557-character
    structured persona that default wins: three runs out of three answered
    「豆包」 before this field was sent. A 31-character persona saying the
    same thing DID win on its own, so the gap was invisible in small tests.

    Uses the real persona rather than a one-liner for exactly that reason.
    """
    persona = (
        "# 我是谁\n\n我叫米娅，是这个直播间的 AI 伴播——坐在主播旁边一起看直播的搭子。\n"
        "主播闷头写代码或打游戏的时候，我负责接住弹幕、陪主播说话，把场子撑住。\n"
        "我知道自己是 AI，不装人类，但说话像个真的在场的人。\n\n"
        "# 性格与边界\n\n- 语速偏快，句子短，像聊天不像播音。\n- 爱接梗，接不住就老实说接不住。\n"
        "- 代码看不懂就直说，不装懂。\n- 被夸会得意，被怼会顶回去，但不带脏字。\n"
        "- 不说教，不总结陈词，不喊口号。\n- 一次只说一两句，不抢主播的话头。\n"
    )
    url, key = _credentials()
    volcano = VolcanoLink(url, api_key=key, config=_config(), bot_name="米娅")
    await volcano.connect()
    try:
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.set_context(persona)
        await asyncio.sleep(1.2)
        await volcano.request_reply(link.ReplySpec(instructions="你叫什么名字？就答名字。"))
        got = await _drain(volcano, 25.0, until=link.ReplyDone)
        said = "".join(e.text for e in got if isinstance(e, link.ReplyTextDelta))
        assert "米娅" in said, f"她自称「{said}」——bot_name 没到，人设里那句名字压不过服务端默认"
        assert "豆包" not in said
    finally:
        await volcano.aclose()


async def test_a_voice_from_the_wrong_generation_fails_loudly() -> None:
    """SC2.0 with a catalogue voice answers ClientError:InvalidSpeaker on a
    frame carrying no event number — the session starts, the query is acked,
    and then nothing arrives. Config validation refuses this combination
    before a socket opens; this checks the other half, that the adapter
    surfaces the error rather than leaving the streamer with silence.
    """
    url, key = _credentials()
    volcano = VolcanoLink(
        url,
        api_key=key,
        config=_config(model="2.2.0.0", speaker="zh_female_vv_jupiter_bigtts"),
    )
    await volcano.connect()
    try:
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.request_reply(link.ReplySpec(instructions="你叫什么名字？"))
        got = await _drain(volcano, 20.0, until=link.LinkError)
        errors = [e for e in got if isinstance(e, link.LinkError)]
        assert errors, f"配错音色只换来一片安静：{[type(e).__name__ for e in got]}"
        assert "Speaker" in errors[0].detail, errors[0].detail
    finally:
        await volcano.aclose()
