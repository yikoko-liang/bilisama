"""Scoring and the danmaku funnel: one winner per window, gifts aggregated.

The flood acceptance from plan section 15.11 B4 lives at the bottom: the
whole event_flood fixture through a real Assembly yields at most one danmaku
intent per window, paid events go out immediately, and every skipped event
has a reason on the books.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.derive import DerivedThresholds, derive
from bilisama.config.enums import Chattiness
from bilisama.config.schema import GrowthSwitches, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent
from bilisama.ingest.bilibili.scoring import danmaku_score
from bilisama.ingest.bilibili.selector import DanmakuSelector
from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Medal,
    Viewer,
)
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop
from tests.fakes.replay import FIXTURE_DIR, read_fixture

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "mia"

_ROOM = 777


def _dm(
    text: str,
    uid: int = 1,
    *,
    guard: GuardLevel = GuardLevel.NONE,
    medal_level: int = 0,
    medal_room: int = _ROOM,
    admin: bool = False,
    user_level: int = 0,
    event_id: str = "",
) -> LiveEvent:
    viewer = Viewer(
        uid=uid,
        name=f"观众{uid}",
        guard_level=guard,
        is_admin=admin,
        user_level=user_level,
        medal=(
            Medal(name="牌子", level=medal_level, anchor_room_id=medal_room)
            if medal_level
            else None
        ),
    )
    return LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=_ROOM,
        viewer=viewer,
        text=text,
        event_id=event_id or f"{uid}:{text}",
    )


def _gift(uid: int, *, coin: int = 20000, event_id: str = "") -> LiveEvent:
    viewer = Viewer(uid=uid, name=f"老板{uid}")
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=_ROOM,
        viewer=viewer,
        gift=Gift(
            gift_id=30607,
            name="小星星",
            num=1,
            coin_type="gold",
            total_coin=coin,
            combo_id=f"{viewer.identity}:30607",
        ),
        value_cny=coin / 1000.0,
        event_id=event_id or f"gift:{uid}",
    )


# ------------------------------------------------------------------ scoring


def test_spam_never_clears_even_the_lowest_bar() -> None:
    for text in ("666", "哈哈哈哈哈哈哈哈", "！！！！！！"):
        assert danmaku_score(_dm(text)) < derive(Chattiness.HIGH).score_threshold, text


def test_plain_viewers_question_passes_medium_but_not_low() -> None:
    score = danmaku_score(_dm("主播今天玩什么"))
    assert derive(Chattiness.MEDIUM).score_threshold <= score
    assert score < derive(Chattiness.LOW).score_threshold


def test_neko_ordering_guard_over_admin_over_medal_over_plain() -> None:
    text = "这波操作可以的"
    plain = danmaku_score(_dm(text))
    medal = danmaku_score(_dm(text, medal_level=40))
    admin = danmaku_score(_dm(text, admin=True))
    captain = danmaku_score(_dm(text, guard=GuardLevel.CAPTAIN))
    assert plain < medal < admin < captain, "livedanmaku.py:477's ordering, renormalised"


def test_another_rooms_medal_counts_for_nothing() -> None:
    text = "路过看看"
    ours = danmaku_score(_dm(text, medal_level=20))
    theirs = danmaku_score(_dm(text, medal_level=20, medal_room=999))
    assert theirs == danmaku_score(_dm(text))
    assert ours > theirs


def test_repetition_is_discounted_not_rewarded() -> None:
    spam = danmaku_score(_dm("哈哈哈哈哈哈哈哈哈哈哈哈"))
    substance = danmaku_score(_dm("今天的代码到底哪里出了问题"))
    assert substance > spam


# ------------------------------------------------------------------ selector


def _thresholds(window_s: int = 2, score: float = 0.35) -> DerivedThresholds:
    return DerivedThresholds(
        idle_threshold_s=90,
        danmaku_window_s=window_s,
        score_threshold=score,
        cooldown_s=12,
        max_output_tokens=120,
    )


async def _selector(
    *, window_s: int = 2, score: float = 0.35, cooldown_s: float = 60.0
) -> tuple[DanmakuSelector, FakeClock, list[LiveEvent], asyncio.Task[None]]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    selector = DanmakuSelector(
        clock, thresholds=lambda: _thresholds(window_s, score), per_uid_cooldown_s=cooldown_s
    )
    delivered: list[LiveEvent] = []

    async def deliver(event: LiveEvent) -> None:
        delivered.append(event)

    task = asyncio.create_task(selector.run(deliver))
    await asyncio.sleep(0)
    return selector, clock, delivered, task


async def _finish(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_one_window_one_winner_and_losers_are_accounted() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("主播这局到底怎么打", uid=1))
        selector.offer(_dm("为什么不先做饰品", uid=2, medal_level=20))  # higher score
        selector.offer(_dm("666", uid=3))  # below the bar
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [2]
        skips = selector.status()["skips"]
        assert isinstance(skips, dict)
        assert skips["selection.lost_window"] == 1
        assert skips["selection.low_value"] == 1
    finally:
        await _finish(task)


async def test_answered_viewer_cools_down_and_someone_else_wins() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("主播这个怎么设置的", uid=1))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1]
        selector.offer(_dm("那这个参数为什么是零", uid=1))  # same viewer, better message
        selector.offer(_dm("什么时候开新档", uid=2))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1, 2]
        skips = selector.status()["skips"]
        assert isinstance(skips, dict) and skips["selection.uid_cooldown"] == 1
    finally:
        await _finish(task)


async def test_empty_window_is_a_counter_not_a_mystery() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("666", uid=1))  # opens the window, fails the bar
        await clock.advance(2.5)
        assert delivered == []
        skips = selector.status()["skips"]
        assert isinstance(skips, dict) and skips["selection.window_empty"] == 1
    finally:
        await _finish(task)


async def test_transport_replay_is_deduped_by_the_ring() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        event = _dm("主播为什么选这个", uid=1, event_id="dm:42")
        selector.offer(event)
        selector.offer(event)  # blivedm inner-reconnect replay, same instant
        await clock.advance(2.5)
        assert len(delivered) == 1
        skips = selector.status()["skips"]
        assert isinstance(skips, dict) and skips["selection.duplicate"] == 1
    finally:
        await _finish(task)


async def test_gifts_settle_on_idle_not_on_the_window() -> None:
    selector, clock, delivered, task = await _selector(window_s=20)
    try:
        selector.offer(_gift(9))
        await clock.advance(1.5)  # combo idle 1.0s < one 20s window
        assert len(delivered) == 1
        gift = delivered[0].gift
        assert gift is not None and gift.aggregated_count == 1
    finally:
        await _finish(task)


async def test_three_delivery_failures_latch_the_breaker_for_the_run() -> None:
    """Deliver is pure intent construction plus a queue push: its failures
    are bugs, not weather, so the latch holds until restart — and the failed
    combo stays pending rather than being silently discarded."""
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    selector = DanmakuSelector(clock, thresholds=lambda: _thresholds(), per_uid_cooldown_s=60.0)

    async def deliver(event: LiveEvent) -> None:
        raise RuntimeError("下游炸了")

    task = asyncio.create_task(selector.run(deliver))
    await asyncio.sleep(0)
    try:
        selector.offer(_gift(11))
        await clock.advance(2.0)  # several ticks: each retry counts one failure
        assert selector.status()["breaker_open"] is True
        assert selector.status()["combos_suppressed"] == 0, "never falsely settled"
        selector.offer(_dm("现在还有人在吗", uid=1))
        skips = selector.status()["skips"]
        assert isinstance(skips, dict) and skips["selection.breaker_open"] == 1
    finally:
        await _finish(task)


# ------------------------------------------------------------------ assembly routing


def _assembly(
    tmp_path: Path, *, chattiness: Chattiness = Chattiness.HIGH
) -> tuple[Assembly, DanmakuSelector, list[Intent], FakeClock]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    growth = GrowthSwitches()
    speak = SpeakSwitches()
    distiller = Distiller(None, store, persona, growth, clock)
    intents: list[Intent] = []
    proactive = ProactiveTopicLoop(
        None,
        store,
        SpeakingFloor(clock),
        clock,
        submit=intents.append,
        prompt="",
        idle_threshold_s=90.0,
    )

    async def push(text: str) -> None:
        return None

    selector = DanmakuSelector(
        clock, thresholds=lambda: derive(chattiness), per_uid_cooldown_s=60.0
    )
    assembly = Assembly(
        store=store,
        distiller=distiller,
        proactive=proactive,
        persona=persona,
        growth=growth,
        speak_enabled=lambda source: bool(getattr(speak, source, False)),
        submit=intents.append,
        push_context=push,
        clock=clock,
        selector=selector,
    )
    return assembly, selector, intents, clock


async def test_super_chat_bypasses_the_funnel_entirely(tmp_path: Path) -> None:
    assembly, _selector, intents, _clock = _assembly(tmp_path)
    sc = LiveEvent(
        kind=EventKind.SUPER_CHAT,
        room_id=_ROOM,
        viewer=Viewer(uid=77, name="金主"),
        text="能出个教程吗",
        value_cny=30.0,
        event_id="sc:1",
    )
    await assembly.on_event(sc)
    assert [i.source for i in intents] == ["super_chat"], "no window wait for paid attention"


async def test_event_flood_one_danmaku_intent_per_window_paid_immediate(tmp_path: Path) -> None:
    """The B4 acceptance, against the real fixture and a real Assembly."""
    assembly, selector, intents, clock = _assembly(tmp_path, chattiness=Chattiness.HIGH)
    loop = asyncio.create_task(selector.run(assembly.deliver_selected))
    await asyncio.sleep(0)
    try:
        cursor = 0.0
        sc_done = False
        for at_s, event in read_fixture(FIXTURE_DIR / "event_flood.jsonl", room_id=_ROOM):
            if at_s > cursor:  # the fixture tail walks backwards (backlog #8)
                await clock.advance(at_s - cursor)
                cursor = at_s
            await assembly.on_event(event)
            if not sc_done and at_s > 5.0:
                await assembly.on_event(
                    LiveEvent(
                        kind=EventKind.SUPER_CHAT,
                        room_id=_ROOM,
                        viewer=Viewer(uid=888, name="金主"),
                        text="加油",
                        value_cny=30.0,
                        event_id="sc:mid",
                    )
                )
                assert any(i.source == "super_chat" for i in intents), "paid waits for nothing"
                sc_done = True
        # Close the one 12s window the 11s flood opened, and settle combos.
        await clock.advance(15.0)

        danmaku = [i for i in intents if i.source == "danmaku"]
        gifts = [i for i in intents if i.source == "gift"]
        assert len(danmaku) <= 1, "one window, at most one danmaku intent"
        assert len(gifts) == 3, "three viewers' combos, one aggregate each"
        status = selector.status()
        skips = status["skips"]
        assert isinstance(skips, dict)
        assert status["offered"] == 203
        delivered = status["delivered"]
        assert isinstance(delivered, int)
        per_event_skips = sum(
            n for reason, n in skips.items() if reason != "selection.window_empty"
        )
        assert per_event_skips + delivered == 203, "every event ends in exactly one account"
    finally:
        await _finish(loop)
