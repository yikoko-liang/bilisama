"""A real room can be replaced without replacing the process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from bilisama.ingest.events import LiveEvent
from bilisama.ui.room_control import SwitchableRoomSource


class _Source:
    name = "bilibili"

    def __init__(self, room_id: int) -> None:
        self.room_id = room_id
        self.connected = False
        self.stopped = asyncio.Event()

    async def start(self, _emit: Callable[[LiveEvent], Awaitable[None]]) -> None:
        self.connected = True
        await self.stopped.wait()
        self.connected = False

    async def stop(self) -> None:
        self.stopped.set()

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "room_id": self.room_id}


def test_switches_room_and_disconnects_in_one_running_source() -> None:
    async def run() -> None:
        made: list[_Source] = []

        def factory(room_id: int) -> _Source:
            source = _Source(room_id)
            made.append(source)
            return source

        async def discard(_event: LiveEvent) -> None:
            return

        source = SwitchableRoomSource(factory)
        task = asyncio.create_task(source.start(discard))
        try:
            first = await source.connect(100, timeout_s=1)
            assert first["connected"] is True
            assert source.status()["active_room_id"] == 100

            second = await source.connect(200, timeout_s=1)
            assert second["connected"] is True
            assert made[0].stopped.is_set()
            assert source.status()["active_room_id"] == 200

            await source.disconnect()
            assert source.status()["connected"] is False
            assert source.status()["active_room_id"] == 0
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
