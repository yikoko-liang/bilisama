"""Exercise same-response speech and reports through the real local socket stack.

The fake server supplies model decisions; these tests prove transport and
scheduling, not the semantic accuracy of those decisions on real speech.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName, VoiceReplyMode
from bilisama.dev_talk import _Fanout
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intents import _event_ref
from bilisama.director.interaction_reports import InteractionReports
from bilisama.director.interaction_state import (
    REPORT_NAME,
    DiscussionUpdate,
    EventUpdate,
    InteractionReport,
    InteractionState,
    report_tool_spec,
)
from bilisama.director.scheduler import Scheduler
from bilisama.director.turn_protocol import TurnPolicy
from bilisama.director.voice_turn import Skip, VoiceTurnGate
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.obs.outcome import SkipReason
from bilisama.realtime import capabilities, dialect, link
from bilisama.realtime.providers.hosted import HostedLink
from tests.fakes.mock_realtime import MockRealtimeServer
from tests.unit.conftest import AssemblyKit, build_assembly_kit

_PCM = bytes(960)


@dataclass
class _Rig:
    kit: AssemblyKit
    server: MockRealtimeServer
    scheduler: Scheduler
    state: InteractionState
    reports: InteractionReports
    gate: VoiceTurnGate
    raw: list[link.LinkEvent] = field(default_factory=list)
    gated: list[link.LinkEvent] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def _collect(events: AsyncIterator[link.LinkEvent], into: list[link.LinkEvent]) -> None:
    async for event in events:
        into.append(event)


@asynccontextmanager
async def _wired(tmp_path: Path) -> AsyncIterator[_Rig]:
    state = InteractionState(FakeClock())
    kit = build_assembly_kit(tmp_path, interaction_state=state)
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = HostedLink(server.url, ProviderName.DASHSCOPE, session_cap_min=0)
        await hosted.configure_tools((report_tool_spec(),))
        await hosted.connect()
        fan = _Fanout(hosted)
        scheduler = Scheduler(fan, SpeakingFloor(kit.clock), kit.clock, interaction_state=state)
        # Keep the real Assembly intake and report application. Only replace
        # the fixture's collecting sinks with production-shaped wiring.
        kit.assembly._submit = scheduler.submit
        kit.assembly._push_context = fan.set_context
        notices: list[str] = []

        def apply(report: InteractionReport, started_at: float | None) -> None:
            kit.assembly.apply_interaction_report(report, voice_started_at=started_at)
            scheduler.refresh_interactions()

        async def refresh_context() -> None:
            await kit.assembly.refresh_context()

        reports = InteractionReports(
            hosted,
            kit.clock,
            apply=apply,
            refresh_context=refresh_context,
            notice=notices.append,
        )

        def on_skip(skip: Skip) -> None:
            scheduler.skip_reply(
                skip.handle,
                detail=skip.ruling.detail() if skip.ruling is not None else "残记号",
                clear_playback=skip.clear_playback,
                preserve_generation=True,
            )

        gate = VoiceTurnGate(
            kit.clock,
            policy=TurnPolicy(),
            mode=VoiceReplyMode.WHEN_ADDRESSED,
            on_skip=on_skip,
        )
        fan.set_gate(gate)
        fan.set_reports(reports)
        rig = _Rig(kit, server, scheduler, state, reports, gate, notices=notices)
        raw_view, gated_view = fan.events(), fan.gated_events()
        tasks = [
            asyncio.create_task(scheduler.run()),
            asyncio.create_task(_collect(raw_view, rig.raw)),
            asyncio.create_task(_collect(gated_view, rig.gated)),
        ]
        fan.start()
        try:
            await _until(lambda: len(fan._sinks) == 2)
            await _until(lambda: server.recorded.count("session.update") == 1)
            yield rig
        finally:
            scheduler.panic_mute()
            await scheduler.drain_pending_io()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fan.aclose()
            kit.store.close()


async def _begin_voice(rig: _Rig, response_id: str) -> None:
    await rig.server.speech_started()
    await _until(lambda: rig.scheduler._floor.streamer_speaking)
    await rig.server.speech_stopped()
    await rig.server.send(dialect.ServerEvent.RESPONSE_CREATED, response={"id": response_id})


async def _text_and_audio(rig: _Rig, response_id: str, text: str) -> None:
    await rig.server.send(dialect.ServerEvent.TRANSCRIPT_DELTA, response_id=response_id, delta=text)
    await rig.server.send(
        dialect.ServerEvent.AUDIO_DELTA,
        response_id=response_id,
        delta=base64.b64encode(_PCM).decode(),
    )
    await _until(lambda: any(isinstance(event, link.ReplyAudioDelta) for event in rig.raw))


async def _report(rig: _Rig, response_id: str, report: InteractionReport) -> None:
    await rig.server.send(
        dialect.ServerEvent.FUNCTION_ARGS_DONE,
        response_id=response_id,
        call_id=f"call-{response_id}",
        name=REPORT_NAME,
        arguments=report.model_dump_json(),
    )
    await _until(lambda: rig.reports.status()["applied"] == 1)


async def _done_and_ack(rig: _Rig, response_id: str) -> None:
    await rig.server.send(
        dialect.ServerEvent.RESPONSE_DONE,
        response={"id": response_id, "status": "completed"},
    )
    await _until(
        lambda: any(
            item.get("item", {}).get("type") == "function_call_output"
            for item in rig.server.recorded.events
        )
    )
    await rig.reports.drain()
    outputs = [
        item["item"]
        for item in rig.server.recorded.events
        if item.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == f"call-{response_id}"


async def test_skip_keeps_same_vad_report_alive_and_cancels_only_its_exact_event(
    tmp_path: Path,
) -> None:
    async with _wired(tmp_path) as rig:
        rid = "voice-skip-and-report"
        await _begin_voice(rig, rid)
        viewer = Viewer(uid=101, name="小松")
        answered = LiveEvent(
            kind=EventKind.DANMAKU, event_id="answered", viewer=viewer, text="任务会上云吗"
        )
        unanswered = LiveEvent(
            kind=EventKind.DANMAKU, event_id="unanswered", viewer=viewer, text="今晚开源吗"
        )
        for event in (answered, unanswered):
            await rig.kit.assembly.on_event(event)
        assert rig.scheduler.status()["queued"] == 2
        await _text_and_audio(rig, rid, "[SKIP] 主播已经回答任务只保存在本地")
        await rig.kit.clock.advance(0.35)
        await _until(lambda: rig.gate.status()["skipped"] == 1)
        assert rig.scheduler.status()["implicit_active"]
        assert rig.server.recorded.count("response.cancel") == 0
        assert rig.reports.status()["applied"] == 0
        assert not any(isinstance(event, link.ReplyAudioDelta) for event in rig.gated)

        await _report(
            rig,
            rid,
            InteractionReport(
                events=[
                    EventUpdate(
                        event_ref=_event_ref(answered),
                        state="handled",
                        evidence="任务只保存在本地，不会上传",
                    )
                ],
                silence="keep",
                discussion=DiscussionUpdate(action="keep", topic=""),
            ),
        )
        assert rig.state.is_handled(answered)
        assert not rig.state.is_handled(unanswered)
        assert rig.scheduler.status()["queued"] == 1
        assert rig.scheduler._heap[0].intent.event == unanswered.redacted()
        assert any(v.reason is SkipReason.HOST_HANDLED for v in rig.scheduler.verdicts)
        assert rig.server.recorded.count("conversation.item.create") == 0
        assert rig.server.recorded.count("response.create") == 0
        await _done_and_ack(rig, rid)
        await _until(lambda: not rig.scheduler.status()["implicit_active"])

        calls = [event for event in rig.raw if isinstance(event, link.ToolCall)]
        started = [event for event in rig.raw if isinstance(event, link.ReplyStarted)]
        assert len(calls) == len(started) == 1
        assert calls[0].handle is started[0].handle
        assert calls[0].handle.implicit and not calls[0].handle.stale
        assert rig.server.recorded.count("response.cancel") == 0
        assert rig.server.recorded.count("response.create") == 0
        assert not any(isinstance(event, link.ReplyAudioDelta) for event in rig.gated)
        assert not any(isinstance(event, link.ReplyTextDelta) for event in rig.gated)
        assert rig.reports.status()["without_report"] == 0
        assert rig.notices == []


async def test_spoken_body_passes_before_function_report_without_an_extra_wait(
    tmp_path: Path,
) -> None:
    async with _wired(tmp_path) as rig:
        rig.state.silenced = True
        assert rig.kit.proactive is not None
        rig.kit.proactive.set_silenced(True)
        rid = "voice-body-before-report"
        await _begin_voice(rig, rid)
        await _text_and_audio(rig, rid, "我选更简单的那个界面。")
        await _until(lambda: any(isinstance(event, link.ReplyAudioDelta) for event in rig.gated))
        # No tool frame or response.done has even been sent, and the fake
        # clock has not advanced: speech release cannot depend on either.
        assert rig.kit.clock.monotonic() == 0
        assert rig.reports.status()["applied"] == 0
        assert rig.reports.status()["pending_acks"] == 0
        assert not any(isinstance(event, (link.ToolCall, link.ReplyDone)) for event in rig.raw)
        assert bool(rig.state.silenced) is True
        text = "".join(event.text for event in rig.gated if isinstance(event, link.ReplyTextDelta))
        assert text == "我选更简单的那个界面。"
        assert [event.pcm for event in rig.gated if isinstance(event, link.ReplyAudioDelta)] == [
            _PCM
        ]

        await _report(
            rig,
            rid,
            InteractionReport(
                events=[],
                silence="release",
                discussion=DiscussionUpdate(action="keep", topic=""),
            ),
        )
        await _done_and_ack(rig, rid)
        assert bool(rig.state.silenced) is False
        assert rig.server.recorded.count("response.create") == 0
        assert rig.server.recorded.count("response.cancel") == 0
        assert rig.reports.status()["without_report"] == 0
        assert rig.notices == []


@pytest.mark.parametrize(
    ("text", "silenced"),
    [("先检查日志。", False), ("[SKIP] 主播在讲解", False), ("[SKIP] 主播在讲解", True)],
)
async def test_unchanged_turn_without_function_call_is_normal(
    tmp_path: Path, text: str, silenced: bool
) -> None:
    async with _wired(tmp_path) as rig:
        rig.state.silenced = silenced
        rid = "no-state-change"
        await _begin_voice(rig, rid)
        await _text_and_audio(rig, rid, text)
        await rig.server.send(
            dialect.ServerEvent.RESPONSE_DONE,
            response={"id": rid, "status": "completed"},
        )
        await _until(lambda: any(isinstance(event, link.ReplyDone) for event in rig.raw))
        await rig.reports.drain()
        assert rig.reports.status()["without_report"] == 1
        assert rig.reports.status()["applied"] == 0
        assert rig.reports.status()["invalid"] == 0
        assert rig.notices == []
        assert rig.state.silenced is silenced
        assert rig.server.recorded.count("response.create") == 0
        assert rig.server.recorded.count("response.cancel") == 0
        assert not any(
            item.get("item", {}).get("type") == "function_call_output"
            for item in rig.server.recorded.events
        )
        audible = [event for event in rig.gated if isinstance(event, link.ReplyAudioDelta)]
        assert bool(audible) is (not text.startswith("[SKIP]"))
