"""Contract tests against a genuinely running speech-to-speech server.

Everything else in the suite runs against tests/fakes/mock_realtime.py — a fake
we wrote from our own reading of the upstream source. These tests exist because
a fake cannot confirm its own reading: the fault flags are ours, the assertions
are ours, and a misreading would be wrong on both sides at once. Plan section 13
item 2 asks for the wedge to be reproduced "装好之后" — after installing, on the
real thing. This file is that.

The server is NOT started here. Start it first (see scripts/
make_official_pipe_config.py for the full recipe); every test skips with a
plain reason when nothing is listening, so the gate stays honest rather than
red on machines without the server.

Upstream version note: the checkout these tests were written against is pinned
in UPSTREAM_DESCRIBE below. test_upstream_checkout_matches_the_pin turns drift
into a visible skip-with-warning instead of silent staleness (backlog, plan
section 16.8 item 12).
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import socket
import struct
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets

pytestmark = pytest.mark.integration

SERVER_URL = "ws://127.0.0.1:8765/v1/realtime"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_S2S_ROOT = _REPO_ROOT.parent / "speech-to-speech"

# `git describe` of the upstream checkout the wedge reproduction was verified
# against. If upstream moves, re-run this file against the new checkout and
# update the pin — handlers/response.py is where the whole out-of-band
# architecture leans, and its recent history is all response bookkeeping.
UPSTREAM_DESCRIBE = "v0.2.12-40-g68f0604"

_INPUT_RATE = 16000  # the append stream is 16 kHz mono s16 (TurnConfig.sample_rate)


def _server_is_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.0):
            return True
    except OSError:
        return False


requires_server = pytest.mark.skipif(
    not _server_is_up(),
    reason="没有跑着的 s2s 服务器（127.0.0.1:8765）。起法见 scripts/make_official_pipe_config.py",
)


def _pcm_tone(ms: int, *, freq: float = 220.0) -> bytes:
    """Synthetic speech-band audio. Loud enough for Silero to call it speech."""
    n = int(_INPUT_RATE * ms / 1000)
    return struct.pack(
        f"<{n}h",
        *(int(12000 * math.sin(2 * math.pi * freq * i / _INPUT_RATE)) for i in range(n)),
    )


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * int(_INPUT_RATE * ms / 1000)


async def _append(ws: Any, pcm: bytes, *, frame_ms: int = 32) -> None:
    """Stream PCM as append frames, the way a real client would."""
    step = 2 * int(_INPUT_RATE * frame_ms / 1000)
    for i in range(0, len(pcm), step):
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm[i : i + step]).decode(),
                }
            )
        )


async def _events_until(
    ws: Any, stop_types: set[str], *, timeout: float, feed_silence: bool = True
) -> list[dict[str, Any]]:
    """Collect events until one of stop_types arrives or the timeout passes.

    Keeps appending silence while waiting when feed_silence is set — rule 7 of
    plan section 3.3: the reopen window runs on the audio clock, so a starved
    append stream freezes the server's sense of time.
    """
    events: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if feed_silence:
            await _append(ws, _silence(64))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
        except TimeoutError:
            continue
        event = json.loads(raw)
        events.append(event)
        if event.get("type") in stop_types:
            return events
    return events


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e.get("type", "") for e in events]


async def _speak_one_turn(ws: Any, *, ms: int = 1200) -> list[dict[str, Any]]:
    """Speak, stop, and collect until the reply finishes."""
    await _append(ws, _pcm_tone(ms))
    await _append(ws, _silence(1600))
    return await _events_until(ws, {"response.done"}, timeout=90.0)


@pytest.fixture
async def ws() -> AsyncIterator[Any]:
    async with websockets.connect(SERVER_URL, max_size=16 * 1024 * 1024) as conn:
        # session.created is the server's hello; nothing works before it.
        first = json.loads(await asyncio.wait_for(conn.recv(), timeout=10.0))
        assert first.get("type") == "session.created", first
        yield conn


# ------------------------------------------------------------ 1. duplex round trip


@requires_server
async def test_full_duplex_round_trip(ws: Any) -> None:
    """One spoken turn comes back with audio: the server genuinely runs.

    This is the missing half of stage 0's first acceptance criterion — installed
    was proven long ago, started never was (plan section 15.8).
    """
    events = await _speak_one_turn(ws)
    kinds = _types(events)

    assert "input_audio_buffer.speech_started" in kinds, kinds
    assert "input_audio_buffer.speech_stopped" in kinds, kinds
    # The official pipeline owns its TTS, so the reply must carry audio.
    assert "response.output_audio.delta" in kinds, kinds
    done = next(e for e in events if e.get("type") == "response.done")
    status = (done.get("response") or {}).get("status")
    assert status == "completed", done


# ------------------------------------------------------------ 2. the wedge, on the real server


@requires_server
async def test_in_band_injection_during_speculative_window_wedges(ws: Any) -> None:
    """Plan section 3.3's worst finding, reproduced on the real service.

    An in-band response.create while the streamer's speculative turn is still
    open gets its reply swallowed — no deltas, no response.done — and the slot
    never frees, so later creates are refused. Until now this was only ever
    shown on our own fake (mock_realtime.py), which proves the client handles a
    wedge, not that the wedge exists.
    """
    # Open a speculative turn: speak, then stop just long enough for the soft
    # end, keeping the reopen window open (64ms silence trips it; the window
    # stays open for unanswered_reopen_ms of appended audio).
    await _append(ws, _pcm_tone(900))
    await _append(ws, _silence(200))
    opened = await _events_until(ws, {"input_audio_buffer.speech_stopped"}, timeout=15.0)
    assert "input_audio_buffer.speech_started" in _types(opened), _types(opened)

    # In-band create against the open turn. Upstream stamps it with the
    # streamer's uncommitted turn id (handlers/response.py:236-238); resuming
    # speech bumps the revision and the reply is discarded wholesale.
    await ws.send(json.dumps({"type": "response.create", "response": {}}))
    # Resume speech so the speculative revision moves and the injected reply
    # goes stale.
    await _append(ws, _pcm_tone(700))
    await _append(ws, _silence(300))

    aftermath = await _events_until(ws, {"response.done"}, timeout=20.0)
    in_band_done = [
        e
        for e in aftermath
        if e.get("type") == "response.done"
        and (e.get("response") or {}).get("status") == "completed"
    ]
    # The injected reply must not complete; the implicit turn may or may not
    # answer, but the injected one is swallowed.
    assert not in_band_done, _types(aftermath)

    # The slot is wedged: a later create is refused outright.
    await ws.send(json.dumps({"type": "response.create", "response": {}}))
    refusal = await _events_until(ws, {"error"}, timeout=10.0)
    errors = [e for e in refusal if e.get("type") == "error"]
    assert errors, "第二个 response.create 没有被拒——卡死没有发生？"
    codes = [(e.get("error") or {}).get("type") for e in errors]
    assert "conversation_already_has_active_response" in codes, codes


@requires_server
async def test_out_of_band_injection_is_immune(ws: Any) -> None:
    """The architectural decision, proven where it matters.

    conversation="none" forces turn_id=None (handlers/response.py:230-241), so
    every speculative staleness gate treats the reply as always-latest. Same
    scenario as the wedge test, opposite outcome.
    """
    await _append(ws, _pcm_tone(900))
    await _append(ws, _silence(200))
    await _events_until(ws, {"input_audio_buffer.speech_stopped"}, timeout=15.0)

    await ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "conversation": "none",
                    "instructions": "用一句话打个招呼。",
                    "output_modalities": ["audio"],
                },
            }
        )
    )
    events = await _events_until(ws, {"response.done"}, timeout=90.0)
    done = [e for e in events if e.get("type") == "response.done"]
    assert done, f"out-of-band 回复没有完成：{_types(events)}"
    status = (done[-1].get("response") or {}).get("status")
    assert status in {"completed", "cancelled"}, done[-1]


# ------------------------------------------------------------ 3. upstream pin


def test_upstream_checkout_matches_the_pin() -> None:
    """Warn loudly when upstream moves out from under these tests.

    The reconciliation gate covers turn-detection field names both ways, but
    the wedge mechanism itself has no gate — only this pin and the tests above.
    """
    if not (_S2S_ROOT / ".git").exists():
        pytest.skip(f"没有本地 speech-to-speech 检出：{_S2S_ROOT}")
    described = subprocess.run(
        ["git", "-C", str(_S2S_ROOT), "describe", "--always", "--dirty"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert described == UPSTREAM_DESCRIBE, (
        f"上游检出是 {described}，这批真机测试是对着 {UPSTREAM_DESCRIBE} 写的。\n"
        "重跑本文件确认卡死复现仍然成立，然后更新 UPSTREAM_DESCRIBE。"
    )
