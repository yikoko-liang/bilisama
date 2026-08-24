"""A Bilibili room source that can be replaced without restarting dev-talk."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from bilisama.ingest.sources import EventSink

__all__ = ["RoomConnectionError", "SwitchableRoomSource"]


class _RoomSource(Protocol):
    name: str

    async def start(self, emit: EventSink) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


RoomFactory = Callable[[int], _RoomSource]


class RoomConnectionError(ConnectionError):
    """The requested room did not reach a connected state."""


class SwitchableRoomSource:
    """Own one room connection and swap it when the panel changes room id."""

    name = "bilibili"

    def __init__(self, factory: RoomFactory, room_id: int = 0) -> None:
        self._factory = factory
        self._target_room_id = room_id
        self._active_room_id = 0
        self._source: _RoomSource | None = None
        self._changed = asyncio.Event()
        self._stopped = False
        self._error = ""

    async def start(self, emit: EventSink) -> None:
        while not self._stopped:
            if self._target_room_id <= 0:
                self._changed.clear()
                await self._changed.wait()
                continue
            wanted = self._target_room_id
            source = self._factory(wanted)
            self._source = source
            self._active_room_id = wanted
            self._error = ""
            source_task = asyncio.create_task(source.start(emit), name=f"room:{wanted}")
            changed_task = asyncio.create_task(self._changed.wait(), name="room:changed")
            done, _ = await asyncio.wait(
                {source_task, changed_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if changed_task in done:
                self._changed.clear()
                await source.stop()
                source_task.cancel()
                await asyncio.gather(source_task, return_exceptions=True)
            else:
                changed_task.cancel()
                await asyncio.gather(changed_task, return_exceptions=True)
                try:
                    source_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._error = str(exc)[:200]
                    # Stay parked on the failed id. A new panel choice wakes
                    # the loop; retrying an invalid room forever is just noise.
                    self._changed.clear()
                    await self._changed.wait()
                    self._changed.clear()
            self._source = None
            self._active_room_id = 0

    async def stop(self) -> None:
        self._stopped = True
        self._changed.set()
        if self._source is not None:
            await self._source.stop()

    async def connect(self, room_id: int, *, timeout_s: float = 12.0) -> dict[str, Any]:
        """Switch rooms and wait until the new transport reports connected."""
        if room_id <= 0:
            raise RoomConnectionError("直播间号必须大于 0")
        self._target_room_id = room_id
        self._error = ""
        self._changed.set()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    state = self.status()
                    if (
                        state.get("connected")
                        and state.get("requested_room_id") == room_id
                        and state.get("active_room_id") == room_id
                    ):
                        return state
                    if self._error and self._active_room_id == room_id:
                        raise RoomConnectionError(self._error)
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise RoomConnectionError(f"连接直播间 {room_id} 超时") from exc

    async def disconnect(self, *, timeout_s: float = 12.0) -> None:
        """Stop the current transport and return only after status is offline."""
        self._target_room_id = 0
        self._error = ""
        self._changed.set()
        try:
            async with asyncio.timeout(timeout_s):
                while self._active_room_id or bool(self.status().get("connected")):
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise RoomConnectionError("断开直播间事件流超时") from exc

    def status(self) -> dict[str, Any]:
        active = self._source.status() if self._source is not None else {}
        return {
            **active,
            "connected": bool(active.get("connected")),
            "requested_room_id": self._target_room_id,
            "active_room_id": self._active_room_id,
            "error": self._error,
        }
