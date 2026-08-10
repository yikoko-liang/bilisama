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
import functools
import json
import platform
import socket
import subprocess
import tempfile
import wave
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


@functools.cache
def _speech(text: str) -> bytes:
    """Real Chinese speech via the macOS synthesizer, as 16 kHz mono s16 PCM.

    A pure sine tone does not work here: Silero is a speech detector and
    (correctly) refuses to call a 220 Hz beep speech, so the whole pipeline
    stays silent. Verified the hard way — 90 seconds of tone produced zero
    events and the server logged cumulative audio=0.00s.
    """
    if platform.system() != "Darwin":
        pytest.skip("测试语料靠 macOS 的 say 合成，这台机器不是 macOS")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    try:
        subprocess.run(
            [
                "say",
                "--file-format=WAVE",
                "--data-format=LEI16@16000",
                "-o",
                str(path),
                text,
            ],
            check=True,
            capture_output=True,
        )
        with wave.open(str(path), "rb") as w:
            assert w.getframerate() == _INPUT_RATE and w.getnchannels() == 1
            return w.readframes(w.getnframes())
    finally:
        path.unlink(missing_ok=True)


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


async def _speak_one_turn(ws: Any, text: str = "你好你好今天天气怎么样") -> list[dict[str, Any]]:
    """Speak, stop, and collect until a reply COMPLETES.

    Punctuation makes the synthesizer pause mid-utterance, the pause splits the
    audio into two turns, and the second turn barge-ins the first reply — so a
    cancelled response.done can arrive before the real one. Collect through
    cancellations until a completed done (or the deadline).
    """
    await _append(ws, _speech(text))
    await _append(ws, _silence(1600))
    collected: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120.0
    while loop.time() < deadline:
        chunk = await _events_until(ws, {"response.done"}, timeout=deadline - loop.time())
        collected.extend(chunk)
        dones = [e for e in chunk if e.get("type") == "response.done"]
        if not dones:
            break
        if any((d.get("response") or {}).get("status") == "completed" for d in dones):
            break
    return collected


@pytest.fixture
async def ws() -> AsyncIterator[Any]:
    # The server runs one pipeline (num_pipelines=1), and releasing a slot lags
    # the previous test's disconnect by a moment. Retry instead of failing on
    # session_limit_reached so back-to-back tests do not race the release.
    deadline = asyncio.get_running_loop().time() + 30.0
    while True:
        conn = await websockets.connect(SERVER_URL, max_size=16 * 1024 * 1024)
        first = json.loads(await asyncio.wait_for(conn.recv(), timeout=10.0))
        if first.get("type") == "session.created":
            break
        await conn.close()
        limit = (first.get("error") or {}).get("type") == "session_limit_reached"
        assert limit and asyncio.get_running_loop().time() < deadline, first
        await asyncio.sleep(1.0)
    try:
        yield conn
    finally:
        await conn.close()


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
async def test_in_band_injection_loses_the_reply_but_the_slot_recovers(ws: Any) -> None:
    """The wedge, checked against the real service — with a finding.

    Plan section 3.3 predicted an in-band response.create against an open
    speculative turn would swallow the reply AND jam the slot for good ("连接
    实际上死了"). Verified on v0.2.12-40-g68f0604: the first half is real, the
    second is not. Every link in the chain exists, but the composition
    self-heals — the injected create sets in_response at once, so the moment
    the streamer resumes, the barge-in path cancels the injected reply and
    frees the slot, and the resumed turn completes normally.

    So the reason for out-of-band stands (an in-band injection loses its reply,
    which for a paid Super Chat thank-you is a revenue bug), while the
    doomsday half gets downgraded. The mock keeps modelling a permanent trap
    on purpose: harsher than the real server is the safe direction — a client
    that survives the permanent wedge also survives the transient one.
    """
    # Open a speculative turn: speak, then only a short silence so the soft end
    # fires while the reopen window stays open.
    await _append(ws, _speech("在吗在吗，问你个事"))
    await _append(ws, _silence(200))
    opened = await _events_until(ws, {"input_audio_buffer.speech_stopped"}, timeout=15.0)
    assert "input_audio_buffer.speech_started" in _types(opened), _types(opened)

    # In-band create against the open turn. Upstream stamps it with the
    # streamer's uncommitted turn id and answers with response.created.
    await ws.send(json.dumps({"type": "response.create", "response": {}}))
    created = await _events_until(ws, {"response.created"}, timeout=10.0)
    created_events = [e for e in created if e.get("type") == "response.created"]
    assert created_events, _types(created)
    injected_id = (created_events[-1].get("response") or {}).get("id")

    # Resume speech: the revision moves, the injected reply goes stale.
    await _append(ws, _speech("我还没说完，接着聊啊"))
    await _append(ws, _silence(1600))

    # Collect until the resumed turn's own reply completes.
    aftermath: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120.0
    while loop.time() < deadline:
        chunk = await _events_until(ws, {"response.done"}, timeout=deadline - loop.time())
        aftermath.extend(chunk)
        dones = [e for e in chunk if e.get("type") == "response.done"]
        if not dones:
            break
        if any((d.get("response") or {}).get("status") == "completed" for d in dones):
            break

    completed = [
        e
        for e in aftermath
        if e.get("type") == "response.done"
        and (e.get("response") or {}).get("status") == "completed"
    ]
    # Half one, still true: the injected reply never completes.
    assert not any(
        (e.get("response") or {}).get("id") == injected_id for e in completed
    ), f"注入的回复居然完成了：{injected_id}"
    # Half two, the finding: the slot recovers — the resumed turn answers.
    assert completed, "主播接着说话后连回复都没有——那才是真卡死"

    # And a later create is admitted rather than refused.
    await ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {"conversation": "none", "instructions": "说一个字。"},
            }
        )
    )
    probe = await _events_until(ws, {"response.done", "error"}, timeout=60.0)
    errors = [e for e in probe if e.get("type") == "error"]
    slot_errors = [
        e
        for e in errors
        if (e.get("error") or {}).get("type") == "conversation_already_has_active_response"
    ]
    assert not slot_errors, "槽位仍然占着——永久卡死在这个版本上复现了，改回原断言并更新计划"


@requires_server
async def test_out_of_band_injection_is_immune(ws: Any) -> None:
    """The architectural decision, proven where it matters.

    conversation="none" forces turn_id=None (handlers/response.py:230-241), so
    every speculative staleness gate treats the reply as always-latest. Same
    scenario as the wedge test, opposite outcome.
    """
    await _append(ws, _speech("在吗在吗，问你个事"))
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
