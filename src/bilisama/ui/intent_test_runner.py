"""Paced voice scenarios with real link observations, never inferred labels."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from bilisama.clock import Clock
from bilisama.ingest.events import LiveEvent
from bilisama.ingest.sources import EventSink, QueueSource
from bilisama.realtime import link
from bilisama.ui.test_runner import (
    MockEvent,
    MockTestCase,
    MockTestCatalog,
    MockTestRunner,
    ScenarioStep,
    TestStatusSink,
)

_FRAME_BYTES = 1024
_FRAME_S = 0.032


class ScenarioIncomplete(RuntimeError):
    """A required stimulus or observable edge did not occur."""


class ScenarioSource(QueueSource):
    """Retire queued test events before they can leak into another case."""

    accepted_run: int | None = None

    async def start(self, emit: EventSink) -> None:
        async def deliver(event: LiveEvent) -> None:
            run_id = (event.raw or {}).get("intent_test_run")
            if self.accepted_run is not None and run_id == self.accepted_run:
                await emit(event)

        await super().start(deliver)


def check_scenario_ready(
    *,
    paused: bool,
    panicked: bool,
    room_connected: bool,
    live_mock_running: bool,
    link_connected: bool,
    output_enabled: bool,
    busy: bool,
) -> None:
    """Refuse a test before acquiring live inputs or cancelling any output."""
    if paused or panicked:
        raise ValueError("请先恢复伴播，再运行自动测试")
    if room_connected or live_mock_running:
        raise ValueError("请先断开系统页直播间并停止直播 Mock，避免真实事件混入测试")
    if not link_connected:
        raise ValueError("语音服务未连接，请等连接恢复后重试")
    if not output_enabled:
        raise ValueError("请先打开语音输出，自动测试需要观察实际播放状态")
    if busy:
        raise ValueError("当前仍有语音或待处理事件，请等这一轮结束后运行测试")


@dataclass(frozen=True)
class ScenarioHooks:
    """Application-owned input lease, context and playback adapters."""

    check_ready: Callable[[], None]
    audio: Callable[[str], Awaitable[bytes]]
    begin: Callable[[list[str]], Awaitable[None]]
    end: Callable[[], Awaitable[None]]
    push_audio: Callable[[bytes], Awaitable[None]]
    playback_busy: Callable[[], bool]
    response_busy: Callable[[], bool]
    monitor_audio: Callable[[bytes], None] = lambda _pcm: None
    pending_events: Callable[[], bool] = lambda: False


class IntentTestRunner(MockTestRunner):
    """Run a case after its session hook; expectations never go upstream."""

    def __init__(
        self,
        catalog: MockTestCatalog,
        source: QueueSource,
        clock: Clock,
        notify: TestStatusSink,
        hooks: ScenarioHooks,
    ) -> None:
        super().__init__(catalog, source, clock, notify)
        self._hooks = hooks
        self._observations: list[dict[str, Any]] = []
        self._unattributed: dict[str, Any] = self._empty_receipts()
        self._unmatched_asr: list[str] = []
        self._handles: dict[int, tuple[link.ReplyHandle, dict[str, Any]]] = {}
        self._model_skips: set[int] = set()
        self._pcm: deque[bytes] = deque()
        self._pump_task: asyncio.Task[None] | None = None
        self._listening = False
        self._fault = ""
        self._input_seq = 0
        self._frames_sent = 0
        self._late_frames = 0
        self._epoch = clock.monotonic()
        self._stopping = False
        self._clip_started = asyncio.Event()
        self._clip_started_at: float | None = None
        self._clip_last_sent_at = 0.0
        self._clip_frames_sent = 0

    @staticmethod
    def _empty_receipts() -> dict[str, Any]:
        return {"reply_details": [], "audio_chunks": 0, "replies": []}

    @property
    def active(self) -> bool:
        return self._task is not None

    @property
    def unfinished_handles(self) -> tuple[link.ReplyHandle, ...]:
        """Handles owned by this run, including implicit voice replies."""
        return tuple(
            handle
            for key, (handle, row) in self._handles.items()
            if any(r["handle_id"] == key and not r["done"] for r in row["reply_details"])
        )

    async def start(self, case_id: str) -> None:
        self._catalog.case(case_id)
        if self.active:
            raise ValueError("请先停止当前测试")
        self._hooks.check_ready()
        self._observations = []
        self._unattributed = self._empty_receipts()
        self._unmatched_asr = []
        self._handles = {}
        self._model_skips.clear()
        self._fault = ""
        self._late_frames = 0
        self._epoch = self._clock.monotonic()
        self._stopping = False
        self._input_seq = 0
        await super().start(case_id)

    async def stop(self) -> None:
        """Wait for lease cleanup and preserve failures reported by that cleanup."""
        task = self._task
        if task is None:
            return
        case_id, run_id = self._case_id, self._run_id
        if not self._stopping:
            self._stopping = True
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                # A caller cancellation must not interrupt lease restoration.
                await asyncio.shield(task)
                raise
        if self._task is task:
            # Cancellation can happen before the task enters its try/finally.
            self._task = None
            self._case_id = ""
            self._publish(
                status="stopped",
                case_id=case_id,
                run_id=run_id,
                text="已停止，后续音频和事件不会再注入",
            )

    def _publish(self, **data: object) -> None:
        super()._publish(
            **data,
            observations=deepcopy(self._observations),
            unattributed_replies=deepcopy(self._unattributed["reply_details"]),
            unmatched_asr=list(self._unmatched_asr),
            classification_available=False,
            audio_late_frames=self._late_frames,
        )

    def note_model_skip(self, handle: link.ReplyHandle) -> None:
        """Muted turns have no gated Done frame; close their test receipt."""
        if not self._listening:
            return
        self._model_skips.add(handle.handle_id)
        owned = self._handles.get(handle.handle_id)
        if owned is not None:
            for detail in owned[1]["reply_details"]:
                if detail["handle_id"] == handle.handle_id:
                    detail.update(done=True, status="skipped")

    def observe(self, event: link.LinkEvent, *, source_event_id: str = "") -> None:
        """Receive a fan-out copy, without consuming the scheduler's stream."""
        if not self._listening:
            return
        if isinstance(event, (link.LinkDown, link.LinkError)):
            self._fault = (
                f"语音链路中断：{event.reason}"
                if isinstance(event, link.LinkDown)
                else f"语音服务报错：{event.code}"
            )
            return
        if isinstance(event, link.UserTranscriptDone):
            # ASR may arrive after its reply. Prefer the oldest untranscribed
            # voice stimulus, not whichever event row happens to be current.
            row = next(
                (r for r in self._observations if r["kind"] == "voice" and not r["asr"]),
                None,
            )
            if not event.text.strip():
                return
            if row is None:
                if len(self._unmatched_asr) < 32:
                    self._unmatched_asr.append(event.text[:4000])
            elif len(row["asr"]) < 8:
                row["asr"].append(event.text[:4000])
            return
        if not isinstance(
            event, (link.ReplyStarted, link.ReplyTextDelta, link.ReplyAudioDelta, link.ReplyDone)
        ):
            return
        key = event.handle.handle_id
        if key not in self._handles:
            if len(self._handles) >= 128:
                self._fault = "本轮回复过多，已停止收集，请检查是否出现循环回复"
                return
            row, source_row, association = self._reply_origin(source_event_id)
            reply: dict[str, Any] = {
                "handle_id": key,
                "text": "",
                "audio_chunks": 0,
                "done": False,
                "status": "generating",
                "at_s": round(self._clock.monotonic() - self._epoch, 3),
                "source_event_id": source_event_id,
                "source_row": source_row,
                "association": association,
                "playback_seen": False,
                "playback_ended": False,
            }
            row["reply_details"].append(reply)
            self._handles[key] = (event.handle, row)
        elif source_event_id:
            self._bind_event_origin(key, source_event_id)
        row = self._handles[key][1]
        detail = next(r for r in row["reply_details"] if r["handle_id"] == key)
        if key in self._model_skips:
            detail.update(done=True, status="skipped")
            return
        if isinstance(event, link.ReplyTextDelta):
            detail["text"] = (detail["text"] + event.text)[:8000]
        elif isinstance(event, link.ReplyAudioDelta) and event.pcm and not event.handle.stale:
            detail["audio_chunks"] += 1
            row["audio_chunks"] += 1
            if "first_audio_s" not in detail:
                detail["first_audio_s"] = round(self._clock.monotonic() - self._epoch, 3)
        elif isinstance(event, link.ReplyDone):
            detail.update(done=True, status=event.status.value)
            if event.text:
                detail["text"] = event.text[:8000]
        row["replies"] = [r["text"] for r in row["reply_details"] if r["text"]]

    def _reply_origin(self, event_id: str) -> tuple[dict[str, Any], int, str]:
        if event_id:
            for row in self._observations:
                for event in row["events"]:
                    if event["event_id"] == event_id:
                        return row, int(event["source_row"]), "event_id"
            return self._unattributed, 0, "event_id 未匹配，不推断来源"
        voice = next((r for r in reversed(self._observations) if r["kind"] == "voice"), None)
        if voice is None:
            return self._unattributed, 0, "无语音来源，不推断归属"
        return voice, int(voice["source_row"]), "最近语音输入时序，非语义归因"

    def _bind_event_origin(self, key: int, event_id: str) -> None:
        """Replace a temporal guess when scheduler provenance arrives later."""
        handle, old_row = self._handles[key]
        detail = next(item for item in old_row["reply_details"] if item["handle_id"] == key)
        if detail["source_event_id"] == event_id:
            return
        row, source_row, association = self._reply_origin(event_id)
        detail.update(source_event_id=event_id, source_row=source_row, association=association)
        if row is not old_row:
            old_row["reply_details"].remove(detail)
            old_row["audio_chunks"] -= detail["audio_chunks"]
            old_row["replies"] = [item["text"] for item in old_row["reply_details"] if item["text"]]
            row["reply_details"].append(detail)
            row["audio_chunks"] += detail["audio_chunks"]
            self._handles[key] = (handle, row)

    async def _run(self, case: MockTestCase, run_id: int) -> None:
        acquired = False
        status = "completed"
        message = "执行完成，待人工判定；当前后端未提供意图分类结果"
        try:
            self._progress(
                case, run_id, "preparing", "正在准备 Seed TTS 2.0 测试语音（首次需要云端合成）"
            )
            clips = {
                step.id: await self._hooks.audio(step.text)
                for step in case.steps
                if step.kind == "voice"
            }
            for step in case.steps:
                if step.kind != "voice":
                    continue
                duration = len(clips[step.id]) / 32000
                if not duration or len(clips[step.id]) % 2:
                    raise ValueError("测试音频必须为非空的单声道 PCM16")
                if any(edge.offset_s >= duration for edge in step.events_during):
                    raise ValueError(f"{step.id} 的事件偏移超出真实语音时长")
            self._hooks.check_ready()
            acquired = True
            self._progress(case, run_id, "preparing", "正在清空历史会话并加载本例背景")
            await asyncio.wait_for(self._hooks.begin(case.context), timeout=20)
            self._epoch = self._clock.monotonic()
            self._listening = True
            self._pump_task = asyncio.create_task(self._pump(), name="intent-test:pcm")
            for index, step in enumerate(case.steps, 1):
                previous = self._observations[-1] if self._observations else None
                if step.after != "delay":
                    self._progress(case, run_id, "step", "等待真实回复状态", index)
                    await self._await_reply(step, previous)
                await self._wait(step.delay_s)
                if step.after == "reply_started" and not self._reply_ready(step, previous):
                    raise ScenarioIncomplete("助手已经播完，未形成要求的语音重叠，请重跑本例")
                row: dict[str, Any] = {
                    "step_id": step.id,
                    "kind": step.kind,
                    "source_row": step.source_row,
                    "input": step.text,
                    "expected": step.expected,
                    "expected_intent": step.expected_intent or case.expected_intent,
                    "actual_intent": None,
                    "intent_status": "unavailable",
                    "asr": [],
                    "replies": [],
                    **self._empty_receipts(),
                    "events": [],
                    "status": "observing",
                    "at_s": round(self._clock.monotonic() - self._epoch, 3),
                    "association": "按输入时序关联，非语义归因",
                }
                self._observations.append(row)
                self._progress(case, run_id, "step", step.text or step.kind, index)
                if step.kind == "voice":
                    await self._play(case, step, run_id, clips[step.id], row)
                elif step.kind == "event" and step.event is not None:
                    await self._inject(case, step.event, run_id, row, source_row=step.source_row)
                elif step.kind == "proactive":
                    raise ScenarioIncomplete(
                        "当前后端尚无第二阶段主动询问入口，DECLINED 待接入后验证"
                    )
                await self._wait(step.observe_s)
                row["status"] = "observed"
                self._progress(case, run_id, "step", "本步观察结束，不自动判定通过", index)
            await self._finish_receipts()
        except asyncio.CancelledError:
            status, message = "stopped", "已停止，后续音频和事件不会再注入"
        except ScenarioIncomplete as exc:
            status, message = "incomplete", str(exc)
            if self._observations:
                self._observations[-1]["status"] = "timeout"
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            status, message = "failed", f"测试未完成：{exc}"
        finally:
            self._listening = False
            cleanup = asyncio.create_task(self._cleanup(acquired), name="intent-test:cleanup")
            try:
                failure = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                failure = await asyncio.shield(cleanup)
                status, message = "stopped", "已停止，后续音频和事件不会再注入"
            if failure:
                status, message = "failed", failure
            if self._run_id == run_id:
                self._task = None
                self._case_id = ""
        self._progress(case, run_id, status, message, len(self._observations))

    async def _cleanup(self, acquired: bool) -> str:
        self._pcm.clear()
        failure = ""
        if acquired and isinstance(self._source, ScenarioSource):
            self._source.accepted_run = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            result = (await asyncio.gather(self._pump_task, return_exceptions=True))[0]
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                failure = f"测试音频发送失败：{result}"
            self._pump_task = None
        if acquired:
            try:
                await asyncio.wait_for(self._hooks.end(), timeout=20)
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                failure = f"测试输入恢复失败：{exc}"
        return failure

    async def _finish_receipts(self) -> None:
        """Do not cut final playback or reject a transcript merely for arriving late."""
        deadline = self._clock.monotonic() + 30
        idle_since: float | None = None
        while True:
            self._check()
            missing_asr = any(r["kind"] == "voice" and not r["asr"] for r in self._observations)
            busy = (
                self._hooks.response_busy()
                or self._hooks.playback_busy()
                or bool(self.unfinished_handles)
                or self._hooks.pending_events()
            )
            if not busy and not missing_asr:
                if idle_since is None:
                    idle_since = self._clock.monotonic()
                if self._clock.monotonic() - idle_since >= 0.5:
                    return
            else:
                idle_since = None
            if self._clock.monotonic() >= deadline:
                if missing_asr:
                    raise ScenarioIncomplete(
                        "部分语音未收到识别回执，请查看逐轮记录；不能视为识别正确"
                    )
                raise ScenarioIncomplete("最后一轮回复未在 30 秒内结束")
            await self._wait(_FRAME_S)

    def _progress(
        self, case: MockTestCase, run_id: int, status: str, text: str, index: int = 0
    ) -> None:
        self._publish(
            status=status,
            case_id=case.id,
            run_id=run_id,
            index=index,
            total=len(case.steps),
            text=text,
        )

    async def _pump(self) -> None:
        self._frames_sent = 0
        while True:
            voiced = bool(self._pcm)
            frame = self._pcm.popleft() if voiced else bytes(_FRAME_BYTES)
            started = self._clock.monotonic()
            await asyncio.wait_for(self._hooks.push_audio(frame), timeout=3)
            if self._stopping:
                return
            self._frames_sent += 1
            now = self._clock.monotonic()
            if voiced:
                # Monitoring is a synchronous output mirror, never another
                # model input or an assistant playback receipt.
                self._hooks.monitor_audio(frame)
                self._clip_frames_sent += 1
                self._clip_last_sent_at = now
                if self._clip_started_at is None:
                    self._clip_started_at = now
                    self._clip_started.set()
            if now - started > _FRAME_S:
                self._late_frames += 1
            # Always leave one frame between sends. Network delays must never
            # turn into consecutive catch-up writes to the realtime backend.
            await self._clock.sleep(_FRAME_S)

    async def _wait(self, seconds: float) -> None:
        until = self._clock.monotonic() + seconds
        while self._clock.monotonic() < until:
            self._check()
            await self._clock.sleep(min(_FRAME_S, until - self._clock.monotonic()))
        self._check()

    def _check(self) -> None:
        if self._fault:
            raise ScenarioIncomplete(self._fault)
        if self._pump_task is not None and self._pump_task.done():
            if self._pump_task.cancelled():
                raise RuntimeError("测试音频发送意外取消")
            failure = self._pump_task.exception()
            if failure is not None:
                raise RuntimeError(f"测试音频发送失败：{failure}") from failure
            raise RuntimeError("测试音频发送已停止")
        self._sample_playback()

    def _sample_playback(self) -> None:
        busy = self._hooks.playback_busy()
        for _handle, row in self._handles.values():
            for detail in row["reply_details"]:
                if not detail["audio_chunks"] or detail["playback_ended"]:
                    continue
                if busy:
                    detail["playback_seen"] = True
                elif detail["playback_seen"] and detail["done"]:
                    detail["playback_ended"] = True

    async def _play(
        self,
        case: MockTestCase,
        step: ScenarioStep,
        run_id: int,
        pcm: bytes,
        row: dict[str, Any],
    ) -> None:
        self._clip_started.clear()
        self._clip_started_at = None
        self._clip_frames_sent = 0
        frames = [
            pcm[i : i + _FRAME_BYTES].ljust(_FRAME_BYTES, b"\0")
            for i in range(0, len(pcm), _FRAME_BYTES)
        ]
        self._pcm.extend(frames)
        edges = sorted(step.events_during, key=lambda edge: edge.offset_s)
        waiting = asyncio.create_task(self._clip_started.wait())
        assert self._pump_task is not None
        try:
            await asyncio.wait((waiting, self._pump_task), return_when=asyncio.FIRST_COMPLETED)
            self._check()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        started_at = self._clip_start_time()
        row["voice_start_s"] = round(started_at - self._epoch, 3)
        row["voice_duration_s"] = len(pcm) / 32000
        while True:
            sent_s = self._clock.monotonic() - started_at
            while edges and edges[0].offset_s <= sent_s + 1e-9:
                edge = edges.pop(0)
                await self._inject(
                    case,
                    edge.event,
                    run_id,
                    row,
                    source_row=edge.source_row,
                    target_offset_s=edge.offset_s,
                )
                if self._clip_finished(len(frames)):
                    raise ScenarioIncomplete("事件因发送延迟落在语音结束后，没有形成要求的重叠")
            if self._clip_finished(len(frames)):
                break
            delay = _FRAME_S
            if edges:
                delay = min(delay, max(0, edges[0].offset_s - sent_s))
            await self._wait(delay)
        if edges:
            raise ScenarioIncomplete("语音已经结束，部分语音中事件尚未注入")
        row["voice_end_s"] = round(self._clock.monotonic() - self._epoch, 3)

    def _clip_start_time(self) -> float:
        if self._clip_started_at is None:
            raise RuntimeError("测试音频尚未开始发送")
        return self._clip_started_at

    def _clip_finished(self, frame_count: int) -> bool:
        return (
            self._clip_frames_sent >= frame_count
            and self._clock.monotonic() >= self._clip_last_sent_at + _FRAME_S - 1e-9
        )

    async def _inject(
        self,
        case: MockTestCase,
        event: MockEvent,
        run_id: int,
        row: dict[str, Any],
        *,
        source_row: int = 0,
        target_offset_s: float | None = None,
    ) -> None:
        self._input_seq += 1
        live = self._live_event(case, event, run_id, self._input_seq)
        live = replace(
            live,
            viewer=replace(
                live.viewer, uid=0, uid_hash=f"intent-test:{run_id}:{live.viewer.identity}"
            ),
        )
        # Test run identity is separate from the case id: late selector winners
        # from an earlier execution must not enter the next run's reply path.
        assert live.raw is not None
        live.raw["intent_test_run"] = run_id
        record = {
            "summary": event.summary(),
            "event_id": live.event_id,
            "at_s": round(self._clock.monotonic() - self._epoch, 3),
            "source_row": source_row,
            "target_offset_s": target_offset_s,
            "status": "injecting",
        }
        # Register identity before pushing: an immediate local reply can race
        # the task resuming from push(), even though the event was accepted.
        row["events"].append(record)
        await asyncio.wait_for(self._source.push(live), timeout=3)
        record.update(status="injected", at_s=round(self._clock.monotonic() - self._epoch, 3))

    async def _await_reply(self, step: ScenarioStep, previous: dict[str, Any] | None) -> None:
        deadline = self._clock.monotonic() + step.timeout_s
        while self._clock.monotonic() < deadline:
            self._check()
            if self._reply_ready(step, previous):
                return
            await self._wait(_FRAME_S)
        raise ScenarioIncomplete(f"{step.id} 等待 {step.after} 超时，要求的播放条件没有发生")

    def _reply_ready(self, step: ScenarioStep, previous: dict[str, Any] | None) -> bool:
        self._sample_playback()
        details = (
            [
                detail
                for row in self._observations
                for detail in row["reply_details"]
                if detail["source_row"] == step.reply_from_row
            ]
            if step.reply_from_row
            else previous["reply_details"] if previous is not None else []
        )
        audible = [item for item in details if item["audio_chunks"] > 0]
        if step.after == "reply_started":
            return bool(
                self._hooks.playback_busy()
                and any(
                    not item["playback_ended"] and item["status"] in {"generating", "completed"}
                    for item in audible
                )
            )
        return bool(
            audible
            and all(item["done"] for item in details)
            and not self._hooks.playback_busy()
            and not self._hooks.response_busy()
        )
