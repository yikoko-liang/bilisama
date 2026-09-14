"""Scoring and the danmaku funnel: one winner per window, gifts aggregated.

The flood acceptance from plan section 15.11 B4 lives at the bottom: the
whole event_flood fixture through a real Assembly yields at most one danmaku
intent per window, paid events go out immediately, and every skipped event
has a reason on the books.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.derive import DerivedThresholds, derive
from bilisama.config.enums import Chattiness
from bilisama.director.intent import Intent
from bilisama.director.intents import intent_for
from bilisama.event_pacing import EventPacer
from bilisama.ingest.bilibili.scoring import TextSignal, danmaku_score, danmaku_text_signal
from bilisama.ingest.bilibili.selector import DanmakuSelector, SkipSink
from bilisama.ingest.events import (
    EventKind,
    GuardLevel,
    LiveEvent,
    Viewer,
)
from bilisama.obs.logging import setup as logging_setup
from bilisama.obs.outcome import SkipReason
from tests.fakes.bili import danmaku_event as _dm
from tests.fakes.bili import gift_event
from tests.fakes.replay import FIXTURE_DIR, replay_driving_clock
from tests.unit.conftest import build_assembly_kit

# setup() lowers these and never puts them back (src/bilisama/obs/logging.py).
_QUIETED = ("websockets", "asyncio", "aiohttp", "httpx", "uvicorn", "blivedm")


def _gift(uid: int, *, coin: int = 20000, event_id: str = "") -> LiveEvent:
    return gift_event(uid=uid, coin=coin, event_id=event_id)


@contextlib.contextmanager
def _json_log() -> Iterator[io.StringIO]:
    """The real setup() and the real formatter, writing into memory.

    Not caplog: what is under test is what the FORMATTER does to a field —
    audience text is folded by FIELD NAME (obs/logging.py's _VIEWER_CONTENT) —
    and caplog sees the record before any of that runs. Everything setup()
    stamps on global logging state goes back on the way out; it clears the root
    handlers, which would otherwise take caplog's own handler down with it for
    the rest of the session.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quieted = {name: logging.getLogger(name).level for name in _QUIETED}
    stream = io.StringIO()
    try:
        logging_setup(level="info", stream=stream)
        yield stream
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, saved in quieted.items():
            logging.getLogger(name).setLevel(saved)


def _lines(stream: io.StringIO, event: str) -> list[dict[str, Any]]:
    """Parsed log lines for one event name."""
    parsed = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [line for line in parsed if line["event"] == event]


def _fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Unformatted fields of every `event` record, in order."""
    return [
        dict(getattr(record, "fields", {}))
        for record in caplog.records
        if record.getMessage() == event
    ]


TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "tofu"

_ROOM = 777


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


# ------------------------------------------------------------------ text signal


def test_actionable_short_text_is_admitted_without_a_model_call() -> None:
    for text in (
        "为什么",
        "主播看下",
        "主播你说错了",
        "没声音了",
        "能不能试试",
    ):
        assert danmaku_text_signal(text) is TextSignal.HARD_ACCEPT, text


def test_the_assistants_configured_name_is_a_mention() -> None:
    """The default list carries role words only; the actual persona name
    arrives per-config through mention_terms, so a rename keeps working."""
    assert danmaku_text_signal("豆腐在吗") is TextSignal.HARD_ACCEPT, "吗 already admits it"
    assert danmaku_text_signal("hanako看这里") is TextSignal.SCORE
    assert danmaku_text_signal("hanako看这里", mention_terms=("hanako",)) is TextSignal.HARD_ACCEPT
    assert danmaku_text_signal("规划一下行程", mention_terms=("hana",)) is TextSignal.SCORE


def test_recent_dialogue_or_stream_topic_can_admit_a_short_relevant_comment() -> None:
    assert (
        danmaku_text_signal("芯片有意思", context_lines=("今晚介绍国产芯片和推理框架",))
        is TextSignal.HARD_ACCEPT
    )
    assert (
        danmaku_text_signal("量化挺稳", context_lines=("刚才主播说这个量化方案终于跑稳了",))
        is TextSignal.HARD_ACCEPT
    )


def test_low_information_text_is_rejected_before_scoring() -> None:
    for text in ("12345", "！！！！", "😂😂😂", "哈哈哈哈哈哈", "666", "来了", "好"):
        assert danmaku_text_signal(text) is TextSignal.REJECT, text


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
    *,
    window_s: int = 2,
    score: float = 0.35,
    context_lines: tuple[str, ...] = (),
    delivery_blocked: Callable[[], bool] | None = None,
    on_skip: SkipSink | None = None,
) -> tuple[DanmakuSelector, FakeClock, list[LiveEvent], asyncio.Task[None]]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    selector = DanmakuSelector(
        clock,
        thresholds=lambda: _thresholds(window_s, score),
        context_lines=lambda: context_lines,
        delivery_blocked=delivery_blocked,
        on_skip=on_skip,
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


async def test_one_window_preserves_all_candidates_under_capacity() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        for uid, text in enumerate(("主播这局到底怎么打", "为什么不先做饰品", "666"), 1):
            selector.offer(_dm(text, uid=uid))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1, 2, 3]
        assert selector.status()["skips"] == {}
    finally:
        await _finish(task)


async def test_answered_viewer_can_win_again_without_a_uid_cooldown() -> None:
    """The 60s per-viewer cooldown is gone: an answered viewer's follow-up is
    the best danmaku a co-host can pick, and it competes immediately."""
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("主播这个怎么设置的", uid=1))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1]
        selector.offer(_dm("那这个参数为什么是零", uid=1))  # same viewer, better message
        selector.offer(_dm("什么时候开新档", uid=2))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1, 1, 2]
        skips = selector.status()["skips"]
        assert isinstance(skips, dict) and "selection.uid_cooldown" not in skips
    finally:
        await _finish(task)


async def test_plain_statement_reaches_model_regardless_of_old_score_bar() -> None:
    selector, clock, delivered, task = await _selector(score=0.99)
    try:
        selector.offer(_dm("这波操作还行", uid=1))
        await clock.advance(2.5)
        assert [e.text for e in delivered] == ["这波操作还行"]
        assert not selector.status()["window_open"]
    finally:
        await _finish(task)


async def test_low_information_is_for_model_to_judge() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("666", uid=1))
        await clock.advance(2.5)
        assert [e.text for e in delivered] == ["666"]
        assert selector.status()["skips"] == {}
    finally:
        await _finish(task)


async def test_streamer_speech_holds_candidates_then_releases_together() -> None:
    speaking = True
    selector, clock, delivered, task = await _selector(
        window_s=1, delivery_blocked=lambda: speaking
    )
    try:
        selector.offer(_dm("这个参数为什么是零", uid=1))
        await clock.advance(1.5)
        selector.offer(_dm("能不能解释一下这里的竞态", uid=2))
        await clock.advance(1.5)
        assert delivered == []
        assert selector.status()["deferred_count"] == 2
        speaking = False
        await clock.advance(0.5)
        assert [e.viewer.uid for e in delivered] == [1, 2]
        assert selector.status()["deferred_count"] == 0
    finally:
        await _finish(task)


async def test_blocked_delivery_does_not_report_success_or_lose_the_candidate() -> None:
    blocked = True
    selector, clock, delivered, task = await _selector(
        window_s=1,
        delivery_blocked=lambda: blocked,
    )
    try:
        selector.offer(_dm("没声音了，能检查一下吗", uid=1))
        await clock.advance(3.0)

        assert delivered == []
        assert selector.status()["delivered"] == 0
        assert selector.status()["deferred_count"] == 1

        blocked = False
        await clock.advance(0.5)
        assert [event.viewer.uid for event in delivered] == [1]
        assert selector.status()["delivered"] == 1
    finally:
        await _finish(task)


async def test_streamer_speech_starts_neither_event_budget_nor_intent_ttl() -> None:
    """A winner held during host speech is a candidate, not an intent: its TTL
    starts when it is released, and the ordinary budget is charged then too —
    otherwise a 25s monologue expires the reply before it can be spoken."""
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    speaking = True
    pacer = EventPacer(clock, chattiness=lambda: Chattiness.MEDIUM)
    intents: list[Intent] = []
    selector = DanmakuSelector(
        clock,
        thresholds=lambda: _thresholds(window_s=1),
        delivery_blocked=lambda: speaking or not pacer.can_consume("danmaku"),
    )

    async def deliver(event: LiveEvent) -> None:
        assert pacer.try_consume("danmaku")
        intent = intent_for(event, now=clock.monotonic())
        assert intent is not None
        intents.append(intent)

    task = asyncio.create_task(selector.run(deliver))
    await asyncio.sleep(0)
    try:
        selector.offer(_dm("这个参数为什么是零", uid=1))
        await clock.advance(25.0)

        assert intents == []
        assert selector.status()["deferred_count"] == 1
        held = pacer.status()
        assert held["ordinary_tokens"] == 2.0
        assert held["consumed"] == {}
        assert held["denied"] == {}

        speaking = False
        await clock.advance(0.5)

        assert len(intents) == 1
        assert intents[0].created_at >= 25.0
        assert intents[0].expires_at is not None
        assert intents[0].expires_at >= clock.monotonic() + 4.5
        released = pacer.status()
        released_tokens = released["ordinary_tokens"]
        assert isinstance(released_tokens, float)
        assert 1.0 <= released_tokens < 1.1
        assert released["consumed"] == {"danmaku": 1}
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


async def test_hard_accept_bypasses_score_bar_but_still_uses_the_window() -> None:
    selector, clock, delivered, task = await _selector(score=0.95)
    try:
        selector.offer(_dm("没声音了", uid=1))
        assert delivered == []
        await clock.advance(2.5)
        assert [event.text for event in delivered] == ["没声音了"]
        assert selector.status()["passes"] == {"model": 1}
    finally:
        await _finish(task)


async def test_context_related_text_bypasses_score_bar() -> None:
    selector, clock, delivered, task = await _selector(
        score=0.95, context_lines=("本场正在介绍国产芯片架构",)
    )
    try:
        selector.offer(_dm("芯片有意思", uid=1))
        await clock.advance(2.5)
        assert [event.text for event in delivered] == ["芯片有意思"]
    finally:
        await _finish(task)


async def test_same_content_from_different_viewers_reaches_model_together() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        selector.offer(_dm("这个模型的推理速度挺快", uid=1, event_id="one"))
        selector.offer(_dm("这个模型的推理速度挺快", uid=2, event_id="two"))
        await clock.advance(2.5)
        assert [e.viewer.uid for e in delivered] == [1, 2]
        assert selector.status()["skips"] == {}
    finally:
        await _finish(task)


async def test_repeated_content_filter_expires_after_two_minutes() -> None:
    selector, clock, delivered, task = await _selector(score=0.1)
    try:
        text = "这个模型的推理速度挺快"
        selector.offer(_dm(text, uid=1, event_id="dm:early"))
        await clock.advance(2.5)
        await clock.advance(121.0)
        selector.offer(_dm(text, uid=2, event_id="dm:later"))
        await clock.advance(2.5)
        assert [event.viewer.uid for event in delivered] == [1, 2]
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
    selector = DanmakuSelector(clock, thresholds=lambda: _thresholds())

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


# ------------------------------------------------------------ per-event skips (#49)


async def test_capacity_drop_hands_out_the_exact_event() -> None:
    records: list[tuple[LiveEvent | None, SkipReason]] = []
    selector, clock, delivered, task = await _selector(on_skip=lambda e, r: records.append((e, r)))
    try:
        events = [_dm("需要看看", uid=uid) for uid in range(10)]
        for event in events:
            selector.offer(event)
        await clock.advance(2.5)
        assert delivered == events[2:]
        assert records == [(events[0], SkipReason.QUEUE_FULL), (events[1], SkipReason.QUEUE_FULL)]
    finally:
        await _finish(task)


async def test_idle_does_not_manufacture_empty_windows() -> None:
    records: list[tuple[LiveEvent | None, SkipReason]] = []
    selector, clock, delivered, task = await _selector(on_skip=lambda e, r: records.append((e, r)))
    try:
        await clock.advance(5)
        assert delivered == []
        assert records == []
        assert not selector.status()["window_open"]
    finally:
        await _finish(task)


async def test_a_broken_skip_sink_never_takes_the_funnel_down_with_it() -> None:
    calls = 0

    def exploding(event: LiveEvent | None, reason: SkipReason) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("面板连接断了")

    selector, clock, delivered, task = await _selector(on_skip=exploding)
    try:
        event = _dm("这里怎么设置", uid=1)
        selector.offer(event)
        selector.offer(event)
        await clock.advance(2.5)
        assert delivered == [event]
        assert calls == 1
    finally:
        await _finish(task)


async def test_no_sink_configured_still_accounts_transport_duplicates() -> None:
    selector, clock, delivered, task = await _selector()
    try:
        event = _dm("666", uid=1)
        selector.offer(event)
        selector.offer(event)
        await clock.advance(2.5)
        assert delivered == [event]
        assert selector.status()["skips"] == {SkipReason.DUPLICATE.value: 1}
    finally:
        await _finish(task)


# ------------------------------------------------------------------ assembly routing


def _assembly(
    tmp_path: Path, *, chattiness: Chattiness = Chattiness.HIGH
) -> tuple[Assembly, DanmakuSelector, list[Intent], FakeClock]:
    kit_clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    selector = DanmakuSelector(kit_clock, thresholds=lambda: derive(chattiness))
    kit = build_assembly_kit(tmp_path, selector=selector)
    # One clock: the selector was built first, so rebind it to the kit's.
    selector._clock = kit.clock
    return kit.assembly, selector, kit.intents, kit.clock


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
    loop = asyncio.create_task(
        selector.run(assembly.deliver_selected, deliver_batch=assembly.deliver_danmaku_batch)
    )
    await asyncio.sleep(0)
    try:
        sc_done = False
        flood = replay_driving_clock(clock, FIXTURE_DIR / "event_flood.jsonl", room_id=_ROOM)
        async for event in flood:
            await assembly.on_event(event)
            if not sc_done and clock.monotonic() > 5.0:
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


# ------------------------------------------------------------ what the log says


async def test_batch_log_has_count_without_viewer_content() -> None:
    body = "主播这局到底怎么打"
    with _json_log() as stream:
        selector, clock, delivered, task = await _selector()
        try:
            selector.offer(_dm(body, uid=1))
            await clock.advance(2.5)
        finally:
            await _finish(task)
    assert len(delivered) == 1
    lines = _lines(stream, "selector.batch_delivered")
    assert len(lines) == 1
    assert lines[0]["count"] == 1
    assert body not in stream.getvalue()


async def test_transport_drop_leaves_a_debug_line_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    selector, clock, delivered, task = await _selector()
    try:
        event = _dm("需要看看", uid=1)
        selector.offer(event)
        selector.offer(event)
        await clock.advance(2.5)
    finally:
        await _finish(task)
    reasons = [fields["reason"] for fields in _fields(caplog, "selector.skipped")]
    assert reasons == [SkipReason.DUPLICATE.value]
    assert delivered == [event]


async def test_the_score_the_window_used_is_the_score_the_log_shows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Score constituents are debug-only, and they have to add up to the number
    the window actually compared against the bar — a breakdown that disagrees
    with the decision is worse than none."""
    caplog.set_level(logging.DEBUG)
    event = _dm("主播这个参数为什么是零", uid=7, medal_level=20)

    assert danmaku_score(event) > 0.0
    scored = _fields(caplog, "scoring.danmaku_scored")
    assert len(scored) == 1
    parts = {key: value for key, value in scored[0].items() if key.endswith("_score")}
    assert sum(parts.values()) == pytest.approx(scored[0]["score"], abs=1e-4)
    assert scored[0]["score"] == pytest.approx(danmaku_score(event), abs=1e-4)
    assert scored[0]["identity"] == "uid:7"
    assert parts["question_score"] > 0.0, "为什么 is a question"
    assert parts["medal_score"] > 0.0, "this room's medal"
