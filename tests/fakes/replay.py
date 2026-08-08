"""JSONL 回放源。

放在 tests/ 而不是生产树里,它是测试设施，不是产品的一部分。

fixture 的格式刻意贴近平台原始负载的形状，这样解析逻辑本身也被测到，
而不是喂已经解析好的 dataclass 进去自欺欺人。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bilisama.clock import Clock, SystemClock
from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Medal,
    Viewer,
    cny_from_gold,
)
from bilisama.ingest.sources import EventSink

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def parse_line(raw: dict[str, Any], *, room_id: int = 0) -> LiveEvent:
    """把 fixture 的一行变成 LiveEvent。

    这里就是「绝不因 uid==0 丢弃」落地的地方：uid 缺失或为 0 时照样构造
    Viewer，靠 uid_hash 给出稳定身份。
    """
    kind = EventKind(raw["kind"])
    v = raw.get("viewer", {})
    viewer = Viewer(
        uid=int(v.get("uid", 0) or 0),
        uid_hash=str(v.get("uid_hash", "")),
        name=str(v.get("name", "")),
        user_level=int(v.get("user_level", 0)),
        wealth_level=int(v.get("wealth_level", 0)),
        guard_level=GuardLevel.from_wire(int(v.get("guard_level", 0))),
        is_admin=bool(v.get("is_admin", False)),
        medal=(
            Medal(
                name=str(v["medal"].get("name", "")),
                level=int(v["medal"].get("level", 0)),
                up_name=str(v["medal"].get("up_name", "")),
                anchor_room_id=int(v["medal"].get("anchor_room_id", 0)),
            )
            if v.get("medal")
            else None
        ),
    )

    gift = None
    value_cny = float(raw.get("value_cny", 0.0))
    if g := raw.get("gift"):
        total_coin = int(g.get("total_coin", 0))
        gift = Gift(
            gift_id=int(g.get("gift_id", 0)),
            name=str(g.get("name", "")),
            num=int(g.get("num", 1)),
            coin_type=str(g.get("coin_type", "")),
            total_coin=total_coin,
            combo_id=str(g.get("combo_id", "")),
            combo_count=int(g.get("combo_count", 0)),
            combo_end=g.get("combo_end"),
        )
        if not value_cny and gift.is_paid:
            value_cny = cny_from_gold(total_coin)

    return LiveEvent(
        kind=kind,
        room_id=room_id or int(raw.get("room_id", 0)),
        viewer=viewer,
        text=str(raw.get("text", "")),
        gift=gift,
        value_cny=value_cny,
        event_id=str(raw.get("event_id", "")),
        ts_ms=int(raw.get("ts_ms", 0)),
        raw=raw,
    )


def read_fixture(path: Path, *, room_id: int = 0) -> Iterator[tuple[float, LiveEvent]]:
    """读 JSONL，产出 (相对开播的秒数, 事件)。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        yield float(raw.get("at_s", 0.0)), parse_line(raw, room_id=room_id)


@dataclass(slots=True)
class ReplaySource:
    """按 fixture 里的时间戳回放。

    speed 是倍速：1.0 是实时（看真实节奏用），大值把 20 秒窗口压到毫秒。
    speed <= 0 表示完全不等待,断言事件顺序和解析时用这个，最确定性。

    注意 FakeClock 需要有人调 advance() 才会走，所以配 FakeClock 时要么由测试
    驱动时间，要么用 speed=0。
    """

    path: Path
    name: str = "replay"
    speed: float = 1000.0
    room_id: int = 0
    clock: Clock = field(default_factory=SystemClock)
    loop_count: int = 1

    _stopped: asyncio.Event | None = None

    async def start(self, emit: EventSink) -> None:
        self._stopped = asyncio.Event()
        for _ in range(self.loop_count):
            cursor = 0.0
            for at_s, event in read_fixture(self.path, room_id=self.room_id):
                if self._stopped.is_set():
                    return
                if self.speed > 0:
                    delay = max(0.0, at_s - cursor) / self.speed
                    if delay:
                        await self.clock.sleep(delay)
                else:
                    await asyncio.sleep(0)  # 让出控制权，但不消耗时间
                cursor = at_s
                await emit(event)

    async def stop(self) -> None:
        if self._stopped is not None:
            self._stopped.set()


def fixture(name: str) -> Path:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"没有这个 fixture：{path}")
    return path
