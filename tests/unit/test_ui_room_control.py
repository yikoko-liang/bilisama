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


def test_a_failed_room_parks_on_the_error_instead_of_retrying_forever() -> None:
    async def run() -> None:
        import pytest

        from bilisama.ui.room_control import RoomConnectionError

        attempts: list[int] = []

        class _Broken:
            name = "bilibili"

            def __init__(self, room_id: int) -> None:
                self.room_id = room_id

            async def start(self, _emit: Callable[[LiveEvent], Awaitable[None]]) -> None:
                attempts.append(self.room_id)
                raise RuntimeError("房号不对，或接口被风控")

            async def stop(self) -> None:
                return

            def status(self) -> dict[str, Any]:
                return {"connected": False}

        source = SwitchableRoomSource(lambda rid: _Broken(rid))

        async def discard(_event: LiveEvent) -> None:
            return

        task = asyncio.create_task(source.start(discard))
        try:
            with pytest.raises(RoomConnectionError, match="风控"):
                await source.connect(404, timeout_s=1)
            await asyncio.sleep(0.1)
            assert attempts == [404], "无脑重试无效房间只是噪音——停在失败的房号上等新指令"
            assert "风控" in source.status()["error"]
        finally:
            await source.stop()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)

    asyncio.run(run())


def test_room_zero_parks_quietly_until_a_panel_choice_arrives() -> None:
    async def run() -> None:
        made: list[_Source] = []

        def factory(room_id: int) -> _Source:
            source = _Source(room_id)
            made.append(source)
            return source

        async def discard(_event: LiveEvent) -> None:
            return

        source = SwitchableRoomSource(factory, 0)
        task = asyncio.create_task(source.start(discard))
        await asyncio.sleep(0.05)
        try:
            assert made == [], "no room named means no transport built"
            assert source.status()["connected"] is False
            await source.connect(777, timeout_s=1)
            assert [s.room_id for s in made] == [777]
        finally:
            await source.stop()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
