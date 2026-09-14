"""One-generation non-spoken reports cannot leak across host turns or sessions."""

import asyncio
import json

import pytest

from bilisama.clock import FakeClock
from bilisama.director.interaction_reports import InteractionReports
from bilisama.director.interaction_state import REPORT_NAME, InteractionReport
from bilisama.realtime import link


class ReportingLink:
    def __init__(self) -> None:
        self.results: list[str] = []
        self.wait = asyncio.Event()
        self.wait.set()

    async def configure_tools(self, tools: tuple[link.ToolSpec, ...]) -> None:
        pass

    async def submit_tool_result(self, call: link.ToolCall, output: str) -> None:
        await self.wait.wait()
        self.results.append(output)


def make_report(
    handle: link.ReplyHandle, *, silence: str = "keep", call_id: str = "one"
) -> link.ToolCall:
    return link.ToolCall(
        handle,
        call_id,
        REPORT_NAME,
        json.dumps(
            {
                "events": [],
                "silence": silence,
                "discussion": {"action": "keep", "topic": ""},
            }
        ),
        session_id="socket-one",
    )


class Kit:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.link = ReportingLink()
        self.applied: list[InteractionReport] = []
        self.starts: list[float | None] = []
        self.silenced = False
        self.notices: list[str] = []
        self.refreshes = 0
        self.reports = InteractionReports(
            self.link,
            self.clock,
            apply=self.apply,
            refresh_context=self.refresh,
            notice=self.notices.append,
        )

    def apply(self, report: InteractionReport, started_at: float | None) -> None:
        self.applied.append(report)
        self.starts.append(started_at)
        if report.silence != "keep":
            self.silenced = report.silence == "enter"

    async def refresh(self) -> None:
        self.refreshes += 1

    def voice(self) -> link.ReplyHandle:
        self.reports.observe(link.SpeechStarted())
        return link.ReplyHandle(implicit=True, input_generation=self.reports.input_generation)


@pytest.mark.asyncio
async def test_tool_first_is_applied_without_delaying_normal_media() -> None:
    kit = Kit()
    handle = kit.voice()
    kit.link.wait.clear()
    kit.reports.observe(make_report(handle))
    assert len(kit.applied) == 1
    kit.reports.observe(link.ReplyStarted(handle))
    kit.reports.observe(link.ReplyTextDelta(handle, "我选第一步。"))
    assert kit.link.results == []
    kit.link.wait.set()
    await kit.reports.drain()
    assert len(kit.link.results) == 1
    assert kit.refreshes >= 1
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_old_report_after_new_host_speech_is_rejected() -> None:
    kit = Kit()
    old = kit.voice()
    kit.voice()
    kit.reports.observe(make_report(old, silence="enter"))
    assert kit.applied == []
    assert kit.reports.status()["stale"] == 1
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_unbound_and_stale_handles_cannot_update_state() -> None:
    kit = Kit()
    kit.reports.observe(make_report(link.ReplyHandle(implicit=True)))
    handle = kit.voice()
    handle.stale = True
    kit.reports.observe(make_report(handle, call_id="two"))
    assert kit.applied == []
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_duplicate_tool_and_second_report_in_same_generation_are_idempotent() -> None:
    kit = Kit()
    handle = kit.voice()
    call = make_report(handle)
    kit.reports.observe(call)
    kit.reports.observe(call)
    kit.reports.observe(make_report(handle, silence="enter", call_id="two"))
    assert len(kit.applied) == 1
    await kit.reports.drain()
    await kit.reports.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["先检查日志。", "[SKIP] 主播在讲解", "[SKIP] 主播自言自语"])
async def test_no_state_change_needs_no_report_or_warning(text: str) -> None:
    kit = Kit()
    handle = kit.voice()
    kit.reports.observe(link.ReplyStarted(handle))
    kit.reports.observe(link.ReplyDone(handle, link.ReplyStatus.COMPLETED, text))
    assert kit.reports.status().get("missing", 0) == 0
    assert kit.reports.status()["without_report"] == 1
    assert kit.applied == []
    assert kit.notices == []
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_invalid_report_does_not_crash_audio_consumer() -> None:
    kit = Kit()
    handle = kit.voice()
    kit.reports.observe(link.ToolCall(handle, "bad", REPORT_NAME, "{}", "socket-one"))
    assert kit.applied == []
    assert kit.reports.status()["invalid"] == 1
    await kit.reports.drain()
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_reset_cancels_blocked_ack_and_rejects_old_handle() -> None:
    kit = Kit()
    handle = kit.voice()
    kit.link.wait.clear()
    kit.reports.observe(make_report(handle))
    await asyncio.sleep(0)
    await kit.reports.reset()
    kit.link.wait.set()
    kit.reports.observe(make_report(handle, call_id="late"))
    await kit.reports.drain()
    assert kit.link.results == []
    assert len(kit.applied) == 1
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_positive_voice_output_does_not_infer_a_background_state_change() -> None:
    kit = Kit()
    kit.silenced = True
    handle = kit.voice()
    kit.reports.observe(link.ReplyTextDelta(handle, "先"))
    kit.reports.observe(link.ReplyTextDelta(handle, "检查日志。"))
    assert bool(kit.silenced) is True
    event_handle = link.ReplyHandle(input_generation=kit.reports.input_generation)
    kit.reports.observe(link.ReplyTextDelta(event_handle, "欢迎。"))
    assert bool(kit.silenced) is True
    kit.reports.observe(make_report(handle, silence="release"))
    assert bool(kit.silenced) is False
    await kit.reports.drain()
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_explicit_silence_report_wins_over_body_in_same_turn() -> None:
    kit = Kit()
    handle = kit.voice()
    kit.reports.observe(make_report(handle, silence="enter"))
    kit.reports.observe(link.ReplyTextDelta(handle, "好。"))
    assert kit.silenced is True
    await kit.reports.drain()
    await kit.reports.aclose()


@pytest.mark.asyncio
async def test_unexpected_adapter_failure_is_observed_without_stopping_receive_loop() -> None:
    class AdapterFailure(Exception):
        pass

    class FailingLink(ReportingLink):
        async def submit_tool_result(self, call: link.ToolCall, output: str) -> None:
            raise AdapterFailure("连接已经关闭")

    kit = Kit()
    kit.reports._speech = FailingLink()
    kit.reports.observe(make_report(kit.voice()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert kit.reports.status()["ack_failed"] == 1
    assert len(kit.applied) == 1
    await kit.reports.aclose()
