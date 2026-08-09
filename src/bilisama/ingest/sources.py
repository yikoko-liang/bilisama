"""事件源。

一个 ABC，不是两个。参考实现里 pull 风格的 EventSource 和 push 风格的
IntentSource 并存，同一条链路上要转换一次,合成一个就够了。

`speak` 开关只影响最后一步：**事件一律入库**（记忆、字幕、事件流照常），
只有产不产生 Intent 是开关。所以"不发声"不等于"不知道",
小助手照样知道阿强来了第五次，只是这次不吭声。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from bilisama.ingest.events import LiveEvent

EventSink = Callable[[LiveEvent], Awaitable[None]]


@runtime_checkable
class Source(Protocol):
    """事件源。直播接入、回放、以及以后的后台 agent 都实现它。

    装配处按配置决定注册哪些,这是「打开一级」的全部动作，不产生任何新分支。
    """

    name: str

    async def start(self, emit: EventSink) -> None:
        """开始产出事件。这个协程一直跑到 stop() 被调用。"""
        ...

    async def stop(self) -> None: ...


class QueueSource:
    """把一个 asyncio.Queue 包成 Source。测试和进程内注入用。"""

    def __init__(self, name: str = "queue", *, maxsize: int = 256) -> None:
        self.name = name
        # 有上限：消费不过来时上游被挡住，而不是无限堆积到 OOM
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._stopped = asyncio.Event()

    async def push(self, event: LiveEvent) -> None:
        await self._queue.put(event)

    async def start(self, emit: EventSink) -> None:
        while not self._stopped.is_set():
            event = await self._queue.get()
            if event is None:
                return
            await emit(event)

    async def stop(self) -> None:
        self._stopped.set()
        await self._queue.put(None)


async def merge(sources: list[Source], emit: EventSink) -> None:
    """并发跑多个源。

    注意 TaskGroup 的语义：任何一个源抛出非取消异常时，其余源会被一起取消，
    最后抛 ExceptionGroup。调用方负责重启。

    直播场景其实想要「一个源挂了不拖垮其余」,那需要给每个源包一层监督协程。
    等真接上多个源时再改（待办第 10 项会连测试一起补）。
    """
    async with asyncio.TaskGroup() as tg:
        for source in sources:
            tg.create_task(source.start(emit), name=f"source:{source.name}")


async def collect(source: Source, *, limit: int) -> list[LiveEvent]:
    """把源的前 N 条事件收进 list。测试用。"""
    out: list[LiveEvent] = []
    done = asyncio.Event()

    async def sink(event: LiveEvent) -> None:
        out.append(event)
        if len(out) >= limit:
            done.set()

    task = asyncio.create_task(source.start(sink))
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        await source.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return out
