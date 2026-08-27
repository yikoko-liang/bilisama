"""Contract tests against the real Volcengine dialogue endpoint.

⚠️ NOT YET RUN. Every assertion below comes from the vendor's documentation
plus one worked example frame, not from a live session — the credentials were
not on this machine when the adapter was written. Read it as the list of
questions to be answered, not as answers. When it does run, whatever it says
goes into plan section 15.17 and the wrong guesses get fixed here first.

That distinction is the point of the file existing at all: the alternative is
an adapter whose assumptions live only in comments, which is how a fake kinder
than the server certified a broken client twice already.

Nothing here opens an audio device or starts anything. Credentials come from
the environment (path.sh supplies them locally) and every test skips with a
plain reason when they are absent, so the gate stays honest on a machine that
has none.

The six things the docs do not settle, in the order they bite:

1. Which optional fields each message type really carries, and in what order.
   The published sample is one TTSResponse; the client frames are inferred.
2. Whether a session-level event will accept a frame with no session id, or
   whether the length prefix is mandatory even when empty.
3. Whether `tts.audio_config.format = "pcm_s16le"` is honoured — the default
   is Ogg Opus, and every consumer above L2 wants raw PCM.
4. The real event order after ChatTextQuery, and in particular whether
   TTSEnded always follows ChatEnded. The adapter settles the reply on
   TTSEnded and lets a watchdog cover the case where it never comes.
5. What the server actually does when the streamer talks over a reply. The
   adapter treats ASRInfo as the whole barge-in signal and assumes generation
   may continue on the server's side.
6. Whether UpdateConfig actually takes effect. This one is not a curiosity:
   the assembly pushes the persona AFTER connect (dev_talk.py:1330), so on
   this protocol the persona ALWAYS travels as an UpdateConfig and never
   inside StartSession. If the event is accepted and ignored, she connects,
   sounds fine, and has no persona — the exact shape of backlog item 28,
   which took a live probe and several days to find.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
from collections.abc import AsyncIterator
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


def _credentials() -> tuple[str, str, str]:
    app_id = os.environ.get("volcano_app_id", "")  # noqa: SIM112  (path.sh 里的原名)
    access_key = os.environ.get("volcano_access_key", "")  # noqa: SIM112
    if not app_id or not access_key:
        pytest.skip("没有火山凭据（volcano_app_id / volcano_access_key）。本机跑先 source path.sh")
    # Lower case to match path.sh, same as the other three names above.
    override = os.environ.get("volcano_url", "")  # noqa: SIM112
    return override or PROFILES[ProviderName.VOLCANO].default_url, app_id, access_key


def _config(**over: Any) -> VolcanoConfig:
    return VolcanoConfig(**over)


async def _link(**over: Any) -> AsyncIterator[VolcanoLink]:
    url, app_id, access_key = _credentials()
    volcano = VolcanoLink(url, app_id=app_id, access_key=access_key, config=_config(**over))
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
    url, app_id, access_key = _credentials()
    headers = {
        "X-Api-App-ID": app_id,
        "X-Api-Access-Key": access_key,
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


async def test_what_the_server_does_when_talked_over() -> None:
    """Question 5, and the one the product hangs on.

    There is no response.cancel on this protocol, so `cancel()` is local only.
    What this establishes is whether the server at least tells us to stop
    playing (ASRInfo), and whether it keeps generating afterwards. Both answers
    are usable; not knowing which one is true is not.

    Feeds silence rather than opening a microphone. If the server's VAD never
    fires on silence — the likely outcome — the test says so instead of
    pretending it proved something.
    """
    async for volcano in _link():
        await _drain(volcano, 5.0, until=link.LinkUp)
        await volcano.request_reply(
            link.ReplySpec(instructions="讲一个五百字左右的长故事，从头讲到尾，中间不要停")
        )
        collected: list[link.LinkEvent] = []
        task = asyncio.create_task(_collect_into(volcano, collected))
        for _ in range(150):  # 3 s of uplink, so the session is not idle
            await volcano.push_audio(_SILENCE_20MS)
            await asyncio.sleep(0.02)
        await asyncio.sleep(10.0)
        task.cancel()

        started = [e for e in collected if isinstance(e, link.SpeechStarted)]
        if not started:
            pytest.skip(
                "喂静音没能触发服务端判停，这一问答不了。"
                "要真答，得像 test_hosted_contract 那样合成一段语音波形喂进去"
            )
        after = collected[collected.index(started[0]) :]
        audio_after = [e for e in after if isinstance(e, link.ReplyAudioDelta)]
        assert not audio_after or all(
            e.handle.stale for e in audio_after
        ), "打断之后还有不带作废标记的音频回来——那两个消费者会把它继续播出去"


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
