"""Stage 6 B5 peripherals: burst welcome, VIP promotion, gift tiers,
parse-budget sampling, ROOM_STATE, SC withdrawal plumbing, chat profile.

Closes the danmaku-dependent backlog: #27 (parse budget), #11 (chat.toml
loads), plus the store guard that keeps ROOM_STATE from minting an "anon"
regular.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bilisama.app import Assembly
from bilisama.clock import FakeClock
from bilisama.config.loader import load
from bilisama.config.schema import GrowthSwitches, SpeakSwitches
from bilisama.director.floor import SpeakingFloor
from bilisama.director.intent import Intent, Priority
from bilisama.director.intents import intent_for
from bilisama.ingest.bilibili.selector import PresenceWelcomer
from bilisama.ingest.bilibili.source import BilibiliEventSource, _Forwarder
from bilisama.ingest.events import EventKind, Gift, LiveEvent, Viewer
from bilisama.memory.distill import Distiller
from bilisama.memory.store import MemoryStore
from bilisama.persona.loader import PersonaStore
from bilisama.proactive import ProactiveTopicLoop
from tests.fakes.replay import FIXTURE_DIR, read_fixture
from tests.unit.test_bili_translate import _danmu_info, _sc_data

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE_ROOT = REPO / "config" / "personas" / "mia"


# ------------------------------------------------------------------ presence


def test_burst_fires_at_five_uniques_and_repeats_do_not_count() -> None:
    welcomer = PresenceWelcomer()
    for i in range(4):
        assert welcomer.note(f"uid:{i}", now=10.0 + i) is None
    assert welcomer.note("uid:0", now=14.5) is None, "a returning viewer is not a fifth person"
    assert welcomer.note("uid:4", now=15.0) == 5


def test_burst_cooldown_blocks_then_the_next_wave_fires() -> None:
    welcomer = PresenceWelcomer(uniques=2, window_s=45.0, cooldown_s=90.0)
    welcomer.note("uid:1", now=0.0)
    assert welcomer.note("uid:2", now=1.0) == 2
    assert welcomer.note("uid:3", now=3.0) is None, "inside the cooldown"
    assert welcomer.note("uid:4", now=89.0) is None, "still inside it"
    assert welcomer.note("uid:5", now=92.0) == 2, "uid:4 and uid:5 are still fresh; uid:3 aged out"


def test_stale_arrivals_age_out_of_the_window() -> None:
    welcomer = PresenceWelcomer(uniques=3, window_s=45.0)
    welcomer.note("uid:1", now=0.0)
    welcomer.note("uid:2", now=1.0)
    assert welcomer.note("uid:3", now=50.0) is None, "the first two aged out"


# ------------------------------------------------------------------ assembly lanes


def _assembly(tmp_path: Path) -> tuple[Assembly, MemoryStore, list[Intent], FakeClock]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
    growth = GrowthSwitches()
    speak = SpeakSwitches()  # entry stays False — the burst must not care
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
        presence=PresenceWelcomer(),
    )
    return assembly, store, intents, clock


def _entry(uid: int) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.ENTRY,
        room_id=777,
        viewer=Viewer(uid=uid, name=f"观众{uid}"),
        event_id=f"iw:{uid}",
    )


async def test_five_entries_buy_one_welcome_with_speak_entry_off(tmp_path: Path) -> None:
    """Plan section 2.7: speak.entry defaults off BECAUSE this is the fallback."""
    assembly, _store, intents, _clock = _assembly(tmp_path)
    for uid in range(1, 6):
        await assembly.on_event(_entry(uid))
    assert [i.source for i in intents] == ["entry"]
    assert intents[0].priority is Priority.BACKGROUND_RESULT
    assert "5 位" in (intents[0].injection.item_text or "")


async def test_known_spender_walking_in_is_promoted_to_vip(tmp_path: Path) -> None:
    assembly, store, intents, _clock = _assembly(tmp_path)
    store.on_event(
        LiveEvent(
            kind=EventKind.GIFT,
            room_id=777,
            viewer=Viewer(uid=55, name="老板"),
            gift=Gift(gift_id=1, name="礼物", coin_type="gold", total_coin=20000),
            value_cny=20.0,
            event_id="gift:seed",
        )
    )
    await assembly.on_event(_entry(55))
    assert [i.source for i in intents] == ["vip_enter"], "memory promoted the arrival"
    await assembly.on_event(_entry(56))
    assert len(intents) == 1, "a stranger's entry stays feed-only"


async def test_presence_replay_one_hello_and_one_named_greeting(tmp_path: Path) -> None:
    """The section 2.7 L2+L4 acceptance against the presence fixture: the
    captain arrives twice and is named once; 121 arrivals buy one hello."""
    assembly, _store, intents, clock = _assembly(tmp_path)
    cursor = 0.0
    for at_s, event in read_fixture(FIXTURE_DIR / "presence.jsonl", room_id=777):
        if at_s > cursor:
            await clock.advance(at_s - cursor)
            cursor = at_s
        await assembly.on_event(event)
    vips = [i for i in intents if i.source == "vip_enter"]
    hellos = [i for i in intents if i.source == "entry"]
    assert len(vips) == 1, "the second arrival stays silent"
    assert len(hellos) == 1, "a hundred entries is one hello, not a greeting machine"


# ------------------------------------------------------------------ gift tiers


def _gift_event(coins: int, *, coin_type: str = "gold") -> LiveEvent:
    return LiveEvent(
        kind=EventKind.GIFT,
        room_id=777,
        viewer=Viewer(uid=9, name="老板"),
        gift=Gift(gift_id=1, name="礼物", coin_type=coin_type, total_coin=coins),
        value_cny=coins / 1000.0 if coin_type == "gold" else 0.0,
        event_id=f"gift:{coins}:{coin_type}",
    )


def test_gift_tiers_follow_the_gold_thresholds() -> None:
    high = intent_for(_gift_event(20000), now=0.0)
    medium = intent_for(_gift_event(5000), now=0.0)
    light = intent_for(_gift_event(500), now=0.0)
    free = intent_for(_gift_event(990, coin_type="silver"), now=0.0)
    assert high is not None and medium is not None and light is not None and free is not None
    assert high.priority is Priority.BIG_GIFT and high.injection.reply.protected
    assert high.requeue_on_interrupt
    assert medium.priority is Priority.VIP_ENTER, "medium rides the VIP rung"
    assert medium.requeue_on_interrupt and not medium.injection.reply.protected
    assert light.priority is Priority.DANMAKU and light.expires_at is not None
    assert free.priority is Priority.DANMAKU and not free.requeue_on_interrupt


# ------------------------------------------------------------------ parse budget (#27)


def _source() -> tuple[BilibiliEventSource, _Forwarder, FakeClock]:
    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    source = BilibiliEventSource(777, clock, queue_size=4096)
    return source, _Forwarder(source), clock


def test_flood_second_sheds_danmaku_over_budget_but_never_paid() -> None:
    source, forwarder, clock = _source()
    for i in range(1000):
        info = _danmu_info(uid=10_000 + i, msg=f"弹幕{i}", medal=False, privilege=0, admin=0)
        forwarder.handle(None, {"cmd": "DANMU_MSG", "info": info})
        if i in (300, 600, 900):
            forwarder.handle(None, {"cmd": "SUPER_CHAT_MESSAGE", "data": _sc_data()})
    status = source.status()
    assert status["counts"]["danmaku"] == 80, "the per-second budget"
    shed = status["shed"]
    assert isinstance(shed, dict) and shed["danmaku"] == 920, "every drop is on the books"
    assert status["counts"]["super_chat"] == 3, "paid commands never shed"
    assert status["map_errors"] == 0

    clock._now += 1.0  # the next second refills the budget
    forwarder.handle(None, {"cmd": "DANMU_MSG", "info": _danmu_info(uid=1, msg="新的一秒")})
    assert source.status()["counts"]["danmaku"] == 81


def test_room_state_is_lifted_out_of_the_ignore_list() -> None:
    source, forwarder, _clock = _source()
    forwarder.handle(None, {"cmd": "LIVE", "data": {}})
    forwarder.handle(None, {"cmd": "PREPARING", "data": {}})
    kinds = []
    while not source._queue.empty():
        kinds.append(source._queue.get_nowait())
    assert [e.text for e in kinds] == ["live", "preparing"]
    assert all(e.kind is EventKind.ROOM_STATE for e in kinds)
    assert source.status()["room_state"] == "preparing"


def test_sc_delete_maps_ids_onto_revoke_keys() -> None:
    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    revoked: list[str] = []
    source = BilibiliEventSource(777, clock, on_sc_delete=revoked.append)
    source.on_sc_delete([888001, 888002])
    assert revoked == ["super_chat:sc:888001", "super_chat:sc:888002"]


# ------------------------------------------------------------------ store guard


def test_room_state_events_never_mint_an_anon_regular() -> None:
    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.on_event(LiveEvent(kind=EventKind.ROOM_STATE, room_id=777, text="live"))
    assert store.viewer("anon") is None, "an anon row would float to the top of regulars"


# ------------------------------------------------------------------ chat profile (#11)


def test_shipped_chat_profile_loads_and_means_no_danmaku() -> None:
    settings = load(REPO / "config" / "bilisama.toml", overrides={"active_profile": "chat"})
    assert settings.active_profile == "chat"
    speak = settings.interaction.speak
    assert not speak.danmaku and not speak.gift and not speak.super_chat
    assert speak.proactive, "chat mode still starts topics — that is its point"
