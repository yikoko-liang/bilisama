"""Probes for the voice gate's open questions, against the real backends.

The gate (director/voice_turn.py) was built on facts read off the fake
server and the upstream source: text arrives before audio, a cancel after
the first token stops the audio, one reply at a time. Each probe here asks
one of those questions of a real server (planner plan §5.8) and prints its
answer as a `PROBE` line — run with `-s` to see them — so the plan's
待验证 table can be filled from a log rather than from memory.

Nothing here starts anything. The s2s probes need a server on 127.0.0.1:8765
(scripts/make_official_pipe_config.py), the hosted ones need path.sh's
credentials; every probe skips with a plain reason otherwise. A probe asserts
only what would make the run meaningless (speech detected, a reply ended);
the facts it exists to learn are printed, not asserted, because the point is
to find out.

What the first runs found (2026-09-09, DashScope on the profile's default
model, volcano O2.0, s2s official pipeline — paraformer / qwen3.7-flash /
Qwen3-TTS — from config/s2s/official-pipe.local.json):

- Both hosted backends write the marker for an audience line with the
  contract in the session: 「[AUDIENCE] 主播在跟观众打招呼。」 and
  「[AUDIENCE] 主播在和观众聊天气话题。」.
- DashScope: first transcript delta 4 ms after response.created, first audio
  delta 213 ms after — text leads by ~210 ms; the whole 3.8 s marker reply
  streams in 0.84 s, so a cancel sent at the first text lands on a reply
  that is already complete (status completed, nothing to roll back).
- Volcano O2.0: text and audio arrive in the same tick, audio marginally
  first; the marker reply completes before ClientInterrupt lands; asked for
  her last line afterwards she repeats the marker line — it is in the dialog.
- DashScope takes input_audio_transcription {model: gummy-realtime-v1} and
  streams the streamer's words as conversation.item.input_audio_transcription
  deltas plus a completed frame.
- DashScope refuses a second in-band response.create while one generates
  (invalid_value, 「Cannot create response while another response is in
  progress」). A conversation-none create was accepted once and refused once
  across two runs; the condition is not understood — the single-slot rule in
  capabilities.py stands until it is.
- s2s official pipeline: she writes 「[AUDIENCE] 主播在跟观众打招呼」 too; the
  whole Chinese reply arrives as ONE output_audio_transcript.done ahead of
  18 audio deltas. A response.cancel sent at that fragment is answered by
  done(cancelled) within 1 ms and NO audio delta ever reaches the client;
  the server logs 「TTS generation cancelled (interruption)」 and nothing
  about the LLM — the history is not rolled back, the audio never starts.
  The next line, addressed to her, completes normally and without a marker.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import wave
from pathlib import Path
from typing import Any

import pytest
import websockets

from bilisama.config.schema import PersonaConfig
from bilisama.persona.loader import live_voice_rules, template_variables
from bilisama.realtime import link
from tests.integration.speech_fixture import speech_wav

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_AUDIENCE_LINE = "各位观众朋友们大家好，今天我们来聊聊天气怎么样"
_TO_HER_LINE = "豆腐，你觉得今天的天气怎么样"


def _instructions() -> str:
    """A short persona plus the marker contract, exactly as dev-talk pushes it."""
    rules = live_voice_rules(_CONFIG_DIR, template_variables(PersonaConfig()), addressing=True)
    return "你是直播伴播豆腐，说话简短。\n\n" + rules


def _probe(name: str, **facts: Any) -> None:
    print("PROBE " + name + ": " + " ".join(f"{k}={v!r}" for k, v in facts.items()))


def _pcm_blocks(wav: Path, *, ms: int) -> list[bytes]:
    with wave.open(str(wav)) as w:
        step = w.getframerate() * ms // 1000
        blocks: list[bytes] = []
        while True:
            block = w.readframes(step)
            if not block:
                return blocks
            blocks.append(block)


# ------------------------------------------------------------ DashScope


class _Stamped:
    """One DashScope connection with every frame kept AND timed — the
    contract file's _Session keeps frames only, and the gate's question is
    about milliseconds."""

    def __init__(self) -> None:
        self.frames: list[tuple[float, dict[str, Any]]] = []
        self._ws: Any = None

    def of(self, kind: str) -> list[tuple[float, dict[str, Any]]]:
        return [(t, f) for t, f in self.frames if f.get("type") == kind]

    def first(self, *kinds: str) -> float | None:
        for t, f in self.frames:
            if f.get("type") in kinds:
                return t
        return None

    async def send(self, **body: Any) -> None:
        await self._ws.send(json.dumps(body))


async def _dashscope_stamped(extra: dict[str, Any] | None = None) -> tuple[Any, _Stamped]:
    from tests.integration.test_hosted_contract import _endpoint

    url, key = _endpoint()
    session = _Stamped()
    ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"})
    session._ws = ws

    async def pump() -> None:
        async for raw in ws:
            session.frames.append((time.monotonic(), json.loads(raw)))

    pump_task = asyncio.create_task(pump())
    await asyncio.sleep(1.2)
    body: dict[str, Any] = {
        "instructions": _instructions(),
        "modalities": ["text", "audio"],
        "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800},
    }
    body.update(extra or {})
    await session.send(type="session.update", session=body)
    await asyncio.sleep(1.0)

    async def close() -> None:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        await ws.close()

    return close, session


@pytest.mark.provider_a
async def test_dashscope_when_the_text_of_her_own_turn_arrives(tmp_path: Path) -> None:
    """§5.8 #1 for DashScope, in milliseconds: the gate holds a turn for at
    most 600 ms waiting for text. If the transcript trails the audio by more
    than that, the hold times out, the audio plays, and a late marker cuts it
    — the audience hears the head of a turn that was never for them."""
    close, session = await _dashscope_stamped()
    try:
        wav = speech_wav(tmp_path, _AUDIENCE_LINE)
        await _feed(session, wav, tail_s=3.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 25.0
        while loop.time() < deadline and session.first("response.done") is None:
            await asyncio.sleep(0.02)
        t_created = session.first("response.created")
        t_audio = session.first("response.audio.delta")
        t_text = session.first(*_TEXT_DELTA_KINDS)
        t_audio_done = session.first("response.audio.done")
        t_done = session.first("response.done")
        pcm_bytes = sum(
            len(base64.b64decode(str(f.get("delta") or "")))
            for _, f in session.of("response.audio.delta")
        )
        head = "".join(
            str(f.get("delta") or "")
            for _, f in session.frames
            if f.get("type") in _TEXT_DELTA_KINDS
        )
        rate = 24000  # DashScope's downlink (capabilities / hosted bootstrap)

        def ms(a: float | None, b: float | None) -> int | None:
            return None if a is None or b is None else round((b - a) * 1000)

        _probe(
            "dashscope.turn_timing",
            created_to_first_audio_ms=ms(t_created, t_audio),
            created_to_first_text_ms=ms(t_created, t_text),
            first_audio_to_first_text_ms=ms(t_audio, t_text),
            first_audio_to_audio_done_ms=ms(t_audio, t_audio_done),
            audio_done_to_first_text_ms=ms(t_audio_done, t_text),
            created_to_done_ms=ms(t_created, t_done),
            audio_deltas=len(session.of("response.audio.delta")),
            audio_ms_of_pcm=round(pcm_bytes / 2 / rate * 1000),
            text_deltas=len([1 for _, f in session.frames if f.get("type") in _TEXT_DELTA_KINDS]),
            head=head[:40],
            head_is_marker=head.lstrip().startswith("["),
        )
        assert t_done is not None, "没等到 response.done"
    finally:
        await close()


@pytest.mark.provider_a
async def test_dashscope_out_of_band_create_runs_beside_or_behind(tmp_path: Path) -> None:
    """§5.8 #10, sharpened by the first run: the in-band second create is
    refused, the conversation-none one is accepted. Accepted to run in
    parallel, or queued behind? The order of the two response.done frames
    and where the second reply's text lands say which."""
    close, session = await _dashscope_stamped()
    try:
        await session.send(
            type="conversation.item.create",
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "讲个五百字的长故事"}],
            },
        )
        await asyncio.sleep(0.6)
        await session.send(
            type="response.create",
            response={"modalities": ["text", "audio"], "max_output_tokens": 1200},
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while loop.time() < deadline and session.first(*_TEXT_DELTA_KINDS) is None:
            await asyncio.sleep(0.02)
        assert session.first(*_TEXT_DELTA_KINDS) is not None, "第一条回复没开始"
        first_id = str((session.of("response.created")[0][1].get("response") or {}).get("id"))
        t_second = time.monotonic()
        await session.send(
            type="response.create",
            response={
                "conversation": "none",
                "modalities": ["text"],
                "instructions": "只说一个字：好",
            },
        )
        deadline = loop.time() + 30.0
        while loop.time() < deadline and len(session.of("response.done")) < 2:
            await asyncio.sleep(0.05)
        dones = [(t, (f.get("response") or {})) for t, f in session.of("response.done")]
        text_done = session.of("response.text.done")
        _probe(
            "dashscope.out_of_band_beside",
            first_id=first_id,
            done_order=[
                (str(r.get("id")) == first_id, str(r.get("status")), round((t - t_second) * 1000))
                for t, r in dones
            ],
            second_text_at_ms=None if not text_done else round((text_done[0][0] - t_second) * 1000),
            second_text=str(text_done[0][1].get("text"))[:20] if text_done else None,
            errors=[(f.get("error") or {}).get("message") for _, f in session.of("error")],
        )
        await session.send(type="response.cancel")
        await asyncio.sleep(1.0)
    finally:
        await close()


async def _dashscope_session(tmp_path: Path, *, extra: dict[str, Any] | None = None) -> Any:
    from tests.integration.test_hosted_contract import _connected

    gen = _connected()
    session = await gen.__anext__()
    body: dict[str, Any] = {
        "instructions": _instructions(),
        "modalities": ["text", "audio"],
        "turn_detection": {"type": "server_vad", "threshold": 0.5, "silence_duration_ms": 800},
    }
    body.update(extra or {})
    await session.send(type="session.update", session=body)
    await session.settle(1.0)
    return gen, session


_SILENCE_100MS = b"\x00" * 3200  # 16 kHz mono s16le
_TEXT_DELTA_KINDS = ("response.audio_transcript.delta", "response.output_audio_transcript.delta")


async def _feed(session: Any, wav: Path, *, tail_s: float = 3.0) -> None:
    """The line, then silence: the server's VAD only closes a turn on the
    audio clock, so a stream that stops with the last word never ends it."""
    for block in _pcm_blocks(wav, ms=100):
        await session.send(type="input_audio_buffer.append", audio=base64.b64encode(block).decode())
        await asyncio.sleep(0.1)
    for _ in range(int(tail_s * 10)):
        await session.send(
            type="input_audio_buffer.append", audio=base64.b64encode(_SILENCE_100MS).decode()
        )
        await asyncio.sleep(0.1)


def _text_deltas(session: Any) -> list[dict[str, Any]]:
    return [f for f in session.frames if f.get("type") in _TEXT_DELTA_KINDS]


async def _wait_frame(session: Any, kind: str, *, timeout: float) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        found = session.of(kind)
        if found:
            first: dict[str, Any] = found[0]
            return first
        await asyncio.sleep(0.02)
    return None


@pytest.mark.provider_a
async def test_dashscope_cancel_reaches_her_own_turn_and_text_leads_audio(tmp_path: Path) -> None:
    """§5.8 #4. Feed a line addressed to the audience with the marker contract
    in the session, cancel at the first transcript character, and read off:
    whether text really leads audio (and by how much), whether audio stops,
    and what she wrote at the head."""
    gen, session = await _dashscope_session(tmp_path)
    try:
        wav = speech_wav(tmp_path, _AUDIENCE_LINE)
        started = time.monotonic()
        await _feed(session, wav)
        assert await _wait_frame(
            session, "input_audio_buffer.speech_started", timeout=10.0
        ), "服务端没把这段波形当成人声"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        while loop.time() < deadline and not _text_deltas(session):
            await asyncio.sleep(0.02)
        first_text = _text_deltas(session)[0] if _text_deltas(session) else None
        t_text = time.monotonic()
        audio_before = len(session.of("response.audio.delta"))
        await session.send(type="response.cancel")
        t_cancel = time.monotonic()
        await session.settle(5.0)
        audio_after = len(session.of("response.audio.delta")) - audio_before
        head = "".join(str(f.get("delta") or "") for f in _text_deltas(session))
        dones = session.of("response.done")
        _probe(
            "dashscope.cancel_implicit",
            first_text_seen=first_text is not None,
            text_lead_ms=(
                None if not session.of("response.audio.delta") else round((t_text - started) * 1000)
            ),
            audio_deltas_before_cancel=audio_before,
            audio_deltas_after_cancel=audio_after,
            cancel_to_done_ms=None if not dones else round((time.monotonic() - t_cancel) * 1000),
            done_status=[(d.get("response") or {}).get("status") for d in dones],
            head=head[:40],
            head_is_marker=head.lstrip().startswith("["),
            frame_types=sorted({str(f.get("type")) for f in session.frames}),
        )
        # What the history kept: ask her, in band, in text.
        await session.send(
            type="conversation.item.create",
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "你上一句原话是什么？只复述，不解释"}],
            },
        )
        await session.settle(0.6)
        await session.send(
            type="response.create",
            response={"modalities": ["text"], "max_output_tokens": 80},
        )
        await session.settle(8.0)
        texts = [
            str(f.get("text") or f.get("delta") or "") for f in session.of("response.text.done")
        ]
        _probe("dashscope.history_after_cancel", she_says=texts[-1][:80] if texts else None)
        assert dones, "没等到 response.done"
    finally:
        await gen.aclose()


@pytest.mark.provider_a
async def test_dashscope_input_transcription_can_be_asked_for(tmp_path: Path) -> None:
    """§5.8 #5. We never set input_audio_transcription; does the session take
    it, and does a transcript of the streamer come back?"""
    gen, session = await _dashscope_session(
        tmp_path, extra={"input_audio_transcription": {"model": "gummy-realtime-v1"}}
    )
    try:
        updated = session.of("session.updated")
        echoed = (
            (updated[-1].get("session") or {}).get("input_audio_transcription") if updated else None
        )
        errors = session.of("error")
        wav = speech_wav(tmp_path, _TO_HER_LINE)
        await _feed(session, wav)
        await session.settle(12.0)
        transcripts = session.of("conversation.item.input_audio_transcription.completed")
        _probe(
            "dashscope.input_transcription",
            echoed=echoed,
            errors=[(e.get("error") or {}).get("code") for e in errors],
            transcript_frames=len(transcripts),
            transcript=(transcripts[0].get("transcript") or "")[:40] if transcripts else None,
            frame_types=sorted({str(f.get("type")) for f in session.frames}),
        )
        assert session.of("input_audio_buffer.speech_started"), "服务端没把这段波形当成人声"
    finally:
        await gen.aclose()


@pytest.mark.provider_a
async def test_dashscope_refuses_a_second_create_while_one_generates(tmp_path: Path) -> None:
    """§5.8 #10. The single-slot conclusion was measured on qwen3.5-omni once
    by hand (capabilities.py); this pins it on whatever model the endpoint
    URL names, in band and with conversation none."""
    gen, session = await _dashscope_session(tmp_path)
    try:
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
            response={"modalities": ["text", "audio"], "max_output_tokens": 1200},
        )
        assert await _wait_frame(
            session, "response.audio_transcript.delta", timeout=15.0
        ), "第一条回复没开始"
        for label, response in (
            ("in_band", {"modalities": ["text"]}),
            (
                "out_of_band",
                {"conversation": "none", "modalities": ["text"], "instructions": "说一个字"},
            ),
        ):
            created_before = len(session.of("response.created"))
            errors_before = len(session.of("error"))
            dones_before = len(session.of("response.done"))
            await session.send(type="response.create", response=response)
            await session.settle(2.5)
            errors = session.of("error")[errors_before:]
            _probe(
                "dashscope.second_create." + label,
                first_reply_still_running=len(session.of("response.done")) == dones_before,
                created=len(session.of("response.created")) - created_before,
                error_codes=[(e.get("error") or {}).get("code") for e in errors],
                error_messages=[str((e.get("error") or {}).get("message"))[:80] for e in errors],
            )
        await session.send(type="response.cancel")
        await session.settle(2.0)
    finally:
        await gen.aclose()


# ------------------------------------------------------------ Volcano


@pytest.mark.provider_a
async def test_volcano_text_leads_audio_and_interrupt_at_first_text(tmp_path: Path) -> None:
    """§5.8 #6 and #7. Her own VAD turn on the shipped generation: which
    frame comes first, how far apart, how much audio follows a ClientInterrupt
    sent at the first text, and what the dialogue remembers afterwards."""
    from tests.integration.test_volcano_contract import GENERATIONS, _link

    async for volcano in _link(**GENERATIONS["O2.0"]):
        await volcano.set_context(_instructions())
        stamped: list[tuple[float, link.LinkEvent]] = []
        collected: list[link.LinkEvent] = []
        task = asyncio.create_task(_stamp_into(volcano, stamped, collected))
        try:
            wav = speech_wav(tmp_path, _AUDIENCE_LINE)
            for block in _pcm_blocks(wav, ms=20):
                await volcano.push_audio(block)
                await asyncio.sleep(0.02)
            for _ in range(150):  # 3 s of silence so the server closes the turn
                await volcano.push_audio(b"\x00" * 640)
                await asyncio.sleep(0.02)
            # Wait for the first text of her turn, then cut it.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20.0
            handle: link.ReplyHandle | None = None
            while loop.time() < deadline and handle is None:
                for _, event in stamped:
                    if isinstance(event, link.ReplyTextDelta):
                        handle = event.handle
                        break
                await asyncio.sleep(0.02)
            errors = [(e.code, e.detail[:120]) for e in collected if isinstance(e, link.LinkError)]
            assert handle is not None, (
                f"20 秒没等到她的文字：{sorted({type(e).__name__ for e in collected})}，"
                f"错误：{errors}"
            )
            audio_before = sum(isinstance(e, link.ReplyAudioDelta) for e in collected)
            await volcano.cancel(handle)
            await asyncio.sleep(4.0)
            times = {
                kind: next((t for t, e in stamped if isinstance(e, kind)), None)
                for kind in (link.ReplyStarted, link.ReplyTextDelta, link.ReplyAudioDelta)
            }
            t_text, t_audio = times[link.ReplyTextDelta], times[link.ReplyAudioDelta]
            head = "".join(e.text for e in collected if isinstance(e, link.ReplyTextDelta))
            dones = [e for e in collected if isinstance(e, link.ReplyDone)]
            _probe(
                "volcano.order_and_interrupt",
                first_after_start=next(
                    (
                        type(e).__name__
                        for _, e in stamped
                        if isinstance(e, link.ReplyTextDelta | link.ReplyAudioDelta)
                    ),
                    None,
                ),
                text_to_audio_ms=(
                    None if t_text is None or t_audio is None else round((t_audio - t_text) * 1000)
                ),
                audio_before_cancel=audio_before,
                audio_after_cancel=sum(isinstance(e, link.ReplyAudioDelta) for e in collected)
                - audio_before,
                done_status=[str(d.status) for d in dones],
                head=head[:40],
                head_is_marker=head.lstrip().startswith("["),
            )
            # #7: what the dialogue kept.
            collected.clear()
            await volcano.request_reply(
                link.ReplySpec(instructions="你上一句原话是什么？只复述，不解释")
            )
            await asyncio.sleep(10.0)
            dones = [e for e in collected if isinstance(e, link.ReplyDone)]
            _probe(
                "volcano.history_after_interrupt", she_says=dones[-1].text[:80] if dones else None
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _stamp_into(
    volcano: Any, stamped: list[tuple[float, link.LinkEvent]], collected: list[link.LinkEvent]
) -> None:
    """Every event with its arrival time. Module-level so the lists it writes
    are arguments rather than names captured from a loop body."""
    async for event in volcano.events():
        stamped.append((time.monotonic(), event))
        collected.append(event)


# ------------------------------------------------------------ s2s


async def _s2s_ws() -> Any:
    from tests.integration.test_real_server import SERVER_URL, _server_is_up

    if not _server_is_up():
        pytest.skip(
            "没有跑着的 s2s 服务器（127.0.0.1:8765）。起法见 scripts/make_official_pipe_config.py"
        )
    # One pipeline, and the slot is released a moment after the previous
    # connection closes (test_real_server's ws fixture): retry on the limit
    # error rather than fail the second probe on the first one's tail.
    deadline = asyncio.get_running_loop().time() + 30.0
    while True:
        ws = await websockets.connect(SERVER_URL, max_size=16 * 1024 * 1024)
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
        except websockets.ConnectionClosed:
            first = {"error": {"type": "session_limit_reached"}}
        if first.get("type") == "session.created":
            break
        await ws.close()
        limit = (first.get("error") or {}).get("type") == "session_limit_reached"
        assert limit and asyncio.get_running_loop().time() < deadline, first
        await asyncio.sleep(1.0)
    # The GA server validates the body against the OpenAI session models,
    # which need `type: realtime` (the codec's session_update adds it too).
    await ws.send(
        json.dumps(
            {
                "type": "session.update",
                "session": {"type": "realtime", "instructions": _instructions()},
            }
        )
    )
    await asyncio.sleep(0.5)
    return ws


async def _s2s_turn(
    ws: Any, text: str, *, cancel_at: str | None = None
) -> tuple[list[dict[str, Any]], float | None]:
    """Speak one line and collect until a done; optionally cancel at the
    first frame of `cancel_at`. Returns the frames and the cancel timestamp."""
    from tests.integration.test_real_server import _append, _events_until, _silence, _speech

    await _append(ws, _speech(text))
    await _append(ws, _silence(1600))
    frames: list[dict[str, Any]] = []
    cancelled_at: float | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90.0
    while loop.time() < deadline:
        stop = {"response.done"} | ({cancel_at} if cancel_at and cancelled_at is None else set())
        chunk = await _events_until(ws, stop, timeout=deadline - loop.time())
        frames.extend(chunk)
        if cancel_at and cancelled_at is None and any(f.get("type") == cancel_at for f in chunk):
            await ws.send(json.dumps({"type": "response.cancel"}))
            cancelled_at = time.monotonic()
            continue
        if any(f.get("type") == "response.done" for f in chunk):
            break
    return frames, cancelled_at


# The GA dialect's names, which the running s2s server speaks (dialect.py's
# GA table); the beta names are DashScope's.
_S2S_TRANSCRIPT_DONE = "response.output_audio_transcript.done"
_S2S_AUDIO_DELTA = "response.output_audio.delta"
_S2S_TEXT_DELTA = "response.output_text.delta"


def _s2s_summary(frames: list[dict[str, Any]]) -> dict[str, Any]:
    types = [str(f.get("type")) for f in frames]
    reply_kinds = [
        t for t in types if t in (_S2S_TRANSCRIPT_DONE, _S2S_AUDIO_DELTA, _S2S_TEXT_DELTA)
    ]
    transcript = "".join(
        str(f.get("transcript") or "") for f in frames if f.get("type") == _S2S_TRANSCRIPT_DONE
    ) or "".join(str(f.get("delta") or "") for f in frames if f.get("type") == _S2S_TEXT_DELTA)
    return {
        "first_reply_frame": reply_kinds[0] if reply_kinds else None,
        "transcript_done_count": types.count(_S2S_TRANSCRIPT_DONE),
        "audio_deltas": types.count(_S2S_AUDIO_DELTA),
        "done_status": [
            (f.get("response") or {}).get("status")
            for f in frames
            if f.get("type") == "response.done"
        ],
        "head": transcript[:40],
        "head_is_marker": transcript.lstrip().startswith("["),
        "frame_types": sorted(set(types)),
        "errors": [
            str((f.get("error") or {}).get("message"))[:80]
            for f in frames
            if f.get("type") == "error"
        ],
    }


@pytest.mark.integration
async def test_s2s_implicit_turn_text_first_and_marker_at_head() -> None:
    """§5.8 #1: on the running s2s configuration, does her own turn send its
    text before its audio, in how many transcript.done fragments, and does the
    marker contract in the session make her write one for an audience line?"""
    ws = await _s2s_ws()
    try:
        frames, _ = await _s2s_turn(ws, _AUDIENCE_LINE)
        summary = _s2s_summary(frames)
        _probe("s2s.implicit_frames", **summary)
        assert summary["done_status"], "没等到 response.done"
    finally:
        await ws.close()


@pytest.mark.integration
async def test_s2s_cancel_at_first_transcript_and_speak_again() -> None:
    """§5.8 #2 and #3: cancel at the first transcript fragment, then speak a
    line addressed to her right away. Whether the cancel rolled the history
    back is on the server's terminal (LLM generation cancelled / Rolled back
    failed generation) — read it there; here we learn how fast the done comes
    and whether the next turn still completes with text."""
    ws = await _s2s_ws()
    try:
        frames, cancelled_at = await _s2s_turn(ws, _AUDIENCE_LINE, cancel_at=_S2S_TRANSCRIPT_DONE)
        first = _s2s_summary(frames)
        done_at = time.monotonic()
        _probe(
            "s2s.cancel_at_first_transcript",
            cancel_sent=cancelled_at is not None,
            cancel_to_done_ms=(
                None if cancelled_at is None else round((done_at - cancelled_at) * 1000)
            ),
            **first,
        )
        frames2, _ = await _s2s_turn(ws, _TO_HER_LINE)
        second = _s2s_summary(frames2)
        _probe("s2s.next_turn_after_cancel", **second)
        assert second["done_status"], "取消之后的下一句没等到 response.done"
    finally:
        await ws.close()
