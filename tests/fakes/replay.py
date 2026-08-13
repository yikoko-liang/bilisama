"""Replay live events from a JSONL fixture.

Lives under tests/ rather than in the package: it is test infrastructure, not part
of the product.

Fixtures deliberately mirror the shape of the platform's raw payloads, so the
parsing code gets exercised too. Feeding in pre-built dataclasses would test
nothing but the fixture.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
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
    """Turn one fixture line into a LiveEvent.

    This is where "never drop an event because uid is 0" actually happens: a
    missing or zero uid still produces a Viewer, with uid_hash carrying identity.
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
    """Read a JSONL fixture as (seconds since stream start, event) pairs."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        yield float(raw.get("at_s", 0.0)), parse_line(raw, room_id=room_id)


@dataclass(slots=True)
class ReplaySource:
    """Replay a fixture at its recorded timing.

    `speed` is a multiplier: 1.0 is real time, useful for watching the actual
    rhythm; a large value squeezes a 20-second window into milliseconds. Zero or
    less skips waiting entirely, which is what you want when asserting on ordering
    or parsing.

    A FakeClock only moves when someone calls advance(), so pair it either with a
    test that drives time or with speed=0.
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
                    await asyncio.sleep(0)  # yield without consuming time
                cursor = at_s
                await emit(event)

    async def stop(self) -> None:
        if self._stopped is not None:
            self._stopped.set()


def fixture(name: str) -> Path:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"no such fixture: {path}")
    return path


async def replay_driving_clock(
    clock: Any, path: Path, *, room_id: int = 0
) -> AsyncIterator[LiveEvent]:
    """Yield fixture events while advancing a FakeClock to each at_s.

    The timing walk two suites used to hand-roll: advance-then-yield, so the
    caller's sink observes each event at its recorded moment and can inject
    extra events mid-replay between iterations.
    """
    cursor = 0.0
    for at_s, event in read_fixture(path, room_id=room_id):
        if at_s > cursor:
            await clock.advance(at_s - cursor)
            cursor = at_s
        yield event
