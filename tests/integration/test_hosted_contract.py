"""Contract tests against the real hosted endpoint (DashScope).

The shipping path had no repeatable test at all. Everything about it was
covered by tests/fakes/mock_realtime.py — a fake written from our own reading —
plus one hand-run session recorded as prose in plan section 15.1 on 2026-08-11.
That is the shape this repo has been burned by twice: a fake kinder than the
server certifies a broken client as correct. Backlog #68.

Nothing here starts anything. Credentials come from the environment (path.sh
supplies them locally) and every test skips with a plain reason when they are
absent, so the gate stays honest on a machine that has none.

No audio device is opened. The one test that needs the server's own VAD to fire
feeds a WAV synthesised by `say` into a file — a real speech waveform that never
goes near a microphone or a speaker.

What was learned running these on 2026-08-25, and why each assertion exists:

- `turn_detection.interrupt_response` is accepted and silently dropped. The
  session.updated echo comes back without the key, and three paired rounds
  showed the reply cancelled whether or not it was sent. So the paid-protection
  window (plan section 4.2) has no mechanism on this provider, and
  HostedLink.end_protection staying empty is correct rather than unfinished
  (backlog #45).
- `response.conversation_id` does not exist in these payloads at all, in-band
  or out-of-band. Pairing response.created with the request that caused it
  cannot use it here (backlog #23).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets

from bilisama.config.enums import ProviderName
from bilisama.realtime.providers import _DEFAULT_MODELS, _hosted_url

pytestmark = pytest.mark.provider_a


def _endpoint() -> tuple[str, str]:
    raw = os.environ.get("dashscope_url", "")  # noqa: SIM112  (path.sh 里的原名)
    key = os.environ.get("ali_api_key", "")  # noqa: SIM112  (path.sh 里的原名)
    if not raw or not key:
        pytest.skip("没有托管端点凭据（dashscope_url / ali_api_key）。本机跑先 source path.sh")
    url = _hosted_url(raw, ProviderName.DASHSCOPE)
    if "model=" not in url:
        url += ("&" if "?" in url else "?") + f"model={_DEFAULT_MODELS[ProviderName.DASHSCOPE]}"
    return url, key


class _Session:
    """One connection, with every frame kept for inspection."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._ws: Any = None
        self._pump: asyncio.Task[None] | None = None

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == kind]

    async def send(self, **body: Any) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(body))

    async def settle(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def _connected() -> AsyncIterator[_Session]:
    url, key = _endpoint()
    session = _Session()
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        session._ws = ws

        async def pump() -> None:
            async for raw in ws:
                session.frames.append(json.loads(raw))

        session._pump = asyncio.create_task(pump())
        await asyncio.sleep(1.2)  # session.created lands first
        try:
            yield session
        finally:
            session._pump.cancel()


@pytest.fixture
async def session() -> AsyncIterator[_Session]:
    async for one in _connected():
        yield one


async def test_the_endpoint_answers_with_a_session(session: _Session) -> None:
    """The floor under everything else: if this fails, read no further."""
    created = session.of("session.created")
    assert (
        created
    ), f"连上了却没有 session.created，收到的是：{[f.get('type') for f in session.frames]}"
    assert (created[0].get("session") or {}).get("turn_detection"), "会话里没有判停配置"


async def test_interrupt_response_is_swallowed_rather_than_honoured(session: _Session) -> None:
    """Backlog #45, the wire half.

    Not refused — which would at least be loud — and not applied. The echo comes
    back describing a turn_detection that never had the key. `item.truncate`
    behaves the same way here (capabilities.py), so this is the provider's habit
    rather than a one-off.
    """
    await session.send(
        type="session.update",
        session={
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "silence_duration_ms": 800,
                "interrupt_response": False,
            }
        },
    )
    await session.settle(2.5)
    assert not session.of("error"), f"服务端报错了：{session.of('error')}"
    updated = session.of("session.updated")
    assert updated, "发了 session.update 却没有 session.updated"
    turn = (updated[-1].get("session") or {}).get("turn_detection") or {}
    assert (
        "interrupt_response" not in turn
    ), f"服务端这次认了 interrupt_response——#45 的结论要重新做：{turn}"


async def test_a_reply_carries_no_conversation_id_to_pair_on(session: _Session) -> None:
    """Backlog #23. The idea was to tell an out-of-band reply from a VAD-started
    one by response.conversation_id (null versus conv_xxx). The field is simply
    not in these payloads, so the FIFO pairing in client.py stays."""
    await session.send(
        type="conversation.item.create",
        item={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "用五个字回答：你好吗"}],
        },
    )
    await session.settle(0.8)
    await session.send(
        type="response.create",
        response={
            "conversation": "none",
            "modalities": ["text"],
            "instructions": "只回五个字",
            "max_output_tokens": 32,
        },
    )
    await session.settle(6)
    done = session.of("response.done")
    assert done, "没等到 response.done"
    for frame in session.of("response.created") + done:
        assert "conversation_id" not in (
            frame.get("response") or {}
        ), f"这次带上 conversation_id 了——#23 的结论要重新做：{frame}"


def _speech_wav(tmp_path: Path) -> Path:
    """A real speech waveform, synthesised to a file. No device is opened."""
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
                "你好，我说句话打断一下，今天天气怎么样啊",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"造不出语音素材（macOS say 不可用）：{exc}")
    return out


async def _one_round(*, protect: bool, wav: Path) -> str:
    """Ask for a long spoken reply, talk over it, report how it ended."""
    turn: dict[str, Any] = {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800}
    if protect:
        turn["interrupt_response"] = False
    async for session in _connected():
        await session.send(type="session.update", session={"turn_detection": turn})
        await session.settle(1.0)
        await session.send(
            type="conversation.item.create",
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "讲个五百字的长故事"}],
            },
        )
        await session.settle(0.6)
        await session.send(
            type="response.create",
            response={
                # Audio, not text: a text reply finishes before the speech can
                # land, and an experiment where the two never overlap answers
                # nothing. The first run of this made exactly that mistake.
                "modalities": ["text", "audio"],
                "instructions": "讲一个五百字左右的长故事，从头讲到尾，中间不要停",
                "max_output_tokens": 1200,
            },
        )
        await session.settle(0.9)  # let her get going, then talk over her
        with wave.open(str(wav)) as w:
            chunk = w.getframerate() // 10  # 100 ms
            while True:
                block = w.readframes(chunk)
                if not block:
                    break
                await session.send(
                    type="input_audio_buffer.append",
                    audio=base64.b64encode(block).decode(),
                )
                await asyncio.sleep(0.1)
        await session.settle(12)
        assert session.of("input_audio_buffer.speech_started"), "服务端没把这段波形当成人声"
        done = session.of("response.done")
        assert done, "没等到 response.done"
        return str((done[-1].get("response") or {}).get("status"))
    raise AssertionError("unreachable")


async def test_protection_does_not_survive_being_talked_over(tmp_path: Path) -> None:
    """Backlog #45, the behaviour half — and the one that decides the product.

    A paid Super Chat's thanks is supposed to finish even if the streamer starts
    talking (plan section 4.2: only panic may kill it). Paired rounds, three
    times each on 2026-08-25: cancelled every time, with the flag and without.
    The control arm matters as much as the protected one — it proves the speech
    really did reach the server's VAD, so a 「completed」 here would mean
    something.
    """
    wav = _speech_wav(tmp_path)
    control = await _one_round(protect=False, wav=wav)
    protected = await _one_round(protect=True, wav=wav)
    assert control == "cancelled", f"对照组没被打断，这一轮不算数：{control}"
    assert (
        protected == "cancelled"
    ), f"关掉打断之后回复活下来了——#45 有救了，去接 end_protection：{protected}"
