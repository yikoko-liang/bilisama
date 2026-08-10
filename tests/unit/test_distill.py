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

import pytest

from bilisama.clock import FakeClock
from bilisama.config.schema import GrowthSwitches
from bilisama.ingest.events import EventKind, LiveEvent, Viewer
from bilisama.memory.distill import Distiller, _parse_json
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore

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
) -> tuple[Distiller, MemoryStore, PersonaStore, FakeSide]:
    clock = FakeClock(wall=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))
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


async def _noop() -> None:
    await asyncio.sleep(0)
