"""Stage 6 B5 peripherals: burst welcome, VIP promotion, gift tiers,
parse-budget sampling, ROOM_STATE, SC withdrawal plumbing, chat profile.

Closes the danmaku-dependent backlog: #27 (parse budget), #11 (chat.toml
loads), plus the store guard that keeps ROOM_STATE from minting an "anon"
regular.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from tests.fakes.bili import danmu_info as _danmu_info
from tests.fakes.bili import entry_event as _entry
from tests.fakes.bili import gift_event
from tests.fakes.bili import sc_data as _sc_data
from tests.fakes.replay import FIXTURE_DIR, replay_driving_clock
from tests.unit.conftest import build_assembly_kit

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE_ROOT = REPO / "config" / "personas" / "tofu"


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


def _assembly(
    tmp_path: Path, *, speak: SpeakSwitches | None = None
) -> tuple[Assembly, MemoryStore, list[Intent], FakeClock]:
    kit = build_assembly_kit(tmp_path, speak=speak, presence=PresenceWelcomer())
    return kit.assembly, kit.store, kit.intents, kit.clock


async def test_five_entries_buy_one_welcome_at_default_switches(tmp_path: Path) -> None:
    """speak.entry governs the entry lane's one voice — the batched hello."""
    assembly, _store, intents, _clock = _assembly(tmp_path)
    for uid in range(1, 6):
        await assembly.on_event(_entry(uid))
    assert [i.source for i in intents] == ["entry"]
    assert intents[0].priority is Priority.DANMAKU, "a hello queues, it never preempts an answer"
    item = intents[0].injection.item_text or ""
    assert "新观众" in item
    assert "5" not in item, "headcounts stay out: the number is stale the moment a sixth walks in"


async def test_entry_off_silences_the_burst_for_observe_mode(tmp_path: Path) -> None:
    """The chat profile's promise — "它只是不回" — must hold for the hello too."""
    assembly, store, intents, _clock = _assembly(tmp_path, speak=SpeakSwitches(entry=False))
    for uid in range(1, 8):
        await assembly.on_event(_entry(uid))
    assert intents == []
    assert store.viewer("uid:3") is not None, "memory still saw everyone"


async def test_wire_identity_promotes_an_arrival_to_vip(tmp_path: Path) -> None:
    """Promotion reads the arrival's own wire identity — guard level or a
    level-5+ current-room medal — never spend history: a first-time captain
    is greeted THIS stream, and no arrival costs a store read."""
    from bilisama.ingest.events import GuardLevel, Medal

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
    assert intents == [], "past spending is not an identity signal any more"

    captain = LiveEvent(
        kind=EventKind.ENTRY,
        room_id=777,
        viewer=Viewer(uid=57, name="舰长", guard_level=GuardLevel.CAPTAIN),
        event_id="iw:57",
    )
    fan = LiveEvent(
        kind=EventKind.ENTRY,
        room_id=777,
        viewer=Viewer(uid=58, name="铁粉", medal=Medal(name="豆腐", level=6, anchor_room_id=777)),
        event_id="iw:58",
    )
    await assembly.on_event(captain)
    await assembly.on_event(fan)
    assert [i.source for i in intents] == ["vip_enter", "vip_enter"]


async def test_presence_replay_one_hello_and_one_named_greeting(tmp_path: Path) -> None:
    """The section 2.7 L2+L4 acceptance against the presence fixture: the
    captain arrives twice and is named once; 121 arrivals buy one hello."""
    assembly, _store, intents, clock = _assembly(tmp_path)
    async for event in replay_driving_clock(clock, FIXTURE_DIR / "presence.jsonl", room_id=777):
        await assembly.on_event(event)
    vips = [i for i in intents if i.source == "vip_enter"]
    hellos = [i for i in intents if i.source == "entry"]
    assert len(vips) == 1, "the second arrival stays silent"
    assert len(hellos) == 1, "a hundred entries is one hello, not a greeting machine"


# ------------------------------------------------------------------ gift tiers


def _gift_event(coins: int, *, coin_type: str = "gold") -> LiveEvent:
    return gift_event(coin=coins, coin_type=coin_type)


def test_gift_tiers_follow_the_battery_thresholds() -> None:
    """Tiers compare total batteries — the number the viewer sees — and no
    tier is protected any more: streamer speech always lands, paid safety is
    the requeue."""
    high = intent_for(_gift_event(150_000), now=0.0)  # 1500 batteries
    medium = intent_for(_gift_event(15_000), now=0.0)  # 150 batteries
    light = intent_for(_gift_event(500), now=0.0)  # 5 batteries
    free = intent_for(_gift_event(990, coin_type="silver"), now=0.0)
    assert high is not None and medium is not None and light is not None and free is not None
    assert high.priority is Priority.BIG_GIFT and not high.injection.reply.protected
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
        item = source._queue.get_nowait()
        if item is not None:
            kinds.append(item)
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
    assert not speak.entry, "observe mode must silence the batched hello too"
    assert speak.proactive, "chat mode still starts topics — that is its point"


# ------------------------------------------------------------------ start() paths


class _FakeClient:
    """Stands in for bili_web.BLiveClient: no network, scripted init result."""

    init_result = True
    room_id = 7734200
    # What upstream's _init_uid resolves a `uid=None` request to. 0 is the
    # -101 branch: an expired SESSDATA, uid forced to 0, init_room still True
    # (_vendor/blivedm/clients/web.py:257-262).
    resolved_uid = 0

    def __init__(self, room_id: int, uid: int | None = None, session: object = None) -> None:
        self._need_init_room = True
        self._uid = uid

    @property
    def uid(self) -> int | None:
        return self._uid

    def set_handler(self, handler: object) -> None:
        self.handler = handler

    async def init_room(self) -> bool:
        if self._uid is None:
            self._uid = type(self).resolved_uid
        return type(self).init_result

    def start(self) -> None:
        return None

    async def stop_and_close(self) -> None:
        return None


async def _drain_nothing(event: LiveEvent) -> None:
    return None


async def test_failed_init_raises_instead_of_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream returns False and silently falls back to the short room id;
    we refuse — a wrong room id poisons medal matching for the whole run."""
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "init_result", False)
    source = BilibiliEventSource(6, SystemClock())
    with pytest.raises(RuntimeError, match="初始化失败"):
        await source.start(_drain_nothing)


async def test_client_death_escapes_start_for_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "init_result", True)
    source = BilibiliEventSource(6, SystemClock())
    task = asyncio.create_task(source.start(_drain_nothing))
    for _ in range(200):
        if source.status()["connected"]:
            break
        await asyncio.sleep(0.01)
    source.on_client_stopped(ConnectionError("ws torn down"))
    with pytest.raises(RuntimeError, match="弹幕连接挂了"):
        await asyncio.wait_for(task, timeout=2.0)


async def test_stop_exits_cleanly_without_a_restart_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "init_result", True)
    source = BilibiliEventSource(6, SystemClock())
    task = asyncio.create_task(source.start(_drain_nothing))
    for _ in range(200):
        if source.status()["connected"]:
            break
        await asyncio.sleep(0.01)
    await source.stop()
    await asyncio.wait_for(task, timeout=2.0)  # no exception: a clean exit


async def test_three_mapping_failures_trip_the_breaker_and_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker's documented job: caught mapping failures stop the lane,
    and the stop surfaces as a raise the supervisor can act on."""
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "init_result", True)
    source = BilibiliEventSource(6, SystemClock())
    task = asyncio.create_task(source.start(_drain_nothing))
    for _ in range(200):
        if source.status()["connected"]:
            break
        await asyncio.sleep(0.01)
    for i in range(3):
        source.note_error(ValueError(f"info 布局变了 {i}"))
    with pytest.raises(RuntimeError, match="熔断"):
        await asyncio.wait_for(task, timeout=2.0)
    assert source.status()["breaker_open"] is True


def test_mirror_danmaku_is_dropped_and_accounted() -> None:
    source, forwarder, _clock = _source()

    class _Mirror:
        is_mirror = True

    forwarder._on_danmaku(None, _Mirror())
    assert source.status()["counts"] == {}, "never mapped, never offered"
    shed = source.status()["shed"]
    assert isinstance(shed, dict) and shed["mirror"] == 1


def test_a_linked_room_flood_does_not_eat_the_home_rooms_parse_budget() -> None:
    """A PK opponent's danmaku shared the home lane's 80/s, so the other
    room's flood pushed our own viewers into the sampled remainder — for
    events we discard anyway, since those people cannot hear a reply here."""
    source, forwarder, _clock = _source()
    for i in range(1000):
        info = _danmu_info(uid=50_000 + i, msg=f"对面{i}", medal=False, privilege=0, admin=0)
        forwarder.handle(None, {"cmd": "DANMU_MSG_MIRROR", "info": info})
    for i in range(80):
        info = _danmu_info(uid=10_000 + i, msg=f"本房{i}", medal=False, privilege=0, admin=0)
        forwarder.handle(None, {"cmd": "DANMU_MSG", "info": info})
    status = source.status()
    counts = status["counts"]
    assert isinstance(counts, dict)
    assert counts.get("danmaku") == 80, "本房那 80 条一条都不该被对面挤掉"
    shed = status["shed"]
    assert isinstance(shed, dict)
    assert shed["mirror"] == 1000, "镜像弹幕全记在自己名下"
    assert "danmaku" not in shed, "本房这一秒没有超预算，不该有丢弃"
    assert status["map_errors"] == 0


def test_the_home_lane_budget_still_bites_on_its_own() -> None:
    """Boundary: giving mirrors their own lane must not quietly lift the home
    lane's cap — 81 in one second is still 80 parsed and one shed."""
    source, forwarder, _clock = _source()
    for i in range(81):
        info = _danmu_info(uid=20_000 + i, msg=f"本房{i}", medal=False, privilege=0, admin=0)
        forwarder.handle(None, {"cmd": "DANMU_MSG", "info": info})
    status = source.status()
    counts = status["counts"]
    shed = status["shed"]
    assert isinstance(counts, dict) and counts["danmaku"] == 80
    assert isinstance(shed, dict) and shed["danmaku"] == 1


def test_sc_delete_purges_the_paid_pocket_before_revoking() -> None:
    """The delete can arrive in the same bundle as the SC itself, while the
    event still sits in the paid deque — pulling it there is what makes the
    withdrawal real; the scheduler never sees a key to revoke."""
    from bilisama.ingest.bilibili._vendor.blivedm.models import web as web_models
    from bilisama.ingest.bilibili.source import event_from_super_chat
    from tests.fakes.bili import sc_data as sc_payload

    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    revoked: list[str] = []
    source = BilibiliEventSource(777, clock, on_sc_delete=revoked.append)
    sc = event_from_super_chat(
        web_models.SuperChatMessage.from_command(sc_payload()),
        room_id=777,
        recv_at=1.0,
        generation=1,
    )
    source.offer(sc)
    assert len(source._paid) == 1
    source.on_sc_delete([888001])
    assert len(source._paid) == 0, "withdrawn before it ever reached the scheduler"
    assert revoked == ["super_chat:sc:888001"]


# ------------------------------------------------------------------ review-round fixes


def _sc_event(sc_id: int = 1) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.SUPER_CHAT,
        room_id=777,
        viewer=Viewer(uid=77, name="金主"),
        text="加油",
        value_cny=30.0,
        event_id=f"sc:{sc_id}",
    )


async def test_replayed_super_chat_is_deduped_at_the_assembly(tmp_path: Path) -> None:
    """An inner-reconnect replay arrives seconds later — long after the
    scheduler settled and freed the key. The 30s assembly ring is the cover
    the paid lane never had."""
    assembly, _store, intents, clock = _assembly(tmp_path)
    await assembly.on_event(_sc_event())
    await clock.advance(3.0)
    await assembly.on_event(_sc_event())  # byte-identical replay
    assert len([i for i in intents if i.source == "super_chat"]) == 1
    assert assembly.events_deduped == 1


async def test_a_remembered_tier_no_longer_promotes_a_bare_entry(tmp_path: Path) -> None:
    """Promotion reads the ARRIVAL's wire identity only. A tier that memory
    recorded from an earlier danmaku stays in memory (the upsert guard still
    protects it from the bare ENTRY) but buys no greeting: the wire carries
    guard level on entries now, so the entry that deserves a name brings its
    own proof."""
    from bilisama.ingest.events import GuardLevel

    assembly, store, intents, _clock = _assembly(tmp_path)
    store.on_event(
        LiveEvent(
            kind=EventKind.DANMAKU,
            room_id=777,
            viewer=Viewer(uid=55, name="老舰长", guard_level=GuardLevel.CAPTAIN),
            text="来了",
            event_id="dm:seed",
        )
    )
    await assembly.on_event(_entry(55))
    assert intents == []
    record = store.viewer("uid:55")
    assert record is not None and record.guard_level is GuardLevel.CAPTAIN


async def test_console_events_skip_the_crowd_funnel(tmp_path: Path) -> None:
    """room_id 0 marks keyboard input (the dev console): a typed 你好 must
    answer immediately instead of losing to the spam bar."""
    from bilisama.ingest.bilibili.selector import DanmakuSelector

    clock = FakeClock(wall=datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    persona = PersonaStore(tmp_path / "live", TEMPLATE_ROOT)
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

    from bilisama.config.derive import derive
    from bilisama.config.enums import Chattiness

    assembly = Assembly(
        store=store,
        distiller=Distiller(None, store, persona, GrowthSwitches(), clock),
        proactive=proactive,
        persona=persona,
        growth=GrowthSwitches(),
        speak_enabled=lambda source: True,
        submit=intents.append,
        push_context=push,
        clock=clock,
        selector=DanmakuSelector(clock, thresholds=lambda: derive(Chattiness.MEDIUM)),
    )
    console = LiveEvent(
        kind=EventKind.DANMAKU,
        viewer=Viewer(uid=1, name="测试观众"),
        text="你好",
        event_id="console:1",
    )
    await assembly.on_event(console)
    assert [i.source for i in intents] == ["danmaku"], "no window, no score bar"


async def test_speak_toggle_mid_window_silences_the_delivery(tmp_path: Path) -> None:
    switches = SpeakSwitches()
    assembly, _store, intents, _clock = _assembly(tmp_path, speak=switches)
    winner = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=777,
        viewer=Viewer(uid=1, name="观众"),
        text="主播怎么看",
        event_id="dm:late",
    )
    switches.danmaku = False  # flipped while the window was open
    await assembly.deliver_selected(winner)
    assert intents == []


def test_danmaku_ttl_counts_from_arrival_with_a_dispatch_floor() -> None:
    stale = LiveEvent(
        kind=EventKind.DANMAKU,
        room_id=777,
        viewer=Viewer(uid=1, name="观众"),
        text="老问题",
        event_id="dm:old",
        recv_at=100.0,
    )
    intent = intent_for(stale, now=128.0)  # a LOW-chattiness 30s window later
    assert intent is not None and intent.expires_at is not None
    assert intent.expires_at == 133.0, "floor: 5s of runway, not 20 more seconds of staleness"
    fresh = intent_for(stale, now=101.0)
    assert fresh is not None and fresh.expires_at == 120.0, "arrival + 20s while fresh"


def test_tierless_upsert_never_erases_a_recorded_guard_level() -> None:
    from bilisama.ingest.events import GuardLevel

    clock = FakeClock(wall=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    store = MemoryStore(":memory:", clock)
    store.begin_stream()
    store.on_event(
        LiveEvent(
            kind=EventKind.DANMAKU,
            room_id=777,
            viewer=Viewer(uid=55, name="老舰长", guard_level=GuardLevel.CAPTAIN),
            text="来了",
            event_id="dm:1",
        )
    )
    store.on_event(_entry(55))  # wire entries carry no guard field
    record = store.viewer("uid:55")
    assert record is not None and record.guard_level is GuardLevel.CAPTAIN


# ------------------------------------------------------------------ credential state (#12)


async def _status_after_connect(
    monkeypatch: pytest.MonkeyPatch, *, sessdata: str, resolved_uid: int
) -> dict[str, object]:
    """Run start() as far as "connected", read health, then shut down cleanly."""
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _FakeClient)
    monkeypatch.setattr(_FakeClient, "init_result", True)
    monkeypatch.setattr(_FakeClient, "resolved_uid", resolved_uid)
    source = BilibiliEventSource(6, SystemClock(), sessdata=sessdata)
    task = asyncio.create_task(source.start(_drain_nothing))
    for _ in range(200):
        if source.status()["connected"]:
            break
        await asyncio.sleep(0.01)
    status = dict(source.status())
    status["credential_stale_property"] = source.credential_stale
    await source.stop()
    await asyncio.wait_for(task, timeout=2.0)
    return status


async def test_working_credential_reports_a_logged_in_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_after_connect(monkeypatch, sessdata="有效凭据", resolved_uid=90001)
    assert status["logged_in"] is True
    assert status["credential_stale"] is False


async def test_expired_sessdata_is_reported_as_a_stale_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The silent failure this exists for: upstream's _init_uid takes the -101
    branch, sets uid 0 and returns True, so init_room succeeds and the whole
    stream is anonymous while every downstream reader still sees "logged in"
    (_vendor/blivedm/clients/web.py:257-262)."""
    status = await _status_after_connect(monkeypatch, sessdata="过期凭据", resolved_uid=0)
    assert status["logged_in"] is False
    assert status["credential_stale"] is True, "配了凭据却拿回 uid 0，这一场其实是匿名"
    assert status["credential_stale_property"] is True, "G7 打横幅要读的那个属性"


async def test_deliberate_anonymous_is_not_a_stale_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: no credential configured at all. Anonymous is then the chosen
    mode, not a broken one, and must not raise the alarm."""
    status = await _status_after_connect(monkeypatch, sessdata="", resolved_uid=0)
    assert status["logged_in"] is False
    assert status["credential_stale"] is False


def test_credential_state_before_connecting_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: a source that never connected has read no uid, so it cannot
    call a credential stale — the alarm needs the platform's answer first."""
    from bilisama.clock import SystemClock

    source = BilibiliEventSource(6, SystemClock(), sessdata="还没连过")
    assert source.credential_stale is False
    assert source.status()["logged_in"] is False


# ------------------------------------------------------------------ re-vendor tripwire (#13)


class _NoInitFlagClient(_FakeClient):
    """A re-vendored client that dropped the private flag we clear."""

    def __init__(self, room_id: int, uid: int | None = None, session: object = None) -> None:
        super().__init__(room_id, uid, session)
        del self._need_init_room


async def test_clearing_the_upstream_init_flag_is_recorded_when_it_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = await _status_after_connect(monkeypatch, sessdata="", resolved_uid=0)
    assert status["vendor_drift"] == "", "属性还在，没有漂移可报"


async def test_a_renamed_init_flag_shows_up_instead_of_minting_a_dead_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: upstream renames or drops `_need_init_room`. A plain
    assignment would create a fresh attribute nobody reads — every start pays a
    second room-init round trip and nothing ever says so."""
    from bilisama.clock import SystemClock

    monkeypatch.setattr("bilisama.ingest.bilibili.source.bili_web.BLiveClient", _NoInitFlagClient)
    monkeypatch.setattr(_FakeClient, "init_result", True)
    source = BilibiliEventSource(6, SystemClock())
    task = asyncio.create_task(source.start(_drain_nothing))
    for _ in range(200):
        if source.status()["connected"]:
            break
        await asyncio.sleep(0.01)
    assert source.status()["vendor_drift"] == "_need_init_room"
    await source.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_upstream_still_carries_both_internals_start_reaches_into() -> None:
    """The re-vendor tripwire VENDOR.md promises, for the two upstream
    internals start() touches.

    Neither is visible to any other test here, because the fake client above
    defines them itself. `_need_init_room` is the flag that keeps
    client.start() from running init_room a second time
    (_vendor/blivedm/clients/ws_base.py:301-310); `uid` is the only thing that
    separates an expired SESSDATA from a working one
    (_vendor/blivedm/clients/web.py:204-210). Construction only — no network.
    """
    import aiohttp

    from bilisama.ingest.bilibili._vendor.blivedm.clients import web as bili_web

    async with aiohttp.ClientSession() as session:
        client = bili_web.BLiveClient(1, uid=0, session=session)
        assert hasattr(client, "_need_init_room"), "上游改名了：init_room 会跑两遍"
        assert client.uid == 0, "上游的 uid 读法变了：过期凭据会被当成登录态"
