"""Event model and replay source.

The rule that matters most: a masked viewer is still a viewer. Dropping those
events looks like nothing is wrong until half the room goes silent.
"""

from __future__ import annotations

import pytest

from bilisama.ingest.events import (
    EventKind,
    Gift,
    GuardLevel,
    LiveEvent,
    Viewer,
    cny_from_gold,
    is_vip_entry,
)
from bilisama.ingest.sources import QueueSource, collect
from tests.fakes.replay import ReplaySource, fixture, read_fixture

# ------------------------------------------------------------ identity


def test_masked_uid_still_has_a_usable_identity() -> None:
    """Bilibili masks uid to 0, and uid_hash becomes the only stable handle.

    N.E.K.O's guard at neko_live/modules/live_events/module.py:238 returns early on
    exactly this, silencing the whole danmaku stream once masking is in play.
    """
    masked = Viewer(uid=0, uid_hash="ab12", name="***")
    assert masked.is_anonymous
    assert masked.identity == "hash:ab12"
    assert masked.identity, "an empty identity key would break dedup and memory"


def test_identity_prefers_uid_when_present() -> None:
    assert Viewer(uid=42, uid_hash="ab12").identity == "uid:42"


def test_identity_falls_back_to_anon_only_when_nothing_available() -> None:
    assert Viewer().identity == "anon"


def test_display_name_never_empty() -> None:
    assert Viewer(uid=1).display_name == "一位观众"
    assert Viewer(uid=1, name="阿强").display_name == "阿强"


# ------------------------------------------------------------ money


@pytest.mark.parametrize(("coin", "cny"), [(1000, 1.0), (20000, 20.0), (198000, 198.0), (0, 0.0)])
def test_gold_to_cny(coin: int, cny: float) -> None:
    assert cny_from_gold(coin) == cny


def test_silver_gift_is_not_paid() -> None:
    assert not Gift(coin_type="silver", total_coin=500).is_paid
    assert Gift(coin_type="gold", total_coin=1000).is_paid


# ------------------------------------------------------------ dedup and redaction


def test_dedup_key_uses_event_id_when_available() -> None:
    e = LiveEvent(kind=EventKind.DANMAKU, event_id="x1", text="666")
    assert e.dedup_key == "danmaku:x1"


def test_dedup_key_falls_back_without_event_id() -> None:
    """Dedup has to work without a platform id, or a reconnect replays reactions."""
    v = Viewer(uid=7)
    a = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=1500)
    b = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=1900)
    assert a.dedup_key == b.dedup_key  # same person, same words, same second
    c = LiveEvent(kind=EventKind.DANMAKU, viewer=v, text="666", ts_ms=2500)
    assert a.dedup_key != c.dedup_key


def test_redacted_drops_raw() -> None:
    """raw is the unsanitised platform payload and must never reach a prompt."""
    e = LiveEvent(kind=EventKind.DANMAKU, text="嗨", raw={"cmd": "DANMU_MSG", "info": [1, 2]})
    assert e.raw is not None
    assert e.redacted().raw is None
    assert e.redacted().text == "嗨"  # other fields survive


def test_redacted_is_cheap_when_already_clean() -> None:
    e = LiveEvent(kind=EventKind.DANMAKU, text="嗨")
    assert e.redacted() is e


# ------------------------------------------------------------ VIP arrivals


def test_guard_makes_a_vip_entry() -> None:
    assert is_vip_entry(Viewer(uid=1, guard_level=GuardLevel.CAPTAIN))
    assert not is_vip_entry(Viewer(uid=1))


def test_past_spending_makes_a_vip_entry() -> None:
    """Past spenders deserve a greeting too, not just current members."""
    assert is_vip_entry(Viewer(uid=1), lifetime_gift_cny=30.0)


def test_guard_level_from_wire() -> None:
    assert GuardLevel.from_wire(3) is GuardLevel.CAPTAIN
    assert GuardLevel.from_wire(1) is GuardLevel.GOVERNOR
    assert GuardLevel.from_wire(0) is GuardLevel.NONE
    assert GuardLevel.from_wire(99) is GuardLevel.NONE  # an unknown tier must not raise


# ------------------------------------------------------------ replay


def test_every_fixture_parses() -> None:
    """Every fixture parses. A broken fixture turns a whole batch of tests green
    for the wrong reason."""
    names = [
        "quiet_stream.jsonl",
        "superchat_during_speech.jsonl",
        "gift_combo.jsonl",
        "event_flood.jsonl",
        "presence.jsonl",
        "anonymous_masked.jsonl",
        "returning_viewer.jsonl",
        "injection_attempt.jsonl",
    ]
    for name in names:
        events = list(read_fixture(fixture(name)))
        assert events, f"{name} is empty"
        assert all(isinstance(e, LiveEvent) for _, e in events)


def test_anonymous_fixture_yields_usable_identities() -> None:
    """Every event in the masked fixture still has a usable, distinguishing identity."""
    events = [e for _, e in read_fixture(fixture("anonymous_masked.jsonl"))]
    assert all(e.is_anonymous for e in events)
    assert all(e.viewer.identity != "anon" for e in events)
    assert (
        len({e.viewer.identity for e in events}) > 1
    ), "masking must not collapse everyone into one person"


def test_gift_value_derived_from_gold() -> None:
    events = [e for _, e in read_fixture(fixture("gift_combo.jsonl"))]
    assert all(e.value_cny == 1.0 for e in events)
    assert events[-1].gift is not None and events[-1].gift.combo_end is True


def test_flood_fixture_has_paid_events_mixed_in() -> None:
    """The flood carries paid events, so we can check they do not lose the window race."""
    events = [e for _, e in read_fixture(fixture("event_flood.jsonl"))]
    assert len(events) > 150
    assert any(e.is_paid for e in events)


async def test_replay_source_emits_in_order() -> None:
    source = ReplaySource(path=fixture("gift_combo.jsonl"), speed=0)
    events = await collect(source, limit=4)
    assert [e.event_id for e in events] == ["g1", "g2", "g3", "g4"]


async def test_replay_source_keeps_relative_order_under_real_clock() -> None:
    """speed is a multiplier, not "ignore timing" — window logic needs the gaps kept."""
    source = ReplaySource(path=fixture("superchat_during_speech.jsonl"), speed=2000.0)
    events = await collect(source, limit=3)
    assert [e.kind for e in events] == [
        EventKind.DANMAKU,
        EventKind.SUPER_CHAT,
        EventKind.DANMAKU,
    ]


async def test_queue_source_round_trip() -> None:
    source = QueueSource()
    await source.push(LiveEvent(kind=EventKind.DANMAKU, text="嗨", event_id="q1"))
    events = await collect(source, limit=1)
    assert events[0].event_id == "q1"


def test_event_kind_covers_the_speak_switches() -> None:
    """The event taxonomy and the speak switches must line up exactly."""
    from bilisama.config import SpeakSwitches

    switches = set(SpeakSwitches.model_fields)
    # proactive and background_result are internal sources, not live events.
    switches -= {"proactive", "background_result"}
    kinds = {k.value for k in EventKind} - {"room_state"}
    assert switches == kinds, (
        f"switches and event kinds disagree: switch-only {switches - kinds}, "
        f"kind-only {kinds - switches}"
    )
