"""Runnable mock scenarios for the desktop acceptance-test console."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bilisama.clock import Clock
from bilisama.ingest.events import EventKind, Gift, GuardLevel, LiveEvent, Medal, Viewer
from bilisama.ingest.sources import QueueSource
from bilisama.obs.logging import get_logger

log = get_logger(__name__)

TestStatusSink = Callable[[dict[str, object]], None]
TestRevokeSink = Callable[[str], None]

_EVENT_LABEL = {
    EventKind.DANMAKU: "弹幕",
    EventKind.GIFT: "礼物",
    EventKind.SUPER_CHAT: "SC",
    EventKind.GUARD_BUY: "上舰",
    EventKind.VIP_ENTER: "VIP 进房",
    EventKind.ENTRY: "普通进房",
    EventKind.FOLLOW: "关注",
    EventKind.LIKE: "点赞",
    EventKind.SHARE: "分享",
    EventKind.ROOM_STATE: "房间状态",
}


class MockMedal(BaseModel):
    """Current-room medal identity returned by the platform interface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    level: int = Field(ge=0)
    up_name: str = ""
    anchor_room_id: int = Field(default=990000, ge=0)


class MockViewer(BaseModel):
    """A deterministic viewer identity used by a mock event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: int = Field(ge=0)
    uid_hash: str = ""
    name: str = Field(min_length=1)
    user_level: int = Field(default=0, ge=0)
    wealth_level: int = Field(default=0, ge=0)
    guard_level: GuardLevel = GuardLevel.NONE
    is_admin: bool = False
    is_anchor: bool = False
    medal: MockMedal | None = None


class MockGift(BaseModel):
    """Gift fields required by the production aggregation and tier logic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gift_id: int = Field(default=1, ge=0)
    name: str = Field(default="小花花", min_length=1)
    num: int = Field(default=1, ge=1)
    coin_type: Literal["", "silver", "gold"] = ""
    total_coin: int = Field(default=0, ge=0)
    unit_battery: int = Field(default=1, ge=1)
    combo_id: str = ""
    combo_count: int = Field(default=0, ge=0)
    combo_end: bool | None = None


class MockEvent(BaseModel):
    """One timed event in a runnable scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_s: float = Field(ge=0)
    kind: EventKind
    viewer: MockViewer
    text: str = ""
    value_cny: float = Field(default=0, ge=0)
    gift: MockGift | None = None
    route: Literal["direct", "crowd"] = "direct"
    dedup_group: str = ""

    @model_validator(mode="after")
    def validate_gift(self) -> MockEvent:
        if self.kind is EventKind.GIFT and self.gift is None:
            raise ValueError("gift 事件必须提供 gift 字段")
        if self.kind is not EventKind.GIFT and self.gift is not None:
            raise ValueError("只有 gift 事件能提供 gift 字段")
        if self.gift is not None:
            expected_coin = self.gift.unit_battery * self.gift.num * 100
            if self.gift.total_coin != expected_coin:
                raise ValueError("gift.total_coin 必须与电池单价和数量一致")
            if self.value_cny != expected_coin / 1000:
                raise ValueError("value_cny 仅保留原始事件金额，必须与 total_coin 一致")
        return self

    def summary(self) -> str:
        """Return the compact mock-event label displayed by the UI."""
        label = _EVENT_LABEL[self.kind]
        detail = self.text
        if self.kind is EventKind.GIFT and self.gift is not None:
            batteries = self.gift.unit_battery * self.gift.num
            detail = f"{self.gift.name} ×{self.gift.num} · {batteries} 电池"
        suffix = f"：{detail}" if detail else ""
        return f"{self.viewer.name} · {label}{suffix}"


class MockBurst(BaseModel):
    """A compact definition for high-volume normalized event pressure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_s: float = Field(ge=0)
    duration_s: float = Field(ge=0)
    count: int = Field(ge=1, le=1000)
    kind: EventKind
    viewer: MockViewer
    unique_viewers: bool = False
    text: str = ""
    value_cny: float = Field(default=0, ge=0)
    gift: MockGift | None = None
    route: Literal["direct", "crowd"] = "direct"
    dedup_group: str = ""

    @model_validator(mode="after")
    def validate_gift(self) -> MockBurst:
        if self.kind is EventKind.GIFT and self.gift is None:
            raise ValueError("gift 洪峰必须提供 gift 字段")
        if self.kind is not EventKind.GIFT and self.gift is not None:
            raise ValueError("只有 gift 洪峰能提供 gift 字段")
        if self.gift is not None:
            expected_coin = self.gift.unit_battery * self.gift.num * 100
            if self.gift.total_coin != expected_coin:
                raise ValueError("gift.total_coin 必须与电池单价和数量一致")
            if self.value_cny != expected_coin / 1000:
                raise ValueError("value_cny 仅保留原始事件金额，必须与 total_coin 一致")
        return self

    def expand(self) -> list[MockEvent]:
        """Expand the burst into deterministic event specs at run time."""
        events: list[MockEvent] = []
        for index in range(self.count):
            ratio = index / (self.count - 1) if self.count > 1 else 0.0
            viewer = self.viewer
            if self.unique_viewers:
                viewer = viewer.model_copy(
                    update={"uid": viewer.uid + index, "name": f"{viewer.name}{index + 1}"}
                )
            gift = self.gift
            if gift is not None and gift.combo_id:
                gift = gift.model_copy(
                    update={"combo_count": index + 1, "combo_end": index == self.count - 1}
                )
            events.append(
                MockEvent(
                    at_s=self.at_s + self.duration_s * ratio,
                    kind=self.kind,
                    viewer=viewer,
                    text=self.text.replace("{n}", str(index + 1)),
                    value_cny=self.value_cny,
                    gift=gift,
                    route=self.route,
                    dedup_group=(f"{self.dedup_group}:{index + 1}" if self.dedup_group else ""),
                )
            )
        return events

    def summary(self) -> str:
        """Return one line instead of rendering hundreds of event chips."""
        seconds = f"{self.duration_s:g} 秒内" if self.duration_s else "同一时刻"
        return f"{_EVENT_LABEL[self.kind]} ×{self.count}（{seconds}）"


class MockAction(BaseModel):
    """A platform control edge that is not a LiveEvent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_s: float = Field(ge=0)
    kind: Literal["sc_revoke"]
    target_group: str = Field(min_length=1)

    def summary(self) -> str:
        """Return the operator-visible action label."""
        return "平台撤回指定 SC"


IntentLabel = Literal["TO_ME", "AUDIENCE", "SELF_TALK", "READING", "GUEST", "UNSURE", "DECLINED"]


class ScenarioEvent(BaseModel):
    """An event offset measured from the start of a voice step."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    offset_s: float = Field(ge=0, le=180)
    event: MockEvent
    source_row: int = 0


class ScenarioStep(BaseModel):
    """Input facts and operator-only expectations stay in separate fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    kind: Literal["voice", "event", "wait", "proactive"]
    text: str = ""
    after: Literal["delay", "reply_started", "reply_finished"] = "delay"
    delay_s: float = Field(default=0, ge=0, le=180)
    timeout_s: float = Field(default=30, gt=0, le=180)
    observe_s: float = Field(default=5, ge=0, le=180)
    expected: str = Field(min_length=1)
    expected_intent: IntentLabel | None = None
    source_row: int = 0
    event: MockEvent | None = None
    reply_from_row: int = Field(default=0, ge=0)
    events_during: list[ScenarioEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_input(self) -> ScenarioStep:
        if self.kind == "voice" and not self.text.strip():
            raise ValueError("语音步骤必须包含实际台词")
        if (self.kind == "event") != (self.event is not None):
            raise ValueError("只有事件步骤必须提供 event")
        if self.events_during and self.kind != "voice":
            raise ValueError("只有语音步骤支持语音中注入事件")
        return self


class MockTestCase(BaseModel):
    """One human-observed acceptance or business test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")
    group: str = Field(min_length=1)
    title: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    focus: list[str] = Field(default_factory=list)
    expected: list[str] = Field(min_length=1)
    reference: str = ""
    duration_s: float = Field(ge=0)
    candidate_id: str = ""
    candidate_name: str = ""
    events: list[MockEvent] = Field(default_factory=list)
    bursts: list[MockBurst] = Field(default_factory=list)
    actions: list[MockAction] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    steps: list[ScenarioStep] = Field(default_factory=list)
    expected_intent: IntentLabel | None = None
    source_rows: list[int] = Field(default_factory=list)
    allow_proactive: bool = False

    @model_validator(mode="after")
    def validate_timeline(self) -> MockTestCase:
        if self.steps and (self.events or self.bursts or self.actions):
            raise ValueError("多轮语音步骤不能混用旧的固定时间线")
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("步骤 id 不能重复")
        times = [item[0] for item in self.timeline()]
        if times != sorted(times):
            raise ValueError("events、bursts 和 actions 必须按 at_s 升序排列")
        if times and times[-1] > self.duration_s:
            raise ValueError("最后一个事件不能晚于 duration_s")
        if bool(self.candidate_id) is not bool(self.candidate_name):
            raise ValueError("candidate_id 和 candidate_name 必须一起提供")
        return self

    def timeline(self) -> list[tuple[float, MockEvent | MockAction]]:
        """Return the expanded, time-ordered sequence consumed by the runner."""
        items: list[tuple[float, MockEvent | MockAction]] = [
            (event.at_s, event) for event in self.events
        ]
        for burst in self.bursts:
            items.extend((event.at_s, event) for event in burst.expand())
        items.extend((action.at_s, action) for action in self.actions)
        return sorted(items, key=lambda item: item[0])

    def public(self) -> dict[str, object]:
        """Return the safe, JSON-friendly description sent in hello."""
        preview = [
            {"at_s": event.at_s, "kind": event.kind.value, "summary": event.summary()}
            for event in self.events
        ]
        preview += [
            {"at_s": burst.at_s, "kind": "burst", "summary": burst.summary()}
            for burst in self.bursts
        ]
        preview += [
            {"at_s": action.at_s, "kind": "action", "summary": action.summary()}
            for action in self.actions
        ]
        preview.sort(key=lambda item: float(item["at_s"]))
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "operator": self.operator,
            "focus": self.focus,
            "expected": self.expected,
            "reference": self.reference,
            "duration_s": self.duration_s,
            "candidate_id": self.candidate_id,
            "candidate_name": self.candidate_name,
            "event_count": len(self.timeline()),
            "events": preview,
            "context": self.context,
            "steps": [step.model_dump(mode="json") for step in self.steps],
            "expected_intent": self.expected_intent,
            "source_rows": self.source_rows,
            "allow_proactive": self.allow_proactive,
            "execution": "voice" if self.steps else "events",
            "classification_available": False,
        }


class MockTestSet(BaseModel):
    """One of the two test collections shown in the desktop UI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Literal["functional", "business", "simple", "hard"]
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cases: list[MockTestCase] = Field(min_length=1)


class MockTestCatalog:
    """Validated lookup and UI projection for both shipped test sets."""

    def __init__(self, sets: list[MockTestSet]) -> None:
        if [test_set.id for test_set in sets] not in (
            ["functional", "business"],
            ["simple", "hard"],
        ):
            raise ValueError("测试集必须依次包含 functional 和 business，或 simple 和 hard")
        cases = [case for test_set in sets for case in test_set.cases]
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("测试用例 id 不能重复")
        self.sets = sets
        self._cases = {case.id: case for case in cases}

    def case(self, case_id: str) -> MockTestCase:
        """Resolve a runnable case or report a user-facing error."""
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise ValueError(f"找不到测试用例：{case_id}") from exc

    def public(self) -> dict[str, object]:
        """Return both test sets for the hello frame."""
        return {
            "sets": [
                {
                    "id": test_set.id,
                    "title": test_set.title,
                    "description": test_set.description,
                    "cases": [case.public() for case in test_set.cases],
                }
                for test_set in self.sets
            ]
        }


def load_test_catalog(root: Path, *, intent: bool = False) -> MockTestCatalog:
    """Load the two tracked JSON test sets from the config directory."""
    sets: list[MockTestSet] = []
    for name in (("simple", "hard") if intent else ("functional", "business")):
        path = root / f"{name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"测试集读不到：{path}（{exc}）") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"测试集不是合法 JSON：{path}:{exc.lineno}") from exc
        sets.append(MockTestSet.model_validate(payload))
    return MockTestCatalog(sets)


class MockTestRunner:
    """Inject one selected scenario into the same Assembly used by live events."""

    def __init__(
        self,
        catalog: MockTestCatalog,
        source: QueueSource,
        clock: Clock,
        notify: TestStatusSink,
        revoke: TestRevokeSink | None = None,
    ) -> None:
        self._catalog = catalog
        self._source = source
        self._clock = clock
        self._notify = notify
        self._revoke = revoke or (lambda _key: None)
        self._task: asyncio.Task[None] | None = None
        self._case_id = ""
        self._run_id = 0
        self._state: dict[str, object] = {"status": "idle", "case_id": ""}

    def state(self) -> dict[str, object]:
        """Return the current state for a newly connected panel."""
        return dict(self._state)

    async def start(self, case_id: str) -> None:
        """Replace the current run with the selected scenario."""
        case = self._catalog.case(case_id)
        await self.stop()
        self._run_id += 1
        run_id = self._run_id
        self._case_id = case.id
        timeline = case.timeline()
        self._publish(
            status="running",
            case_id=case.id,
            run_id=run_id,
            index=0,
            total=len(timeline),
            text="测试开始",
        )
        self._task = asyncio.create_task(
            self._run(case, run_id), name=f"ui-test:{case.id}:{run_id}"
        )

    async def stop(self) -> None:
        """Stop the active scenario without stopping the shared mock source."""
        task = self._task
        if task is None:
            return
        case_id = self._case_id
        run_id = self._run_id
        self._task = None
        self._case_id = ""
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._publish(
            status="stopped",
            case_id=case_id,
            run_id=run_id,
            text="已停止，后续事件不会再注入",
        )

    async def _run(self, case: MockTestCase, run_id: int) -> None:
        cursor = 0.0
        try:
            timeline = case.timeline()
            for index, (at_s, item) in enumerate(timeline, start=1):
                await self._clock.sleep(at_s - cursor)
                cursor = at_s
                if isinstance(item, MockEvent):
                    await self._source.push(self._live_event(case, item, run_id, index))
                else:
                    self._run_action(case, item, run_id)
                self._publish(
                    status="event",
                    case_id=case.id,
                    run_id=run_id,
                    index=index,
                    total=len(timeline),
                    text=item.summary(),
                )
            await self._clock.sleep(case.duration_s - cursor)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError) as exc:
            log.exception("ui_test.failed", case_id=case.id, error_text=str(exc))
            self._publish(
                status="failed",
                case_id=case.id,
                run_id=run_id,
                text=f"测试运行失败：{exc}",
            )
        else:
            self._publish(
                status="completed",
                case_id=case.id,
                run_id=run_id,
                index=len(timeline),
                total=len(timeline),
                text="事件已注入，请按预期人工判断",
            )
        finally:
            if self._run_id == run_id:
                self._task = None
                self._case_id = ""

    def _live_event(
        self, case: MockTestCase, spec: MockEvent, run_id: int, index: int
    ) -> LiveEvent:
        viewer = Viewer(
            uid=spec.viewer.uid,
            uid_hash=spec.viewer.uid_hash,
            name=spec.viewer.name,
            user_level=spec.viewer.user_level,
            wealth_level=spec.viewer.wealth_level,
            guard_level=spec.viewer.guard_level,
            is_admin=spec.viewer.is_admin,
            is_anchor=spec.viewer.is_anchor,
            medal=(Medal(**spec.viewer.medal.model_dump()) if spec.viewer.medal else None),
        )
        gift = None
        if spec.gift is not None:
            gift_data = spec.gift.model_dump()
            gift = Gift(**gift_data)
        return LiveEvent(
            kind=spec.kind,
            room_id=990000 if spec.route == "crowd" else 0,
            viewer=viewer,
            text=spec.text,
            gift=gift,
            value_cny=spec.value_cny,
            event_id=self._event_id(case, spec, run_id, index),
            ts_ms=int(self._clock.wall().timestamp() * 1000),
            recv_at=self._clock.monotonic(),
            raw={"mock_test": case.id},
        )

    @staticmethod
    def _event_id(case: MockTestCase, spec: MockEvent, run_id: int, index: int) -> str:
        discriminator = spec.dedup_group or str(index)
        return f"ui-test:{run_id}:{case.id}:{discriminator}"

    def _run_action(self, case: MockTestCase, action: MockAction, run_id: int) -> None:
        if action.kind == "sc_revoke":
            event_id = f"ui-test:{run_id}:{case.id}:{action.target_group}"
            self._revoke(f"{EventKind.SUPER_CHAT.value}:{event_id}")

    def _publish(self, **data: object) -> None:
        self._state = dict(data)
        self._notify({"kind": "test", **data})
