"""Tier 1 distillation: budgets, switches, and the anchor invariant.

The two stage-3 acceptance properties live here: the growth switches gate
exactly what they claim (off = nothing distilled or written), and a full
distill cycle leaves the anchor files byte-identical — the machine has no
path to them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bilisama.clock import FakeClock
from bilisama.config.schema import GrowthSwitches
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.distill import Distiller, _parse_json
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.side import SideModelError

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "mia"


class FakeSide:
    """Canned side model. Records every call so tests can count them."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[dict[str, str]] = []

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls.append({"system": system, "user": user})
        return self.replies.pop(0) if self.replies else "{}"

    async def aclose(self) -> None:
        return None


def _batch_reply(**overrides: object) -> str:
    payload: dict[str, object] = {
        "viewer_facts": [{"identity": "uid:1001", "fact": "爱聊猫", "tags": ["宠物"]}],
        "session_summary": "聊了编译器和猫",
        "relationship": ["观众给主播起了外号「卷王」"],
        "voice": ["这把稳了，稳得一批"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _make(
    tmp_path: Path,
    *,
    replies: list[str] | None = None,
    growth: GrowthSwitches | None = None,
    guard: object = None,
    clock: FakeClock | None = None,
) -> tuple[Distiller, MemoryStore, PersonaStore, FakeSide]:
    # Pass a clock in when the test has to drive it: the B18 retry sleeps, and
    # FakeClock only moves when someone calls advance().
    clock = clock or FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.on_event(
        LiveEvent(kind=EventKind.DANMAKU, viewer=Viewer(uid=1001, name="阿强"), text="猫呢")
    )
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    side = FakeSide(replies or [_batch_reply()])
    distiller = Distiller(
        side,
        store,
        persona,
        growth or GrowthSwitches(),
        clock,
        every_n_events=3,
        guard=guard,  # type: ignore[arg-type]
    )
    return distiller, store, persona, side


def _fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """The `fields=` payload of every record carrying this event name."""
    return [
        getattr(record, "fields", {}) for record in caplog.records if record.getMessage() == event
    ]


class FailingSide:
    """Raises SideModelError for the first `fail_times` calls, then answers.

    FakeSide never fails, so nothing exercised the distiller's error paths —
    the rolling summary's single catch and the batch's one-retry-then-give-up.
    """

    def __init__(self, *, fail_times: int, reply: str = "") -> None:
        self.fail_times = fail_times
        self.reply = reply
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise SideModelError(f"侧路模型返回 503: 第 {self.calls} 次")
        return self.reply

    async def aclose(self) -> None:
        return None


class BlockingSide:
    """Parks inside complete() until released — the window every race lives in."""

    def __init__(self, reply: str = "迟到的摘要") -> None:
        self.gate = asyncio.Event()
        self.reply = reply
        self.calls = 0

    async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
        self.calls += 1
        await self.gate.wait()
        return self.reply

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------ rolling summary


async def test_rolling_summary_lands_in_the_stream_fact(tmp_path: Path) -> None:
    distiller, store, _persona, side = _make(tmp_path, replies=["弹幕在聊主播的猫"])
    report = await distiller.rolling_summary()
    assert report.ran
    assert store.facts("stream", str(store.stream_id))[0].text == "弹幕在聊主播的猫"
    assert len(side.calls) == 1


async def test_fingerprint_skips_an_unchanged_input(tmp_path: Path) -> None:
    """No new events, no LLM spend — the plan's cost discipline."""
    distiller, _store, _persona, side = _make(tmp_path, replies=["摘要一", "摘要二"])
    assert (await distiller.rolling_summary()).ran
    second = await distiller.rolling_summary()
    assert not second.ran
    assert second.reason == "fingerprint_unchanged"
    assert len(side.calls) == 1


async def test_rolling_summary_crossing_streams_writes_nothing(tmp_path: Path) -> None:
    """The inflight race (B-series): the side call comes back after the stream
    has already rolled over — its summary belongs to a world that no longer
    exists and must not be written into the new stream's facts."""
    distiller, store, _persona, _side = _make(tmp_path)
    side = BlockingSide()
    distiller._side = side
    task = asyncio.create_task(distiller.rolling_summary())
    for _ in range(50):
        if side.calls:
            break
        await asyncio.sleep(0)
    assert side.calls == 1, "the distill must be parked inside the side call"
    old_sid = store.stream_id
    store.end_stream()
    store.begin_stream()
    side.gate.set()
    report = await task
    assert not report.ran
    assert report.reason == "stream_moved_on"
    assert not store.facts("stream", str(old_sid)), "the dead stream must stay unwritten"
    assert not store.facts("stream", str(store.stream_id)), "the new stream too"


async def test_end_of_stream_runs_once_per_stream(tmp_path: Path) -> None:
    """The once-latch: a Ctrl-C plus a finally block means end_of_stream can be
    called twice for the same stream — the second must be a no-op, or growth
    entries land twice and budgets lie."""
    distiller, _store, persona, side = _make(
        tmp_path,
        replies=[_batch_reply(), _batch_reply()],
        growth=GrowthSwitches(relationship="collect", voice="collect"),
    )
    first = await distiller.end_of_stream()
    assert first.ran
    grown = persona.growth_entries("relationship")
    second = await distiller.end_of_stream()
    assert not second.ran
    assert second.reason == "already_ran"
    assert len(side.calls) == 1, "the second call must not spend a token"
    assert persona.growth_entries("relationship") == grown, "no double-applied growth"


async def test_note_event_fires_at_the_threshold_not_before(tmp_path: Path) -> None:
    distiller, store, _persona, side = _make(tmp_path, replies=["摘要"])
    distiller.note_event()
    distiller.note_event()
    before = distiller._state.inflight
    assert before is None, "below threshold nothing runs"
    distiller.note_event()
    inflight = distiller._state.inflight
    assert inflight is not None
    await inflight
    assert len(side.calls) == 1
    assert store.facts("stream", str(store.stream_id))


async def test_an_overlong_summary_is_clipped_never_erased(tmp_path: Path) -> None:
    distiller, store, _persona, _side = _make(tmp_path, replies=["长" * 400])
    await distiller.rolling_summary()
    text = store.facts("stream", str(store.stream_id))[0].text
    assert len(text) == 200


async def test_no_side_model_reports_instead_of_crashing(tmp_path: Path) -> None:
    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    distiller = Distiller(
        None, store, PersonaStore(tmp_path, TEMPLATE_ROOT), GrowthSwitches(), clock
    )
    assert (await distiller.rolling_summary()).reason == "no_side_model"
    assert (await distiller.end_of_stream()).reason == "no_side_model"


# ------------------------------------------------------------ end of stream


async def test_batch_applies_viewer_facts_and_summary(tmp_path: Path) -> None:
    distiller, store, _persona, _side = _make(tmp_path)
    report = await distiller.end_of_stream()
    assert report.ran
    assert [f.text for f in store.facts("viewer", "uid:1001")] == ["爱聊猫"]
    assert "编译器" in store.facts("stream", str(store.stream_id))[0].text


async def test_invented_identities_are_dropped(tmp_path: Path) -> None:
    """The model may only attach facts to viewers who were actually there."""
    reply = _batch_reply(viewer_facts=[{"identity": "uid:9999", "fact": "编造的", "tags": ["假"]}])
    distiller, store, _persona, _side = _make(tmp_path, replies=[reply])
    report = await distiller.end_of_stream()
    assert store.facts("viewer", "uid:9999") == []
    assert any("unknown_identity" in d for d in report.dropped)


async def test_the_final_summary_lands_under_the_stream_the_latch_recorded(
    tmp_path: Path,
) -> None:
    """One id per batch, captured at the door.

    The latch remembers the stream id read on entry; the final summary used to
    re-read the live one at write time. Anyone who moves end_stream() ahead of
    the batch — the ordering dev_talk.py happens to get right today — makes the
    two disagree: the summary lands under subject "0" while the latch holds the
    real stream, and nothing complains.
    """
    distiller, store, _persona, _side = _make(tmp_path)
    sid = store.stream_id

    class EndingSide:
        """Ends the stream from inside the call — the reordering, simulated."""

        async def complete(self, *, system: str, user: str, max_tokens: int = 512) -> str:
            store.end_stream()
            return _batch_reply()

        async def aclose(self) -> None:
            return None

    distiller._side = EndingSide()
    report = await distiller.end_of_stream()

    assert report.ran
    assert [f.text for f in store.facts("stream", str(sid))] == ["聊了编译器和猫"]
    assert store.facts("stream", "0") == [], "摘要不能落到已经关掉的场次号下面"


# ------------------------------------------------------------ error paths


async def test_a_failed_rolling_call_writes_nothing_and_stays_retryable(tmp_path: Path) -> None:
    """A 503 must cost the tick, not the stream.

    The fingerprint may not advance on a failure: it is the "已经蒸馏过这批事件"
    marker, and moving it here would make the next tick skip the same events as
    unchanged — one transient error and the session summary stops updating
    until new events arrive.
    """
    distiller, store, _persona, _side = _make(tmp_path)
    distiller._side = FailingSide(fail_times=1)

    report = await distiller.rolling_summary()

    assert not report.ran
    assert report.reason == "side_error"
    assert store.facts("stream", str(store.stream_id)) == []
    assert distiller._state.fingerprint == "", "失败不能算作已经蒸馏过"


async def test_the_batch_backs_off_and_retries_once_after_a_transient_failure(
    tmp_path: Path,
) -> None:
    """B18: the end-of-stream call is the only memory-sedimentation chance a
    whole stream gets (runbook.md:177), so one failure buys a second shot."""
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    distiller, store, persona, _side = _make(tmp_path, growth=growth, clock=clock)
    side = FailingSide(fail_times=1, reply=_batch_reply())
    distiller._side = side

    task = asyncio.create_task(distiller.end_of_stream())
    for _ in range(50):
        await asyncio.sleep(0)
    assert not task.done(), "重试之间必须真的退避，不能立刻打第二枪"
    assert side.calls == 1

    await clock.advance(2.0)
    report = await task

    assert report.ran
    assert side.calls == 2
    assert [f.text for f in store.facts("viewer", "uid:1001")] == ["爱聊猫"]
    assert persona.growth_entries("voice") == ["这把稳了，稳得一批"], "重试一次，不是写两遍"


async def test_two_batch_failures_write_nothing_and_leave_the_latch_open(
    tmp_path: Path,
) -> None:
    """Give up after the second try — and do not mark the stream as done.

    Nothing was written, so a later caller (the Ctrl-C path calls this twice)
    can safely try again; latching here would spend the stream's one chance on
    a call that produced nothing.
    """
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    distiller, store, persona, _side = _make(tmp_path, growth=growth, clock=clock)
    side = FailingSide(fail_times=2)
    distiller._side = side

    task = asyncio.create_task(distiller.end_of_stream())
    await clock.advance(2.0)
    report = await task

    assert not report.ran
    assert report.reason == "side_error"
    assert side.calls == 2, "两次就收手，不无限重试"
    assert store.facts("viewer", "uid:1001") == []
    assert not persona.growth_path("voice").exists()
    assert distiller._state.batch_done == set(), "一次没写成的批量不占用那把闩"


async def test_malformed_json_writes_nothing(tmp_path: Path) -> None:
    distiller, store, persona, _side = _make(tmp_path, replies=["这不是 JSON"])
    report = await distiller.end_of_stream()
    assert not report.ran
    assert report.reason == "bad_json"
    assert store.facts("viewer", "uid:1001") == []
    assert persona.growth_entries("voice") == []


# ------------------------------------------------------------ growth switches


async def test_growth_off_distills_nothing_and_writes_nothing(tmp_path: Path) -> None:
    """Off means off: the prompt pins empty arrays AND writes are refused,
    so even a disobedient model cannot grow the files."""
    distiller, _store, persona, side = _make(tmp_path, growth=GrowthSwitches())
    await distiller.end_of_stream()
    assert "固定给空数组" in side.calls[0]["user"]
    assert not persona.growth_path("relationship").exists()
    assert not persona.growth_path("voice").exists()


@pytest.mark.parametrize("mode", ["collect", "on"])
async def test_collect_and_on_both_land_growth_on_disk(tmp_path: Path, mode: str) -> None:
    """collect versus on differ at injection time, not at distill time."""
    growth = GrowthSwitches.model_validate({"relationship": mode, "voice": mode})
    distiller, _store, persona, side = _make(tmp_path, growth=growth)
    await distiller.end_of_stream()
    assert "口癖样本" in side.calls[0]["user"], "growth rules made it into the prompt"
    assert persona.growth_entries("voice") == ["这把稳了，稳得一批"]
    relationship = persona.growth_entries("relationship")
    assert len(relationship) == 1
    # Wall 2026-08-12 20:00 UTC is 04:00 on the 13th in China — exactly the
    # logical-day boundary, which belongs to the NEW day.
    assert relationship[0].startswith("2026-08-13 "), "entries carry the CST logical date"
    assert "卷王" in relationship[0]


async def test_one_layer_on_does_not_write_the_other(tmp_path: Path) -> None:
    growth = GrowthSwitches.model_validate({"relationship": "off", "voice": "on"})
    distiller, _store, persona, _side = _make(tmp_path, growth=growth)
    await distiller.end_of_stream()
    assert persona.growth_entries("voice")
    assert not persona.growth_path("relationship").exists()


async def test_guard_blocks_a_growth_entry_before_disk(tmp_path: Path) -> None:
    growth = GrowthSwitches.model_validate({"voice": "on"})
    distiller, _store, persona, _side = _make(
        tmp_path, growth=growth, guard=lambda text: "稳得一批" in text
    )
    report = await distiller.end_of_stream()
    assert persona.growth_entries("voice") == []
    assert any(d.startswith("voice:") for d in report.dropped)


async def test_swap_cap_holds_even_when_the_model_overdelivers(tmp_path: Path) -> None:
    reply = _batch_reply(voice=["句一", "句二", "句三", "句四"])
    growth = GrowthSwitches.model_validate({"voice": "on"})
    distiller, _store, persona, _side = _make(tmp_path, growth=growth, replies=[reply])
    await distiller.end_of_stream()
    assert persona.growth_entries("voice") == ["句一", "句二"], "two per stream, plan section 4.6"


async def test_the_batch_says_what_it_was_fed_and_what_it_wrote(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """一场直播里最值钱的一次调用，之前只在失败时说话。

    「她把今晚全忘了」的排查要分得清两件事：批处理没跑，和批处理跑了但一条都
    没写下来。开始那条给的是喂进去多少料、生长层允不允许写；结束那条给的是
    真的落了几条。
    """
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    distiller, _store, _persona, _side = _make(tmp_path, growth=growth)

    with caplog.at_level("INFO"):
        report = await distiller.end_of_stream()

    assert report.ran
    started = _fields(caplog, "distill.batch_started")
    assert len(started) == 1, "每场一次，重跑会被闩住"
    assert started[0]["trigger"] == "stream_end"
    assert started[0]["event_count"] == 1, "_make 只喂了一条弹幕"
    assert started[0]["viewer_count"] == 1
    assert started[0]["growth_voice"] == "on"

    applied = _fields(caplog, "distill.batch_applied")
    assert len(applied) == 1
    assert applied[0]["viewer_fact_count"] == 1
    assert applied[0]["growth_added"] == 2, "共同经历一条、口癖一条"
    assert applied[0]["dropped_count"] == 0
    assert applied[0]["summary_chars"] == len("聊了编译器和猫")

    # 写库那一层自己也数了一遍：观众事实一条、本场摘要一条。
    written = _fields(caplog, "memory.facts_written")
    assert [f["scope"] for f in written] == ["viewer", "stream"]
    assert all(f["fact_count"] == 1 for f in written)


async def test_a_rolling_rewrite_says_why_it_woke_up(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """滚动摘要是每 N 条事件一次，不是每条弹幕一次——日志得能证明这一点。

    指纹没变就直接返回，这时候不该有「开始」那条：一次没发出去的调用记成开始，
    会让这条链路看起来比实际忙得多。
    """
    distiller, store, _persona, _side = _make(tmp_path, replies=["聊了猫和编译器"])

    with caplog.at_level("INFO", logger="bilisama.memory.distill"):
        assert (await distiller.rolling_summary()).ran is True
        started = _fields(caplog, "distill.rolling_started")
        assert len(started) == 1
        assert started[0]["trigger"] == "event_threshold"
        assert started[0]["every_n"] == 3, "阈值就是 every_n_events"
        assert started[0]["event_count"] == 1
        assert _fields(caplog, "distill.rolling_done")[0]["summary_chars"] == len("聊了猫和编译器")

        caplog.clear()
        assert (await distiller.rolling_summary()).reason == "fingerprint_unchanged"
        assert _fields(caplog, "distill.rolling_started") == [], "没真的调模型就不该有开始"

    assert store.facts("stream", str(store.stream_id))[-1].text == "聊了猫和编译器"


async def test_a_budget_trim_says_so_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Plan section 4.7: budgets drop whole entries, and never quietly.

    A full 共同经历 layer taking one more entry pushes the oldest out. Silent,
    that reads as "the layer stopped growing" to anyone watching the file.
    """
    growth = GrowthSwitches.model_validate({"relationship": "on"})
    distiller, _store, persona, _side = _make(tmp_path, growth=growth)
    persona.write_growth("relationship", [f"2026-08-01 旧事{i}" for i in range(30)])

    with caplog.at_level("WARNING", logger="bilisama.memory.distill"):
        report = await distiller.end_of_stream()

    assert report.ran
    assert len(persona.growth_entries("relationship")) == 30, "预算就是 30 条，不会长到 31"
    assert any("growth_trimmed" in record.getMessage() for record in caplog.records)
    assert distiller.status()["growth_added_last_batch"] == 1


async def test_anchor_files_are_byte_identical_through_a_full_cycle(tmp_path: Path) -> None:
    """THE acceptance invariant: distillation with growth on never touches an
    anchor, template or live."""
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    distiller, _store, _persona, _side = _make(
        tmp_path, growth=growth, replies=["摘要", _batch_reply()]
    )
    before = {p.name: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")}

    await distiller.rolling_summary()
    await distiller.end_of_stream()

    assert {p.name: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")} == before
    live = tmp_path / "live"
    assert not (live / "identity.md").exists()
    assert not (live / "personality.md").exists()
    assert (live / "voice.md").exists(), "growth grew, anchors did not move"


# ------------------------------------------------------------ plumbing


async def test_assistant_lines_are_capped(tmp_path: Path) -> None:
    distiller, _store, _persona, _side = _make(tmp_path)
    for i in range(60):
        distiller.note_assistant_line(f"第 {i} 句")
    assert len(distiller._state.assistant_lines) == 40
    assert distiller._state.assistant_lines[-1] == "第 59 句"


def test_parse_json_tolerates_fences_and_rejects_garbage() -> None:
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前置废话 {"a": 1} 后置废话') == {"a": 1}
    assert _parse_json("完全不是") is None
    assert _parse_json("[1, 2]") is None


# ------------------------------------------------------------ health probe


async def test_the_probe_answers_what_a_running_stream_cannot_show_today(
    tmp_path: Path,
) -> None:
    """Whether the end-of-stream batch landed, when the last rolling rewrite
    was, and how many growth entries it added: none of it is visible while the
    stream runs — Ctrl-C printing two lines afterwards is the only report."""
    growth = GrowthSwitches.model_validate({"relationship": "on", "voice": "on"})
    distiller, store, _persona, _side = _make(
        tmp_path, growth=growth, replies=["弹幕在聊猫", _batch_reply()]
    )

    cold = distiller.status()
    assert cold["side_configured"] is True
    assert cold["rolling_ok"] == 0
    assert cold["rolling_last_wall"] == ""
    assert cold["batch_ok"] == 0
    assert cold["batch_ran_this_stream"] is False

    await distiller.rolling_summary()
    await distiller.end_of_stream()

    hot = distiller.status()
    assert hot["rolling_ok"] == 1
    assert str(hot["rolling_last_wall"]).startswith("2026-08-12T20:00")
    assert hot["batch_ok"] == 1
    assert hot["batch_ran_this_stream"] is True
    assert hot["growth_added_last_batch"] == 2, "一条共同经历加一句口癖"
    assert hot["dropped_last_batch"] == 0
    _ = store


async def test_the_probe_counts_failures_too(tmp_path: Path) -> None:
    """A degraded distiller looks exactly like a healthy one from outside —
    which is the reason the failure counters are the point of the probe."""
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
    distiller, _store, _persona, _side = _make(tmp_path, clock=clock)
    distiller._side = FailingSide(fail_times=3)

    await distiller.rolling_summary()
    task = asyncio.create_task(distiller.end_of_stream())
    await clock.advance(2.0)
    await task

    probe = distiller.status()
    assert probe["rolling_failed"] == 1
    assert probe["rolling_ok"] == 0
    assert probe["batch_failed"] == 1, "两次重试算一次失败的批量，不是两次"
    assert probe["batch_ran_this_stream"] is False


def test_the_probe_is_shaped_for_the_health_registry(tmp_path: Path) -> None:
    """The complaint behind this probe was "stage 5 mounts /health and finds
    nothing to mount here" — so assert it mounts."""
    from bilisama.obs.health import HealthRegistry

    clock = FakeClock()
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    distiller = Distiller(
        None, store, PersonaStore(tmp_path, TEMPLATE_ROOT), GrowthSwitches(), clock
    )

    registry = HealthRegistry()
    registry.register("distill", distiller.status)
    snapshot = registry.snapshot()

    assert snapshot["status"] == "ok"
    assert snapshot["components"]["distill"]["side_configured"] is False
