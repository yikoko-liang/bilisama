"""Tool reports stay separate from speech and cannot cross live sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from bilisama.config.enums import ProviderName
from bilisama.realtime import capabilities, dialect, link
from bilisama.realtime.providers.hosted import HostedLink
from tests.fakes.mock_realtime import MockRealtimeServer


def _tool() -> link.ToolSpec:
    return link.ToolSpec(
        name="report_interaction",
        description="只向后台报告互动状态，不播报",
        parameters={"type": "object", "properties": {"can_speak": {"type": "boolean"}}},
    )


def _hosted(server: MockRealtimeServer) -> HostedLink:
    return HostedLink(server.url, ProviderName.DASHSCOPE, session_cap_min=0)


async def _wait_count(server: MockRealtimeServer, kind: str, count: int) -> None:
    async with asyncio.timeout(2):
        while server.recorded.count(kind) < count:
            await asyncio.sleep(0.005)


async def _call(hosted: HostedLink) -> link.ToolCall:
    await hosted._client._dispatch(
        dialect.ServerEvent.FUNCTION_ARGS_DONE,
        {
            "response_id": "response-report",
            "call_id": "call-report",
            "name": "report_interaction",
            "arguments": '{"can_speak":false}',
        },
    )
    async with asyncio.timeout(2):
        async for event in hosted.events():
            if isinstance(event, link.ToolCall):
                return event
    raise AssertionError("missing tool call")


async def _done(hosted: HostedLink) -> None:
    await hosted._client._dispatch(
        dialect.ServerEvent.RESPONSE_DONE,
        {"response": {"id": "response-report", "status": "completed"}},
    )


def test_tool_spec_is_frozen_and_protocol_is_optional() -> None:
    spec = _tool()
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"  # type: ignore[misc]
    detached = spec.parameters
    detached["properties"]["changed"] = {"type": "string"}
    assert "changed" not in spec.parameters["properties"]
    assert isinstance(HostedLink("ws://unused", ProviderName.DASHSCOPE), link.ToolReportingLink)
    assert not isinstance(object(), link.ToolReportingLink)


async def test_tools_register_only_when_explicitly_configured_and_replay_on_reset() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            await _wait_count(server, "session.update", 1)
            expected = dialect.BETA.tool_spec(
                "report_interaction", _tool().description, dict(_tool().parameters)
            )
            assert server.recorded.events[0]["session"]["tools"] == [expected]
            await hosted.reset_conversation("只保留这一例背景")
            await _wait_count(server, "session.update", 3)
            tool_updates = [
                event["session"]["tools"]
                for event in server.recorded.events
                if event.get("type") == "session.update" and "tools" in event["session"]
            ]
            assert tool_updates == [[expected], [expected]]
            assert server.recorded.count("response.create") == 0
        finally:
            await hosted.aclose()


async def test_unconfigured_tools_do_not_change_the_bootstrap() -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    assert hosted._bootstrap_frame() is None


async def test_configuring_tools_copies_schema_and_can_clear_live_configuration() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        parameters: dict[str, Any] = {"type": "object", "properties": {}}
        spec = link.ToolSpec("report_interaction", "后台状态", parameters)
        await hosted.configure_tools((spec,))
        parameters["properties"]["unexpected"] = {"type": "string"}
        await hosted.connect()
        try:
            await _wait_count(server, "session.update", 1)
            sent = server.recorded.events[0]["session"]["tools"][0]
            assert sent["function"]["parameters"]["properties"] == {}
            await hosted.configure_tools(())
            await _wait_count(server, "session.update", 2)
            assert server.recorded.events[-1]["session"]["tools"] == []
        finally:
            await hosted.aclose()


async def test_report_result_waits_for_done_and_never_starts_another_inference() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            call = await _call(hosted)
            assert call.session_id
            task = asyncio.create_task(hosted.submit_tool_result(call, '{"recorded":true}'))
            await asyncio.sleep(0)
            assert not task.done()
            assert server.recorded.count("conversation.item.create") == 0
            await _done(hosted)
            async with asyncio.timeout(2):
                await task
            await _wait_count(server, "conversation.item.create", 1)
            item = server.recorded.events[-1]["item"]
            assert item == {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": '{"recorded":true}',
            }
            await hosted.submit_tool_result(call, '{"recorded":true}')
            await asyncio.sleep(0.01)
            assert server.recorded.count("conversation.item.create") == 1
            assert server.recorded.count("response.create") == 0
        finally:
            await hosted.aclose()


@pytest.mark.parametrize("transition", ["cancel", "reset", "suspend", "resume"])
async def test_stale_tool_result_is_not_written(transition: str) -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            call = await _call(hosted)
            await _done(hosted)
            if transition == "cancel":
                call.handle.stale = True
            elif transition == "reset":
                await hosted.reset_conversation("新的测试")
            else:
                await hosted.suspend()
                if transition == "resume":
                    await hosted.resume()
            await hosted.submit_tool_result(call, '{"recorded":true}')
            await asyncio.sleep(0.01)
            assert server.recorded.count("conversation.item.create") == 0
            assert server.recorded.count("response.create") == 0
        finally:
            await hosted.aclose()


async def test_pending_result_rechecks_session_after_waiting_for_slot() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            call = await _call(hosted)
            task = asyncio.create_task(hosted.submit_tool_result(call, '{"recorded":true}'))
            await asyncio.sleep(0)
            assert not task.done()
            await hosted.reset_conversation("新的测试")
            async with asyncio.timeout(2):
                await task
            await asyncio.sleep(0.01)
            assert server.recorded.count("conversation.item.create") == 0
        finally:
            await hosted.aclose()


async def test_unbound_tool_call_and_unknown_tool_cannot_write_results() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            unbound = link.ToolCall(link.ReplyHandle(), "external", "report_interaction", "{}")
            await hosted.submit_tool_result(unbound, "{}")
            call = await _call(hosted)
            await _done(hosted)
            await hosted.submit_tool_result(replace(call, name="unknown_tool"), "{}")
            await asyncio.sleep(0.01)
            assert server.recorded.count("conversation.item.create") == 0
        finally:
            await hosted.aclose()


async def test_tool_configuration_survives_automatic_session_rotation() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = HostedLink(
            server.url, ProviderName.DASHSCOPE, session_cap_min=0, reconnect_backoff_s=0.001
        )
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            call = await _call(hosted)
            await _done(hosted)
            original_session = hosted._client.session_id
            await hosted._client.rotate("test-tools")
            async with asyncio.timeout(2):
                async for event in hosted.events():
                    if isinstance(event, link.LinkUp):
                        break
            assert hosted._client.session_id != original_session
            await hosted.submit_tool_result(call, "{}")
            await _wait_count(server, "session.update", 2)
            assert all(
                event["session"]["tools"][0]["function"]["name"] == "report_interaction"
                for event in server.recorded.events
                if event.get("type") == "session.update"
            )
            assert server.recorded.count("conversation.item.create") == 0
        finally:
            await hosted.aclose()


async def test_result_pending_on_a_cancelled_handle_is_not_written() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            call = await _call(hosted)
            task = asyncio.create_task(hosted.submit_tool_result(call, "{}"))
            await asyncio.sleep(0)
            call.handle.stale = True
            await _done(hosted)
            async with asyncio.timeout(2):
                await task
            await asyncio.sleep(0.01)
            assert server.recorded.count("conversation.item.create") == 0
        finally:
            await hosted.aclose()


async def test_socket_receiver_can_finish_a_response_while_result_waits() -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = _hosted(server)
        await hosted.configure_tools((_tool(),))
        await hosted.connect()
        try:
            await server.send(
                dialect.ServerEvent.FUNCTION_ARGS_DONE,
                response_id="wire-report",
                call_id="wire-call",
                name="report_interaction",
                arguments="{}",
            )
            calls = [event async for event in _until_tool(hosted)]
            call = calls[-1]
            assert isinstance(call, link.ToolCall)
            task = asyncio.create_task(hosted.submit_tool_result(call, "{}"))
            await asyncio.sleep(0)
            assert not task.done()
            await server.send(
                dialect.ServerEvent.RESPONSE_DONE,
                response={"id": "wire-report", "status": "completed"},
            )
            async with asyncio.timeout(2):
                await task
            await _wait_count(server, "conversation.item.create", 1)
            assert server.recorded.count("response.create") == 0
        finally:
            await hosted.aclose()


async def _until_tool(hosted: HostedLink) -> AsyncIterator[link.LinkEvent]:
    async with asyncio.timeout(2):
        async for event in hosted.events():
            yield event
            if isinstance(event, link.ToolCall):
                return


@pytest.mark.parametrize("name", ["", " "])
async def test_empty_tool_names_are_rejected_before_configuration(name: str) -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    with pytest.raises(ValueError, match="工具名称"):
        await hosted.configure_tools((link.ToolSpec(name, "", {}),))
    assert hosted._bootstrap_frame() is None


async def test_duplicate_tool_names_are_rejected_before_configuration() -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    with pytest.raises(ValueError, match="工具名称"):
        await hosted.configure_tools((_tool(), _tool()))
    assert hosted._bootstrap_frame() is None


async def test_tool_handle_keeps_generation_from_created_before_later_speech() -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
    await hosted._client._dispatch(
        dialect.ServerEvent.RESPONSE_CREATED, {"response": {"id": "response-report"}}
    )
    await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
    while not hosted._client._events.empty():
        assert isinstance(hosted._client._events.get_nowait(), link.SpeechStarted)
    call = await _call(hosted)
    assert call.handle.input_generation == 1
    assert call.handle.implicit


async def test_unannounced_reply_has_no_claimed_input_generation() -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
    call = await _call(hosted)
    assert call.handle.input_generation is None


async def test_explicit_reply_binds_generation_after_slot_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hosted = HostedLink("ws://unused", ProviderName.DASHSCOPE)
    sent: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        sent.append(frame)

    monkeypatch.setattr(hosted._client, "_send_raw", send)
    hosted._client._take_slot("test-existing-response")
    task = asyncio.create_task(hosted._client.request_reply({"type": "response.create"}))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
        hosted._client._free_slot("test-existing-done")
        async with asyncio.timeout(2):
            handle = await task
        assert handle.input_generation == 1
        assert sent == [{"type": "response.create"}]
        await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
        hosted._client._on_created("response-report")
        assert (await _call(hosted)).handle is handle
        assert handle.input_generation == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await hosted.aclose()


@pytest.mark.parametrize("transition", ["reset", "rotate"])
async def test_input_generation_restarts_for_every_new_socket(transition: str) -> None:
    async with MockRealtimeServer(caps=capabilities.DASHSCOPE, codec=dialect.BETA) as server:
        hosted = HostedLink(
            server.url, ProviderName.DASHSCOPE, session_cap_min=0, reconnect_backoff_s=0.001
        )
        await hosted.connect()
        try:
            await hosted._client._dispatch(dialect.ServerEvent.SPEECH_STARTED, {})
            hosted._client._on_created("response-report")
            assert (await _call(hosted)).handle.input_generation == 1
            await _done(hosted)
            if transition == "reset":
                await hosted.reset_conversation("新的场景")
            else:
                await hosted._client.rotate("test-generation")
                async with asyncio.timeout(2):
                    async for event in hosted.events():
                        if isinstance(event, link.LinkUp):
                            break
            hosted._client._on_created("new-session-response")
            handle = hosted._client._replies["new-session-response"].handle
            assert handle.input_generation == 0
        finally:
            await hosted.aclose()
