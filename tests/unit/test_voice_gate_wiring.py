"""The voice gate wired the way dev-talk wires it, against the fake server.

fan-out → gate → scheduler kill path, on both shipped shapes: the s2s text-
first audio reply and DashScope's character-by-character transcript. The
scheduler reads the raw view (it must close the floor the instant a turn
starts); the "speakers" here read the gated view, and what they collect is
the proof — a turn not for her leaves no audio there.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from bilisama.clock import SystemClock
from bilisama.config.enums import ProviderName, VoiceReplyMode
from bilisama.dev_talk import _Fanout
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.scheduler import PlaybackClear, Scheduler
from bilisama.director.turn_protocol import TurnPolicy
from bilisama.director.voice_turn import Skip, VoiceTurnGate
from bilisama.obs.outcome import Outcome, Phase, SkipReason
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.providers.hosted import HostedLink
from bilisama.realtime.providers.s2s import S2SLink
from tests.fakes.mock_realtime import MockRealtimeServer, Script

_S2S_TTS = replace(caps_mod.S2S, owns_tts=True)


@dataclass
class _Rig:
    scheduler: Scheduler
    floor: SpeakingFloor
    server: MockRealtimeServer
    gate: VoiceTurnGate
    gated: list[link.LinkEvent] = field(default_factory=list)
    raw: list[link.LinkEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@contextlib.asynccontextmanager
async def _wired(
    script: Script,
    *,
    hosted: bool = False,
    hold_max_s: float = 0.6,
) -> AsyncIterator[_Rig]:
    clock = SystemClock()
    caps = caps_mod.DASHSCOPE if hosted else _S2S_TTS
    codec = dia.BETA if hosted else None
    async with MockRealtimeServer(caps=caps, codec=codec, script=script) as server:
        inner: link.SpeechLink = (
            HostedLink(server.url, ProviderName.DASHSCOPE) if hosted else S2SLink(server.url)
        )
        await inner.connect()
        fan = _Fanout(inner)
        floor = SpeakingFloor(clock)
        scheduler = Scheduler(fan, floor, clock)
        rig = _Rig(scheduler=scheduler, floor=floor, server=server, gate=None)  # type: ignore[arg-type]

        def on_skip(skip: Skip) -> None:
            # dev_talk.on_voice_skip, minus the proactive loop.
            ruling = skip.ruling
            scheduler.skip_implicit(
                skip.handle,
                reason=SkipReason.VOICE_NOT_ADDRESSED,
                detail=ruling.detail() if ruling is not None else "残记号",
                clear_playback=skip.clear_playback,
            )
            if ruling is not None and ruling.note:
                rig.notes.append(f"[主播{ruling.label}] {ruling.note}")

        gate = VoiceTurnGate(
            clock,
            policy=TurnPolicy(),
            mode=VoiceReplyMode.WHEN_ADDRESSED,
            on_skip=on_skip,
            hold_max_s=hold_max_s,
        )
        rig.gate = gate
        fan.set_gate(gate)
        # Views registered before anything flows.
        gated_view, raw_view = fan.gated_events(), fan.events()
        fan.start()

        async def collect(view: AsyncIterator[link.LinkEvent], into: list[link.LinkEvent]) -> None:
            async for event in view:
                into.append(event)

        tasks = [
            asyncio.create_task(scheduler.run()),
            asyncio.create_task(collect(gated_view, rig.gated)),
            asyncio.create_task(collect(raw_view, rig.raw)),
        ]
        try:
            yield rig
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fan.aclose()


async def _until(predicate: object, *, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"等了 {timeout}s 也没等到：{what}")


def _audio_frames(events: list[link.LinkEvent]) -> list[link.ReplyAudioDelta]:
    return [e for e in events if isinstance(e, link.ReplyAudioDelta)]


def _drain(scheduler: Scheduler) -> list[PlaybackClear]:
    out: list[PlaybackClear] = []
    while not scheduler.controls.empty():
        out.append(scheduler.controls.get_nowait())
    return out


async def test_a_turn_not_for_her_never_reaches_the_speakers() -> None:
    script = Script(reply_text="[READING] 主播在念弹幕", delta_chunks=2, delta_interval_s=0.05)
    async with _wired(script) as rig:
        await rig.server.emit_implicit_reply()
        await _until(lambda: len(rig.scheduler.verdicts) == 1, what="门的判决")
        verdict = rig.scheduler.verdicts[0]
        assert verdict.source == "voice"
        assert (verdict.outcome, verdict.phase) == (Outcome.SKIPPED, Phase.GENERATING)
        assert verdict.reason is SkipReason.VOICE_NOT_ADDRESSED
        assert verdict.detail == "READING · 主播在念弹幕"
        await _until(lambda: not rig.floor.implicit_active, what="地板放开")
        await asyncio.sleep(0.1)  # let every late frame land in both views
        assert rig.server.recorded.count("response.cancel") == 1
        assert _audio_frames(rig.gated) == [], "nothing of the turn reached the speakers"
        assert not any(isinstance(e, link.ReplyTextDelta) for e in rig.gated)
        assert not any(isinstance(e, link.ReplyDone) for e in rig.gated), "held turn: no end"
        assert any(isinstance(e, link.ReplyStarted) for e in rig.gated), "the start passes"
        assert _audio_frames(rig.raw), "the raw view (the scheduler's) saw the audio"
        assert rig.scheduler.status()["implicit_active"] is False
        assert _drain(rig.scheduler) == [], "nothing played, nothing to flush"
        assert rig.notes == ["[主播念弹幕] 主播在念弹幕"]


async def test_a_turn_for_her_reaches_the_speakers_whole() -> None:
    script = Script(reply_text="好的，我看到了。", delta_chunks=3)
    async with _wired(script) as rig:
        await rig.server.emit_implicit_reply()
        await _until(lambda: any(isinstance(e, link.ReplyDone) for e in rig.gated), what="回复播完")
        await asyncio.sleep(0.05)
        assert rig.gated == rig.raw, "a plain head: the speakers see exactly what the link sent"
        assert rig.scheduler.verdicts == []
        assert rig.server.recorded.count("response.cancel") == 0
        assert rig.gate.status()["passed"] == 1


async def test_the_dashscope_shape_decides_on_the_closing_bracket() -> None:
    # 14 characters over 8 chunks: the fake splits into one-character deltas,
    # so the decision has to land on the `]` and not before.
    script = Script(reply_text="[AUDIENCE] 主播在讲故事", delta_chunks=8, delta_interval_s=0.01)
    async with _wired(script, hosted=True) as rig:
        await rig.server.emit_implicit_reply()
        await _until(lambda: len(rig.scheduler.verdicts) == 1, what="门的判决")
        verdict = rig.scheduler.verdicts[0]
        assert verdict.reason is SkipReason.VOICE_NOT_ADDRESSED
        assert verdict.detail.startswith("AUDIENCE")
        await _until(lambda: not rig.floor.implicit_active, what="地板放开")
        await asyncio.sleep(0.1)
        assert rig.server.recorded.count("response.cancel") == 1
        assert _audio_frames(rig.gated) == []
        assert _audio_frames(
            rig.raw
        ), "the raw view saw the audio the fake streamed before the cancel"


async def test_audio_ahead_of_its_text_is_released_then_cut_on_the_late_marker() -> None:
    script = Script(reply_text="[SELF_TALK] 嘀咕", delta_chunks=1, text_lag_s=0.5)
    async with _wired(script, hold_max_s=0.15) as rig:
        await rig.server.emit_implicit_reply()
        await _until(lambda: len(_audio_frames(rig.gated)) == 1, what="超时放行的音频")
        assert rig.gate.status()["timeouts"] == 1
        assert rig.scheduler.verdicts == [], "nothing to decide yet"
        await _until(lambda: len(rig.scheduler.verdicts) == 1, what="迟到记号的判决")
        verdict = rig.scheduler.verdicts[0]
        assert verdict.reason is SkipReason.VOICE_NOT_ADDRESSED
        assert verdict.detail == "SELF_TALK · 嘀咕"
        assert rig.gate.status()["late_markers"] == 1
        assert _drain(rig.scheduler) == [PlaybackClear(reason="voice_not_addressed")]
        await _until(lambda: not rig.floor.implicit_active, what="地板放开")
        # The fake ends the reply right behind the late text, so the cancel
        # usually finds the record already settled and stays home — which is
        # the link's contract (client.py cancel: a finished reply is not
        # cancelled, that frame would kill the next one). Either way at most
        # one frame goes out, and the flush above is what protects the room.
        assert rig.server.recorded.count("response.cancel") <= 1
        assert rig.scheduler.status()["implicit_active"] is False


async def test_an_intent_waits_while_her_turn_is_held() -> None:
    script = Script(reply_text="好的，我看到了。", delta_chunks=4, delta_interval_s=0.05)
    async with _wired(script) as rig:
        await rig.server.emit_implicit_reply()
        await _until(lambda: rig.floor.implicit_active, what="她自起的回复开始")
        rig.scheduler.submit(
            Intent(
                source="danmaku",
                priority=Priority.DANMAKU,
                injection=Injection(
                    reply=link.ReplySpec(instructions="回一句"), item_text="[弹幕] 观众A: 你好"
                ),
            )
        )
        await asyncio.sleep(0.05)
        assert rig.server.recorded.count("response.create") == 0, "held behind her own turn"
        await _until(lambda: len(rig.scheduler.verdicts) == 1, what="弹幕回复的判决")
        assert rig.scheduler.verdicts[0].outcome is Outcome.SPOKEN
        assert rig.server.recorded.count("response.create") == 1
