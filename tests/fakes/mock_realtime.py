"""进程内的假 Realtime 服务端。

它的价值不在于"能连上"，而在于**§3.3 那八条客户端规则每一条都是一个可复现的
失败模式**。否则那八条只是文档里的好意。

用 Capabilities + Codec 构造，所以同一个测试类可以跑三遍：speech-to-speech 形状、
DashScope 形状、OpenAI GA 形状。

不加载任何模型，不联网，发罐装 PCM。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.capabilities import Capabilities


class Fault(StrEnum):
    """可脚本化的失败模式。每一条对应 §3.3 的一条规则。"""

    # 规则 1/2：注入撞上投机轮次 → 回复被吞，且永不发 response.done，in_response 卡死
    WEDGE_ON_INJECTION = "wedge_on_injection"
    # 规则 3：response.cancel 在 response_pending 时是空操作
    CANCEL_IS_NOOP = "cancel_is_noop"
    # 规则 4：隐式回复不发 response.created
    NO_RESPONSE_CREATED = "no_response_created"
    # item.create 被静默延后，回复结束后才补发 ack
    DEFER_ITEM_CREATE = "defer_item_create"
    # 下面两个是计划 §10.1 要求覆盖、但还没实现的场景。
    # 留着是为了不把缺口从记录里抹掉；实现见待办第 9 项。
    #
    # NOT IMPLEMENTED：speech_stopped 前面没有 speech_started
    ORPHAN_SPEECH_STOPPED = "orphan_speech_stopped"
    # NOT IMPLEMENTED：被取消的文本回复不发 output_text.done
    NO_TEXT_DONE_ON_CANCEL = "no_text_done_on_cancel"
    # 回复卡住，用来验客户端看门狗
    STALL_RESPONSE = "stall_response"
    # 会话容量满
    SESSION_LIMIT = "session_limit"
    # 发一个工具调用
    EMIT_TOOL_CALL = "emit_tool_call"


@dataclass(slots=True)
class Script:
    """一次测试要复现什么。"""

    faults: set[Fault] = field(default_factory=set)
    reply_text: str = "好的，我看到了。"
    delta_chunks: int = 3
    # 每个 delta 之间等多久，让测试能插入打断
    delta_interval_s: float = 0.0
    audio_ms_per_delta: int = 40

    def has(self, fault: Fault) -> bool:
        return fault in self.faults


@dataclass(slots=True)
class Recorded:
    """服务端收到了什么。断言客户端行为用。"""

    events: list[dict[str, Any]] = field(default_factory=list)

    def types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]

    def count(self, wire_type: str) -> int:
        return sum(1 for e in self.events if e.get("type") == wire_type)

    def last(self, wire_type: str) -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e.get("type") == wire_type:
                return e
        return None


def _pcm(ms: int, *, rate: int = 24000, freq: float = 220.0) -> bytes:
    n = int(rate * ms / 1000)
    return struct.pack(
        f"<{n}h", *(int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
    )


class MockRealtimeServer:
    """在 127.0.0.1 的临时端口上起一个假 Realtime 服务端。

    用法::

        async with MockRealtimeServer(caps=capabilities.S2S) as server:
            ...  # server.url 连过去
            assert server.recorded.count("response.create") == 1
    """

    def __init__(
        self,
        *,
        caps: Capabilities | None = None,
        codec: dia.Codec | None = None,
        script: Script | None = None,
    ) -> None:
        self.caps = caps or caps_mod.S2S
        self.codec = codec or dia.GA
        self.script = script or Script()
        self.recorded = Recorded()
        self._server: Any = None
        self._conn: ServerConnection | None = None
        self._port = 0
        # 服务端状态。刻意跟上游同名，好对照着读。
        self._in_response = False
        self._response_pending = False
        self._response_id = 0
        self._deferred: list[dict[str, Any]] = []
        self._speculative_open = False
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> MockRealtimeServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self._port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/v1/realtime"

    # ------------------------------------------------------------ 服务端推事件

    async def send(self, event: dia.ServerEvent, **payload: Any) -> None:
        """按当前方言把内部事件名翻成 wire 名发出去。"""
        if self._conn is None:
            raise RuntimeError("还没有客户端连上来")
        body = {"type": dia.outbound_name(self.codec, event), **payload}
        await self._conn.send(json.dumps(body))

    async def speech_started(self) -> None:
        self._speculative_open = True
        await self.send(dia.ServerEvent.SPEECH_STARTED)

    async def speech_stopped(self) -> None:
        """主播停口。之后投机重开窗口还开着一小会儿。"""
        await self.send(dia.ServerEvent.SPEECH_STOPPED)

    async def close_speculative_window(self) -> None:
        self._speculative_open = False

    async def barge_in(self) -> None:
        """模拟主播打断。注意顺序：先 response.done(cancelled)，后 speech_started。

        这个顺序是反直觉的，但上游就是这么发的（`websocket_router.py:745-785`
        先取 in_response 快照并 finish_response，再 dispatch 出 speech_started）。
        """
        if self._in_response:
            await self._finish_response(status="cancelled", reason="turn_detected")
        self._speculative_open = True
        await self.send(dia.ServerEvent.SPEECH_STARTED)

    async def emit_implicit_reply(self) -> None:
        """服务端 VAD 自己发起的那一轮。默认**不发** response.created。"""
        self._response_pending = True
        task = asyncio.create_task(self._run_response(implicit=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 内部

    async def _handle(self, conn: ServerConnection) -> None:
        self._conn = conn
        await self.send(dia.ServerEvent.SESSION_CREATED, session={"id": "mock"})
        try:
            async for raw in conn:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.recorded.events.append(event)
                await self._dispatch(event)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conn = None

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == dia.ClientEvent.SESSION_UPDATE.value:
            if self.caps.acknowledges_session_update:
                await self.send(dia.ServerEvent.SESSION_UPDATED, session=event.get("session", {}))
        elif kind == dia.ClientEvent.AUDIO_APPEND.value:
            pass  # 音频只记不回
        elif kind == dia.ClientEvent.ITEM_CREATE.value:
            await self._on_item_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CREATE.value:
            await self._on_response_create(event)
        elif kind == dia.ClientEvent.RESPONSE_CANCEL.value:
            await self._on_cancel()
        elif kind == dia.ClientEvent.ITEM_TRUNCATE.value:
            if self.caps.item_truncate:
                await self.send(
                    dia.ServerEvent.ITEM_TRUNCATED,
                    item_id=event.get("item_id"),
                    content_index=event.get("content_index", 0),
                    audio_end_ms=event.get("audio_end_ms", 0),
                )
            else:
                await self._error("unknown_or_invalid_event", "不支持 conversation.item.truncate")

    async def _on_item_create(self, event: dict[str, Any]) -> None:
        # 有回复在生成时，item.create 被静默延后：返回空，回复结束后补发 ack
        if self._in_response and self.script.has(Fault.DEFER_ITEM_CREATE):
            self._deferred.append(event)
            return
        await self.send(dia.ServerEvent.ITEM_CREATED, item=event.get("item", {}))

    async def _on_response_create(self, event: dict[str, Any]) -> None:
        out_of_band = (event.get("response") or {}).get("conversation") == "none"

        if self.script.has(Fault.SESSION_LIMIT):
            await self._error("session_limit_reached", "会话容量已满")
            return

        occupies_slot = not (out_of_band and self.caps.out_of_band_exempt_from_slot)
        if self.caps.single_response_slot and self._in_response and occupies_slot:
            await self._error("conversation_already_has_active_response", "已经有一个回复在生成了")
            return

        # 规则 1：in-band 注入撞上还开着的投机轮次 → 整条回复被吞，连 done 都不发
        if self.script.has(Fault.WEDGE_ON_INJECTION) and not out_of_band and self._speculative_open:
            self._in_response = True  # 卡死：之后所有 response.create 都会被拒
            return

        task = asyncio.create_task(self._run_response(implicit=False, event=event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_cancel(self) -> None:
        # 规则 3：只有 in_response 为真时才真的取消。pending 时是空操作
        if self.script.has(Fault.CANCEL_IS_NOOP) and not self._in_response:
            return
        if not self._in_response:
            return
        await self._finish_response(status="cancelled", reason="client_cancelled")

    async def _run_response(self, *, implicit: bool, event: dict[str, Any] | None = None) -> None:
        self._response_id += 1
        rid = f"resp_{self._response_id}"
        self._in_response = True
        self._response_pending = False

        # 规则 4：隐式回复不发 response.created
        skip_created = implicit or self.script.has(Fault.NO_RESPONSE_CREATED)
        if not skip_created:
            await self.send(dia.ServerEvent.RESPONSE_CREATED, response={"id": rid})

        if self.script.has(Fault.STALL_RESPONSE):
            await asyncio.sleep(3600)  # 让客户端的看门狗去处理
            return

        if self.script.has(Fault.EMIT_TOOL_CALL):
            await self.send(
                dia.ServerEvent.FUNCTION_ARGS_DONE,
                response_id=rid,
                call_id="call_1",
                name="get_stream_status",
                arguments='{"key": "uptime"}',
            )
            await self._finish_response(status="completed")
            return

        text = self.script.reply_text
        step = max(1, len(text) // max(1, self.script.delta_chunks))
        pieces = [text[i : i + step] for i in range(0, len(text), step)]

        for piece in pieces:
            if not self._in_response:
                return  # 中途被取消了
            if self.caps.owns_tts:
                await self.send(dia.ServerEvent.TRANSCRIPT_DELTA, response_id=rid, delta=piece)
                await self.send(
                    dia.ServerEvent.AUDIO_DELTA,
                    response_id=rid,
                    delta=base64.b64encode(_pcm(self.script.audio_ms_per_delta)).decode(),
                )
            else:
                await self.send(dia.ServerEvent.TEXT_DELTA, response_id=rid, delta=piece)
            if self.script.delta_interval_s:
                await asyncio.sleep(self.script.delta_interval_s)

        if not self._in_response:
            return
        if not self.caps.owns_tts:
            await self.send(dia.ServerEvent.TEXT_DONE, response_id=rid, text=text)
        await self._finish_response(status="completed")

    async def _finish_response(self, *, status: str, reason: str | None = None) -> None:
        if not self._in_response:
            return
        self._in_response = False
        self._response_pending = False

        payload: dict[str, Any] = {
            "response": {"id": f"resp_{self._response_id}", "status": status}
        }
        if reason:
            payload["response"]["status_details"] = {"reason": reason}
        await self.send(dia.ServerEvent.RESPONSE_DONE, **payload)

        # 延后的 item.create 在回复结束时补发 ack,客户端要能接住这个迟到的 ack
        while self._deferred:
            pending = self._deferred.pop(0)
            await self.send(dia.ServerEvent.ITEM_CREATED, item=pending.get("item", {}))

    async def _error(self, code: str, message: str) -> None:
        await self.send(dia.ServerEvent.ERROR, error={"type": code, "message": message})
