"""Browser-level tests for the pet page, against the real served frontend.

This is the automated half of what used to be a purely manual checklist
(CONTRIBUTING「界面改动的人工验收」): a real chromium drives the real page
served by the real UiServer, and the test coroutine plays director — it IS
the hub, broadcasting frames between assertions on the same event loop.

The tier follows the s2s integration precedent: marked ui_browser, deselected
by default, run by the gate when the browser is installed and skipped OUT
LOUD when it is not (`python -m playwright install chromium`).

What stays manual: audio-coupled behaviour (the poke's spoken reply), the
Electron shell's window physics (transparency, drag, the panel window), and
taste. Everything DOM-observable lives here instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from bilisama.clock import SystemClock
from bilisama.config.schema import Settings
from bilisama.obs.health import HealthRegistry
from bilisama.ui.audio import AudioBroker
from bilisama.ui.config_edit import apply_panel_edits
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import UiServer, bind_ui_socket, create_ui_app
from bilisama.ui.skins import list_skin_packs, packaged_skins_root

try:
    from playwright.async_api import Browser, Page, async_playwright
except ImportError:  # pragma: no cover - the gate reports this out loud
    pytest.skip("playwright 未安装：uv pip install playwright", allow_module_level=True)

pytestmark = pytest.mark.ui_browser

_TOKEN = "uitest0token0000deadbeef"


@dataclass
class Harness:
    hub: UiHub
    url: str
    origin: str
    port: int
    calls: list[tuple[ClientEvent, dict[str, Any]]]
    avatar: dict[str, str]
    server: UiServer
    settings: Settings = field(default_factory=Settings)
    room: dict[str, Any] = field(
        default_factory=lambda: {"connected": False, "active_room_id": 0, "error": ""}
    )
    user_skins_root: Any = None  # a tmp dir with user packs, when a test needs one
    broker: AudioBroker | None = None
    uplink: list[bytes] = field(default_factory=list)
    _sock_port: int = 0

    hello_override: dict[str, Any] = field(default_factory=dict)
    gate_health: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "when_addressed",
            "holding": 0,
            "passed": 30,
            "skipped": 12,
            "recent_turns": 20,
            "recent_skipped": 5,
            "timeouts": 0,
            "late_markers": 0,
            "longest_hold_ms": 0,
        }
    )

    def hello(self) -> dict[str, Any]:
        return {
            **{
                "protocol": 1,
                "persona": {"id": "tofu", "name": "豆腐"},
                "provider": "s2s",
                "room_connected": False,
                "avatar": self.avatar,
                "panel": {"panicked": False, "speak": {"danmaku": True, "gift": False}},
            },
            **self.hello_override,
        }


@dataclass
class _Recorder:
    calls: list[tuple[ClientEvent, dict[str, Any]]] = field(default_factory=list)

    def handler(self, event: ClientEvent) -> Any:
        async def handle(data: dict[str, Any]) -> None:
            self.calls.append((event, data))

        return handle


def _build_server(hub: UiHub, harness_ref: list[Harness], port: int = 0) -> UiServer:
    recorder = _Recorder(harness_ref[0].calls if harness_ref else [])
    sock = bind_ui_socket(port)
    real_port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{real_port}"
    registry = HealthRegistry()
    registry.register("assembly", lambda: {"events_seen": 1})
    # The selector's real shape (ingest/bilibili/selector.py:181-189), field for
    # field: seven of them, with the three that matter most to a streamer
    # mid-stream sitting at the end. A card that shows only the first few is a
    # card that hides the breaker exactly when it trips.
    registry.register(
        "selector",
        lambda: {
            "offered": 12,
            "delivered": 3,
            "skips": {"cooldown": 2},
            "window_open": False,
            "breaker_open": True,
            "breaker_reason": "上游连续报错",
            "combos_suppressed": 4,
        },
    )
    # The voice gate, so the strip on the system and chat pages has something
    # to render. A test that wants another state edits harness.gate_health.
    registry.register("voice_gate", lambda: (harness_ref[0].gate_health if harness_ref else {}))
    settings = harness_ref[0].settings if harness_ref else Settings()
    handlers = {event: recorder.handler(event) for event in ClientEvent}
    if harness_ref:
        # dev-talk's on_panel_set, minus its terminal print and reload hooks:
        # the write itself goes through the SHARED apply_panel_edits, so the
        # browser tests exercise production's channel instead of a lookalike
        # that can drift from it (both shapes — config rows and the matrix).
        record_panel_set = handlers[ClientEvent.PANEL_SET]

        async def panel_set(data: dict[str, Any]) -> None:
            await record_panel_set(data)
            room_patch = data.get("room")
            if harness_ref and isinstance(room_patch, dict):
                action = room_patch.get("action")
                room = harness_ref[0].room
                if action == "connect":
                    room.update(
                        connected=True,
                        active_room_id=int(room_patch.get("room_id") or 0),
                        error="",
                    )
                elif action == "disconnect":
                    # A CLEAN disconnect: connected false, error empty — the
                    # exact pair that used to strand roomPending forever.
                    room.update(connected=False, active_room_id=0, error="")
            apply_panel_edits(
                settings,
                data,
                announce=lambda line: hub.broadcast(
                    ServerEvent.EVENT_FEED, {"kind": "system", "text": line}
                ),
            )
            speak = settings.interaction.speak
            hub.broadcast(
                ServerEvent.PANEL_STATE,
                {
                    "panicked": False,
                    "speak": {n: bool(getattr(speak, n)) for n in type(speak).model_fields},
                    # The audio trio rides the same echo so the pet's quick
                    # keys are observable end-to-end (their ack gate lifts on
                    # this frame).
                    "audio": {
                        "input_enabled": settings.audio.input_enabled,
                        "output_enabled": settings.audio.output_enabled,
                        "noise_sensitivity": settings.audio.noise_sensitivity,
                    },
                    "room": {
                        **(harness_ref[0].room if harness_ref else {}),
                        "streamer_name": settings.persona.streamer_name,
                        "stream_intro": settings.room.stream_intro,
                    },
                    # The appearance echo is what lets a picked skin remount
                    # the pet without a reconnect — same key production sends.
                    "appearance": {
                        "skins": list_skin_packs(
                            packaged_skins_root(),
                            harness_ref[0].user_skins_root if harness_ref else None,
                        ),
                        "avatar": {
                            "renderer": settings.avatar.renderer,
                            "model_id": settings.avatar.model_id,
                        },
                    },
                },
            )

        handlers[ClientEvent.PANEL_SET] = panel_set
    broker = AudioBroker()
    uplink: list[bytes] = []

    async def on_audio(chunk: bytes) -> None:
        uplink.append(chunk)

    app = create_ui_app(
        hub=hub,
        registry=registry,
        settings=settings,
        token=_TOKEN,
        origin=origin,
        handlers=handlers,
        hello=harness_ref[0].hello if harness_ref else dict,
        broker=broker,
        on_audio=on_audio,
        user_skins_root=harness_ref[0].user_skins_root if harness_ref else None,
    )
    server = UiServer(app, sock)
    if harness_ref:
        harness_ref[0].server = server
        harness_ref[0].port = real_port
        harness_ref[0].url = f"{origin}/{_TOKEN}/"
        harness_ref[0].origin = origin
        harness_ref[0].calls = recorder.calls
        harness_ref[0].broker = broker
        harness_ref[0].uplink = uplink
    server.start()
    return server


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    hub = UiHub(SystemClock())
    ha = Harness(
        hub=hub,
        url="",
        origin="",
        port=0,
        calls=[],
        avatar={"renderer": "sprite", "model_id": ""},
        server=None,  # type: ignore[arg-type]  # filled by _build_server
    )
    _build_server(hub, [ha])
    for _ in range(200):
        if ha.server.started:
            break
        await asyncio.sleep(0.01)
    yield ha
    await hub.aclose()
    await ha.server.stop()


# Function-scoped on purpose: a session-scoped async fixture would need a
# session-scoped event loop, and pytest-asyncio's default function loop
# deadlocks it before the first test. A headless-shell launch is ~0.3s.
@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            launched = await pw.chromium.launch(
                args=[
                    # A synthetic microphone, granted without a prompt: headless
                    # chromium has no capture device, and without one the audio
                    # tests would assert against a getUserMedia that always
                    # rejects — green for the wrong reason.
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    # Autoplay policy parks an AudioContext until a gesture,
                    # and nothing here clicks before she first speaks.
                    "--autoplay-policy=no-user-gesture-required",
                ]
            )
        except Exception:
            pytest.skip("chromium 未装：.venv/bin/python -m playwright install chromium")
        yield launched
        await launched.close()


# bypass_csp: the shipped CSP (default-src 'self') rightly blocks eval,
# which wait_for_function needs. The header itself is pinned by
# test_ui_server; the harness gets a pass.
@pytest.fixture
async def page(browser: Browser, harness: Harness) -> AsyncIterator[Page]:
    context = await browser.new_context(color_scheme="light", bypass_csp=True)
    opened = await context.new_page()
    await opened.goto(harness.url)
    yield opened
    await context.close()


async def _wait(page: Page, expr: str, *, timeout_ms: int = 5000) -> None:
    await page.wait_for_function(expr, timeout=timeout_ms)


# ------------------------------------------------------------ arrival


async def test_hello_names_the_persona_and_mounts_the_tofu(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    # The built-in skin rides the sprite pipeline: a canvas, not CSS divs.
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")


async def test_voice_state_drives_the_stage(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "thinking"})
    await _wait(page, "document.getElementById('stage').dataset.visual === 'thinking'")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    await _wait(page, "document.getElementById('stage').dataset.visual === 'speaking'")


# ------------------------------------------------------------ bubble


async def test_bubble_streams_then_lingers_then_hides(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "今晚"})
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "打两把"})
    await _wait(page, "!document.getElementById('bubble').hidden")
    assert "今晚打两把" in await page.text_content("#bubble")  # type: ignore[operator]
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "idle"})
    # LINGER_MS is 1.5s; allow slack.
    await _wait(page, "document.getElementById('bubble').hidden", timeout_ms=4000)


async def test_a_new_reply_replaces_the_lingering_one(page: Page, harness: Harness) -> None:
    """The bubble outlives its text stream on purpose, so a reply arriving
    inside that linger window used to be APPENDED to the previous one — on a
    busy stream every reply concatenated until a quiet gap finally cleared it."""
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "第一句"})
    harness.hub.broadcast(ServerEvent.REPLY_DONE, {"status": "completed", "text": "第一句"})
    await _wait(page, "document.getElementById('bubble').textContent.includes('第一句')")
    # Idle starts the 1.5s linger; the next reply lands well inside it.
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "idle"})
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "第二句"})
    await _wait(
        page,
        "(() => { const b = document.getElementById('bubble');"
        " return !b.hidden && b.textContent === '第二句'; })()",
    )


async def test_a_reply_right_after_a_shatter_still_shows(page: Page, harness: Harness) -> None:
    """The regression the review found: the shatter animation ends invisible,
    and voice-state noise inside its window used to cancel the cleanup —
    freezing the bubble transparent through the NEXT reply."""
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "这句会被掐断"})
    await _wait(page, "!document.getElementById('bubble').hidden")
    harness.hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": "barge_in"})
    # The barge-in's listening edge lands inside the shatter window.
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "listening"})
    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "新的一句"})
    await _wait(
        page,
        "(() => { const b = document.getElementById('bubble');"
        " return !b.hidden && b.textContent.includes('新的一句')"
        " && !b.classList.contains('shatter'); })()",
    )


# ------------------------------------------------------------ panel


async def test_panel_tabs_switch_with_aria(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await _wait(page, "document.getElementById('panel').classList.contains('open')")
    await page.click("[data-tab='chat']")
    await _wait(
        page,
        "document.getElementById('tab-chat').classList.contains('active')"
        " && document.querySelector(\"[data-tab='chat']\").getAttribute('aria-selected')"
        " === 'true'",
    )


async def test_panic_state_flips_the_button(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.PANEL_STATE, {"panicked": True, "speak": {"danmaku": True}})
    await _wait(
        page,
        "(() => { const b = document.getElementById('p-panic');"
        " return b.textContent === '恢复说话'"
        " && b.getAttribute('aria-pressed') === 'true'; })()",
    )


async def test_pet_click_sends_a_poke(page: Page, harness: Harness) -> None:
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
    await page.click("#pet-mount")
    for _ in range(100):
        if any(event is ClientEvent.PET_POKE for event, _ in harness.calls):
            break
        await asyncio.sleep(0.05)
    assert any(event is ClientEvent.PET_POKE for event, _ in harness.calls)


async def test_config_tab_offers_editors_for_live_and_badges_for_frozen(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='config']")
    # A live field renders a control...
    await _wait(
        page,
        "document.querySelector(\"[data-path='interaction.speak.danmaku'] input[type=checkbox]\")"
        " !== null",
    )
    # ...chattiness went live with the control-centre rework and renders a
    # select now...
    await _wait(
        page,
        "document.querySelector(\"[data-path='interaction.chattiness'] select\") !== null",
    )
    # ...and a frozen field renders greyed with its reload badge, no control.
    await _wait(
        page,
        "(() => { const row = document.querySelector(\"[data-path='avatar.renderer']\");"
        " return row && !row.querySelector('input,select')"
        " && row.querySelector('.cfg-badge').textContent === '重启生效'; })()",
    )


async def test_config_edit_round_trips_to_the_settings_object(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='config']")
    box = "[data-path='interaction.speak.danmaku'] input[type=checkbox]"
    await _wait(page, f'document.querySelector("{box}") !== null')

    # Read through a call so mypy cannot narrow the attribute to Literal[True]
    # at the first assert and call the later False-checks unreachable.
    def danmaku() -> bool:
        return harness.settings.interaction.speak.danmaku

    assert danmaku() is True
    await page.click(box)
    # The edit lands on the real Settings object on the server side...
    for _ in range(100):
        if danmaku() is False:
            break
        await asyncio.sleep(0.05)
    assert danmaku() is False
    # ...the ack line reaches the chat feed...
    await page.click("[data-tab='chat']")
    await _wait(
        page,
        "[...document.querySelectorAll('#timeline .entry')]"
        ".some(e => e.textContent.includes('配置已改：普通弹幕 → 关'))",
    )
    # ...and the canonical re-fetch keeps the control on the applied value.
    await page.click("[data-tab='config']")
    await _wait(
        page,
        f'(() => {{ const b = document.querySelector("{box}");'
        " return b !== null && b.checked === false; })()",
        timeout_ms=6000,
    )


# ------------------------------------------------------------ reconnect


async def test_reconnect_does_not_double_the_panel(page: Page, harness: Harness) -> None:
    """The server replays its rings on every attach; the page must clear the
    timeline first, or every reconnect doubles the history."""
    await _wait(page, "document.title.includes('豆腐')")
    for n in range(3):
        harness.hub.broadcast(
            ServerEvent.EVENT_FEED, {"kind": "danmaku", "name": "阿强", "text": f"第{n}条"}
        )
    await _wait(page, "document.querySelectorAll('#timeline .entry').length === 3")
    port = harness.port
    await harness.server.stop()  # the 3s axe covers the held WebSocket
    _build_server(harness.hub, [harness], port=port)
    for _ in range(200):
        if harness.server.started:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("重建的 UiServer 没起来")
    # A FOURTH entry, broadcast only after the rebuild, is what proves the page
    # actually came back: waiting on the old count alone passes instantly
    # against the stale pre-disconnect DOM, which made this test green even
    # with the de-duplication deleted. The page reconnects on its own (same
    # port, same token) and eats the replay onto a CLEARED timeline, so the
    # replayed three plus this one is four — a broken reset gives seven.
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED, {"kind": "danmaku", "name": "阿强", "text": "重连后"}
    )
    await _wait(
        page,
        "document.querySelectorAll('#timeline .entry').length === 4",
        timeout_ms=15000,
    )


# ------------------------------------------------------------ themes & motion


async def test_dark_mode_follows_the_system(browser: Browser, harness: Harness) -> None:
    context = await browser.new_context(color_scheme="dark", bypass_csp=True)
    dark_page = await context.new_page()
    try:
        await dark_page.goto(harness.url)
        await _wait(dark_page, "document.title.includes('豆腐')")
        bg = await dark_page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == "rgb(31, 30, 29)"  # --page in theme-dark.css (Claude Code charcoal)
    finally:
        await context.close()


async def test_reduced_motion_freezes_the_sprite(browser: Browser, harness: Harness) -> None:
    context = await browser.new_context(reduced_motion="reduce", bypass_csp=True)
    still_page = await context.new_page()
    try:
        await still_page.goto(harness.url)
        await _wait(still_page, "document.querySelector('#pet-mount canvas') !== null")
        snap = "document.querySelector('#pet-mount canvas').toDataURL()"
        first = await still_page.evaluate(snap)
        await still_page.wait_for_timeout(900)  # past a blink cycle's frame flips
        second = await still_page.evaluate(snap)
        assert first == second, "reduced motion must freeze the frame loop"
    finally:
        await context.close()


async def test_blink_actually_animates_without_reduced_motion(page: Page) -> None:
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
    snap = "document.querySelector('#pet-mount canvas').toDataURL()"
    first = await page.evaluate(snap)
    # The idle track blinks every 1.6s; two seconds must show a change.
    await _wait(
        page,
        f"document.querySelector('#pet-mount canvas').toDataURL() !== {first!r}",
        timeout_ms=3000,
    )


# ------------------------------------------------------------ degrade


async def test_missing_pack_degrades_to_tofu_with_a_warning(
    browser: Browser, harness: Harness
) -> None:
    harness.avatar = {"renderer": "sprite", "model_id": "no-such-skin"}
    context = await browser.new_context(bypass_csp=True)
    warned: list[str] = []
    degraded_page = await context.new_page()
    degraded_page.on(
        "console", lambda msg: warned.append(msg.text) if "退回内置形象" in msg.text else None
    )
    try:
        await degraded_page.goto(harness.url)
        await _wait(degraded_page, "document.querySelector('#pet-mount canvas') !== null")
        assert warned, "the degrade path must announce itself in the console"
    finally:
        await context.close()


async def test_a_missing_pack_leaves_a_line_where_the_streamer_can_read_it(
    browser: Browser, harness: Harness
) -> None:
    """The console is not a place a streamer can look.

    There are no devtools in the shell, so a skin that failed to load used to
    surface as 「换了皮肤怎么还是豆腐」 and nothing else. §15.12 asked for a
    line in the log area; this is that line.
    """
    harness.avatar = {"renderer": "sprite", "model_id": "no-such-skin"}
    context = await browser.new_context(bypass_csp=True)
    degraded_page = await context.new_page()
    try:
        await degraded_page.goto(harness.url)
        await _wait(degraded_page, "document.querySelector('#pet-mount canvas') !== null")
        await _wait(degraded_page, "document.querySelectorAll('#loglines .logline').length > 0")
        text = await degraded_page.locator("#loglines").inner_text()
        assert "no-such-skin" in text, f"日志区没提是哪个皮肤包没加载上：{text}"
        assert "退回内置形象" in text, f"日志区没说降级到了哪儿：{text}"
        # The log tab is not the one the panel opens on; a line there with
        # nothing pointing at it is only marginally better than the console.
        alert = await degraded_page.locator("#tab-btn-logs").get_attribute("data-alert")
        assert alert == "1", "日志标签没有未读标记，这行等于还是没人看得见"
    finally:
        await context.close()


async def test_a_renderer_this_build_cannot_mount_says_so_instead_of_degrading_quietly(
    browser: Browser, harness: Harness
) -> None:
    """live2d is a legal config value (config/schema.py's AvatarConfig) with no
    implementation on this side — stage 5's work, still behind §6.4's licensing
    gate. Until then the page mounts the built-in instead, which is right; doing
    it without a word is not. The streamer edits the config, the pet looks
    identical, and nothing anywhere says the setting did not take.
    """
    harness.avatar = {"renderer": "live2d", "model_id": "hiyori"}
    context = await browser.new_context(bypass_csp=True)
    page = await context.new_page()
    try:
        await page.goto(harness.url)
        # Still a pet on screen: saying so must not cost the fallback.
        await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
        await _wait(page, "document.querySelectorAll('#loglines .logline').length > 0")
        text = await page.locator("#loglines").inner_text()
        assert "live2d" in text, f"日志区没说是哪个渲染器没生效：{text}"
        assert "renderer" in text, f"日志区没告诉主播该去改哪个配置项：{text}"
    finally:
        await context.close()


async def test_the_panel_window_hears_about_it_too(browser: Browser, harness: Harness) -> None:
    """Inside the shell the panel is a separate window that mounts no pet at
    all, so a notice raised by the mount would never reach it. The one thing
    both windows do get is hello — which is where the configured renderer
    comes from — so this notice is driven from there."""
    harness.avatar = {"renderer": "live2d", "model_id": "hiyori"}
    context = await browser.new_context(bypass_csp=True)
    panel_window = await context.new_page()
    try:
        await panel_window.goto(f"{harness.url}#panel")
        await _wait(panel_window, "document.body.classList.contains('panel-only')")
        await _wait(panel_window, "document.querySelectorAll('#loglines .logline').length > 0")
        text = await panel_window.locator("#loglines").inner_text()
        assert "live2d" in text, f"面板窗口不知道配置里的渲染器没生效：{text}"
    finally:
        await context.close()


async def test_an_empty_model_id_mounts_the_builtin_quietly(
    browser: Browser, harness: Harness
) -> None:
    """Reversed with the v4 axis split (ledger #40): sprite + empty model_id
    IS the built-in tofu now — a legal default, not a half-configured pack.
    The old version of this test demanded a warning here; the warning would
    now fire on every fresh install."""
    harness.avatar = {"renderer": "sprite", "model_id": ""}
    context = await browser.new_context(bypass_csp=True)
    page = await context.new_page()
    try:
        await page.goto(harness.url)
        await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
        text = await page.locator("#loglines").inner_text()
        assert "model_id" not in text, f"合法默认不该出警告：{text}"
    finally:
        await context.close()


async def test_one_notice_does_not_pile_up_across_reconnects(page: Page, harness: Harness) -> None:
    """hello arrives on every attach, and the panel wipes its rows on
    reconnect. A notice that re-added itself per hello without the wipe would
    grow a stack of identical lines; one that never re-added itself would
    vanish at the first reconnect."""
    harness.avatar = {"renderer": "live2d", "model_id": "hiyori"}
    await page.reload()
    await _wait(page, "document.querySelectorAll('#loglines .logline').length > 0")
    harness.hub.broadcast(ServerEvent.HELLO, harness.hello())
    harness.hub.broadcast(ServerEvent.HELLO, harness.hello())
    await asyncio.sleep(0.3)
    count: int = await page.evaluate("document.querySelectorAll('#loglines .logline').length")
    assert count == 1, f"同一条提示重复了 {count} 次"


# ------------------------------------------------------------ health card


async def test_the_health_card_shows_the_breaker_not_just_the_first_four_fields(
    page: Page, harness: Harness
) -> None:
    """The selector reports seven fields and the card showed four.

    breaker_open / breaker_reason / combos_suppressed sit at the end of
    selector.status(), so the three the streamer most needs mid-stream — 熔断了
    没有 —— were the three that could never appear.
    """
    await page.click("#corner")
    await _wait(page, "document.querySelectorAll('#health .card').length >= 2")
    text = await page.locator("#health").inner_text()
    assert "breaker_open" in text, f"熔断状态在健康卡上看不见：{text}"
    assert "breaker_reason" in text, f"熔断原因在健康卡上看不见：{text}"
    assert "combos_suppressed" in text, f"连击压制数在健康卡上看不见：{text}"


# ------------------------------------------------------------ audio in the page


@pytest.fixture
async def audio_page(browser: Browser, harness: Harness) -> AsyncIterator[Page]:
    """A page with a fake microphone, granted without a prompt.

    --use-fake-device-for-media-stream gives getUserMedia a synthetic tone, so
    capture genuinely runs: the worklet resamples, the socket carries frames,
    and the assertions are about real bytes rather than a mock's say-so.
    """
    context = await browser.new_context(
        bypass_csp=True,
        permissions=["microphone"],
    )
    opened = await context.new_page()
    await opened.goto(harness.url)
    yield opened
    await context.close()


@pytest.mark.ui_browser
async def test_the_page_takes_the_devices_and_the_microphone_reaches_the_server(
    audio_page: Page, harness: Harness
) -> None:
    """The whole point of the move, end to end.

    Echo cancellation is why audio lives here now, and it only works on audio
    the browser itself both captures and renders — so if capture does not
    genuinely run in the page, nothing else in this slice matters.
    """
    for _ in range(300):
        if harness.broker is not None and harness.broker.owner is not None:
            break
        await asyncio.sleep(0.02)
    assert harness.broker is not None
    assert harness.broker.owner == "browser", "页面没拿到设备"

    for _ in range(400):
        if harness.uplink:
            break
        await asyncio.sleep(0.02)
    assert harness.uplink, "麦克风没有把帧送上来"
    # 20ms of 16 kHz mono s16 — the frame size section 3.2 asks for. This pins
    # the frame SIZE and the byte-for-byte forwarding under it (server.py
    # relays the payload untouched, and nothing on the way re-frames it). It
    # says nothing about the sample rate: capture-worklet.js:19 posts at a
    # constant FRAME = 320 whatever this.ratio is, so a worklet resampling to
    # the wrong rate would still hand over 640-byte frames.
    assert len(harness.uplink[0]) == 640, len(harness.uplink[0])
    # The rate shows up in HOW OFTEN the frames come, which is the assertion
    # this test was missing: 16 kHz in 320-sample frames is 50 a second. Ship
    # the 48 kHz samples unresampled and it is 150 — the failure that makes the
    # streamer sound three times too slow to the server.
    before = len(harness.uplink)
    started = asyncio.get_running_loop().time()
    await asyncio.sleep(1.5)  # long enough that one late frame cannot swing it
    span = asyncio.get_running_loop().time() - started
    rate = (len(harness.uplink) - before) / span
    assert 35 <= rate <= 70, f"每秒 {rate:.1f} 帧，不是 16 kHz 该有的 50 帧"


@pytest.mark.ui_browser
async def test_reply_audio_plays_and_reports_itself_segment_by_segment(
    audio_page: Page, harness: Harness
) -> None:
    """The receipts that feed the floor gate (ledger #41)."""
    for _ in range(300):
        if harness.broker is not None and harness.broker.owner is not None:
            break
        await asyncio.sleep(0.02)
    assert harness.broker is not None
    assert harness.broker.owner is not None, "页面还没拿到设备就开始播，这条断言先说话"

    # Half a second of 24 kHz silence, in two segments.
    chunk = b"\x00\x00" * 12000
    harness.broker.play(chunk)
    harness.broker.play(chunk)

    for _ in range(400):
        started = [c for c in harness.calls if c[0] == ClientEvent.PLAYBACK_STARTED]
        if len(started) >= 2:
            break
        await asyncio.sleep(0.02)
    started = [c for c in harness.calls if c[0] == ClientEvent.PLAYBACK_STARTED]
    assert len(started) >= 2, f"两段音频只报了 {len(started)} 次开始"


@pytest.mark.ui_browser
async def test_a_barge_in_stops_playback_and_says_how_much_was_heard(
    audio_page: Page, harness: Harness
) -> None:
    for _ in range(300):
        if harness.broker is not None and harness.broker.owner is not None:
            break
        await asyncio.sleep(0.02)
    assert harness.broker is not None
    assert harness.broker.owner is not None, "页面还没拿到设备"
    # Two segments, and the short one is the point. Barging in on a single long
    # buffer reports played_ms=0 — audio.js only counts a segment once its
    # onended fires, so the piece cut mid-flight contributes nothing — and a
    # test written that way passes just as happily against a played_ms that is
    # hard-wired to zero.
    harness.broker.play(b"\x00\x00" * 7200)  # 0.3s at 24 kHz, plays out whole
    harness.broker.play(b"\x00\x00" * 48000)  # 2s, cut in the middle

    for _ in range(400):
        if any(c[0] == ClientEvent.PLAYBACK_ENDED for c in harness.calls):
            break
        await asyncio.sleep(0.02)
    assert any(c[0] == ClientEvent.PLAYBACK_ENDED for c in harness.calls), "第一段没播完"
    harness.hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": "test"})

    for _ in range(400):
        cancelled = [c for c in harness.calls if c[0] == ClientEvent.PLAYBACK_CANCELLED]
        if cancelled:
            break
        await asyncio.sleep(0.02)
    cancelled = [c for c in harness.calls if c[0] == ClientEvent.PLAYBACK_CANCELLED]
    assert cancelled, "打断之后没有回执"
    heard = cancelled[0][1].get("played_ms")
    # 阶段 5 要拿这个数去裁记忆（ui/events.py 里那条注释），所以断言它的**值**：
    # 第一段 300ms 播完了，第二段一刀切在中间。落在这个区间之外只有两种可能——
    # 要么把没播的算进去了，要么把播过的丢了。
    assert isinstance(heard, (int, float)), cancelled[0][1]
    assert 250 <= heard <= 500, f"播完的是 300ms，回执说 {heard}ms"


@pytest.mark.ui_browser
async def test_the_panel_names_the_devices_once_the_microphone_is_granted(
    audio_page: Page, harness: Harness
) -> None:
    """Device labels stay blank until permission lands, which is why the panel
    enumerates after a claim rather than on load — a list of 「未命名设备」
    helps nobody pick a microphone."""
    await audio_page.click("#corner")
    owner = audio_page.locator("#audio-owner")
    await owner.wait_for(state="visible")
    for _ in range(400):
        if "已接管" in (await owner.inner_text()):
            break
        await asyncio.sleep(0.02)
    text = await owner.inner_text()
    assert "已接管" in text, text
    # "Requested", not "on". The browser accepting the constraint is all this
    # window can honestly report: the canceller only subtracts Chromium's own
    # playback, so OBS monitoring or a game goes into the microphone whatever
    # this line says.
    assert "回声消除已开" not in text, f"别把「答应了」说成「做到了」：{text}"
    # 断言列表是**重画过**的，不是出厂那一个占位 option。index.html 里就写着
    # `<option value="">读取中…</option>`，所以「至少有一个 option」这条老断言
    # 在 renderDevices 一次都不被调用时照样绿——它验收不了「面板永远停在读取中」
    # 这个真出过的故障。
    read_options = "[...document.querySelectorAll('#audio-in option')].map(o => [o.value, o.text])"
    options: list[list[str]] = []
    for _ in range(200):  # 列表是一次 request("devices") 往返之后才回来的
        options = await audio_page.evaluate(read_options)
        if options and options[0] == ["", "跟随系统"]:
            break
        await asyncio.sleep(0.02)
    assert options, "麦克风下拉是空的"
    assert options[0] == ["", "跟随系统"], f"第一项该是「跟随系统」：{options}"
    assert not any(label == "读取中…" for _, label in options), f"下拉还停在读取中：{options}"
    # 至少一个真设备，而且带名字：标签要等麦克风授权之后才有，这正是面板不在
    # 加载时就 enumerate 的原因。
    named = [(value, label) for value, label in options[1:] if value and label != "（未命名设备）"]
    assert named, f"没有一个能选的具名麦克风：{options}"


def _level_asks(harness: Harness) -> int:
    """How many times the panel has asked the device holder for the input level."""
    return sum(
        1
        for event, data in harness.calls
        if event is ClientEvent.AUDIO_ASK and data.get("what") == "level"
    )


@pytest.mark.ui_browser
async def test_a_denied_microphone_outlives_the_owner_broadcast(
    browser: Browser, harness: Harness
) -> None:
    """「这个窗口拿不到麦克风」是只有这个窗口知道的事。

    服务端广播的 audio.owner 说的是「谁持有设备」，说不了「持有的那个窗口其实
    一个字都录不上去」。而 audio.owner 是 sticky 帧，每次 attach 都会重放一遍，
    所以只要它能盖掉本地那条错误，主播看到的就永远是「已接管」——面板说麦克风
    活着，实际什么都没传上去。
    """
    context = await browser.new_context(bypass_csp=True)
    # 相当于在浏览器里点了「拒绝」。--use-fake-ui-for-media-stream 会自动同意，
    # 所以要按住 getUserMedia 才拿得到这条真实路径。
    await context.add_init_script(
        "navigator.mediaDevices.getUserMedia = () =>"
        " Promise.reject(new DOMException('拒绝了', 'NotAllowedError'));"
    )
    denied = await context.new_page()
    try:
        await denied.goto(harness.url)
        await _wait(
            denied,
            "document.getElementById('audio-owner').textContent.includes('拿不到麦克风')",
        )
        # 一次重连、或者别的窗口 claim/release，服务端就再广播一遍这一帧。
        harness.hub.broadcast(ServerEvent.AUDIO_OWNER, {"owner": "browser"})
        await asyncio.sleep(0.4)
        text = await denied.locator("#audio-owner").inner_text()
        assert "拿不到麦克风" in text, f"广播把本地那条事实盖掉了：{text}"
        # 也不该顺手把电平表开起来：这个窗口没有麦克风，那条进度条只会恒定 0%。
        await denied.click("#corner")
        await _wait(denied, "document.getElementById('panel').classList.contains('open')")
        await asyncio.sleep(0.6)
        assert _level_asks(harness) == 0, f"没有麦克风还在问电平：{_level_asks(harness)} 次"
    finally:
        await context.close()


@pytest.mark.ui_browser
async def test_unplugging_the_selected_microphone_falls_back_to_the_system_default(
    audio_page: Page, harness: Harness
) -> None:
    """拔掉正在用的设备之后，下拉框要回到「跟随系统」，不能变成空白。

    HTML 的 value setter 碰到没有 option 匹配时，会把所有 option 取消选中，
    selectedIndex 掉到 -1，读回来是空串——面板既不显示「跟随系统」也不显示真正
    在录的那个设备，主播不知道现在用的是哪个麦克风。
    """
    await audio_page.click("#corner")
    # 先等持有方那份真实列表落地：它是 setAudioOwner 里 request("devices") 的
    # 回包，只在接管时来一次。等它先来，后面两帧才是这条用例自己的。
    await _wait(
        audio_page,
        "[...document.querySelectorAll('#audio-in option')].some(o => o.value !== '')",
    )
    harness.hub.broadcast(
        ServerEvent.AUDIO_DEVICES,
        {"devices": [{"id": "usb-mic", "kind": "audioinput", "label": "USB 麦克风"}]},
    )
    await _wait(
        audio_page,
        "[...document.querySelectorAll('#audio-in option')].some(o => o.value === 'usb-mic')",
    )
    # 直接写 value，不走 select_option：后者会真发一次 use_input，持有方回的是
    # chromium 的假设备列表、里面没有 usb-mic，那一趟往返自己就先把下拉打成 -1
    # 了——测的就不是「拔设备」这一帧了。
    await audio_page.evaluate("document.getElementById('audio-in').value = 'usb-mic'")
    # 设备还在的那次重新枚举，选择要留住——这是回落不能踩坏的正常路径。
    harness.hub.broadcast(
        ServerEvent.AUDIO_DEVICES,
        {
            "devices": [
                {"id": "builtin", "kind": "audioinput", "label": "内建麦克风"},
                {"id": "usb-mic", "kind": "audioinput", "label": "USB 麦克风"},
            ]
        },
    )
    await asyncio.sleep(0.3)
    kept = await audio_page.evaluate("document.getElementById('audio-in').value")
    assert kept == "usb-mic", f"设备还在，选择却被换掉了：{kept}"
    # 拔掉：下一份列表里没有它。
    harness.hub.broadcast(
        ServerEvent.AUDIO_DEVICES,
        {"devices": [{"id": "builtin", "kind": "audioinput", "label": "内建麦克风"}]},
    )
    await asyncio.sleep(0.4)
    picked: dict[str, Any] = await audio_page.evaluate(
        "(() => { const s = document.getElementById('audio-in');"
        " return { index: s.selectedIndex, value: s.value,"
        " text: s.selectedIndex < 0 ? null : s.options[s.selectedIndex].text }; })()"
    )
    assert picked["index"] >= 0, f"下拉变成空白了：{picked}"
    assert picked["text"] == "跟随系统", picked


@pytest.mark.ui_browser
async def test_the_level_meter_polls_only_while_the_panel_is_open(
    audio_page: Page, harness: Harness
) -> None:
    """10 Hz 的电平轮询要跟着面板开合走，跟「谁持有设备」无关。

    轮询是 audio.owner 广播启动的，而那一帧发给所有客户端：一个从没点开过面板、
    只看着桌宠的标签页照样每秒问 10 次，壳里两个窗口各起一份表。每次 ask 还要经
    服务端按客户端数扇出 audio.command 和 audio.level。健康轮询在同一个文件里就是
    跟着面板可见性走的（startHealth / stopHealth），电平表漏了这一条。
    """
    for _ in range(300):
        if harness.broker is not None and harness.broker.owner is not None:
            break
        await asyncio.sleep(0.02)
    assert harness.broker is not None
    assert harness.broker.owner is not None, "页面还没拿到设备"

    await asyncio.sleep(0.8)  # 8 个轮询周期，够了
    assert _level_asks(harness) == 0, f"面板没打开就在问电平：{_level_asks(harness)} 次"

    await audio_page.click("#corner")
    await _wait(audio_page, "document.getElementById('panel').classList.contains('open')")
    await asyncio.sleep(0.8)
    while_open = _level_asks(harness)
    assert while_open >= 4, f"面板开着反而不刷新电平：{while_open} 次"
    # 回包也要真的走完一圈，落到那条进度条上——只发不收等于表还是坏的。
    await _wait(audio_page, "document.getElementById('audio-level').style.width !== ''")

    await audio_page.keyboard.press("Escape")
    await _wait(audio_page, "!document.getElementById('panel').classList.contains('open')")
    settled = _level_asks(harness)
    await asyncio.sleep(0.8)
    assert (
        _level_asks(harness) == settled
    ), f"面板关上之后还在问：{_level_asks(harness) - settled} 次"

    # 没有窗口持有设备时，面板重新打开也没什么可读——问了也只能问到自己。
    harness.hub.broadcast(ServerEvent.AUDIO_OWNER, {"owner": None})
    await _wait(
        audio_page, "document.getElementById('audio-owner').textContent.includes('本机播放')"
    )
    await audio_page.click("#corner")
    await _wait(audio_page, "document.getElementById('panel').classList.contains('open')")
    idle = _level_asks(harness)
    await asyncio.sleep(0.6)
    assert _level_asks(harness) == idle, f"没人持有设备还在问电平：{_level_asks(harness) - idle} 次"


@pytest.mark.ui_browser
async def test_the_panel_only_window_keeps_its_meter(audio_page: Page, harness: Harness) -> None:
    """壳里的面板窗口（#panel）没有「收起」这一说：整扇窗就是面板，open() 永远
    不会被调用。把电平轮询绑到面板可见性上，不能把这扇窗连坐了。"""
    for _ in range(300):
        if harness.broker is not None and harness.broker.owner is not None:
            break
        await asyncio.sleep(0.02)
    assert harness.broker is not None
    assert harness.broker.owner is not None, "页面还没拿到设备"
    # 设备在桌宠那扇窗，面板窗自己不 claim——两扇窗抢同一个麦克风是另一个故障。
    panel_window = await audio_page.context.new_page()
    try:
        await panel_window.goto(f"{harness.url}#panel")
        await _wait(panel_window, "document.body.classList.contains('panel-only')")
        before = _level_asks(harness)
        await asyncio.sleep(0.8)
        asked = _level_asks(harness) - before
        assert asked >= 4, f"面板窗口的电平表没转起来：0.8 秒问了 {asked} 次"
    finally:
        await panel_window.close()


@pytest.mark.ui_browser
async def test_a_denied_window_stops_blaming_itself_once_something_stronger_takes_over(
    browser: Browser, harness: Harness
) -> None:
    """被顶掉之后，这条「本窗口拿不到麦克风」就不再是关于当前持有者的话。

    上一条测试要的是「广播盖不掉本地事实」。反过来也得成立：本窗口一旦不再争抢
    设备——被壳顶掉，或者自己终于拿到了麦克风——那条旧的拒绝就不该继续挂在新持有者
    头上。否则壳明明录得好好的，面板还在报一个属于别人的故障。
    """
    assert harness.broker is not None
    context = await browser.new_context(bypass_csp=True)
    await context.add_init_script(
        "navigator.mediaDevices.getUserMedia = () =>"
        " Promise.reject(new DOMException('拒绝了', 'NotAllowedError'));"
    )
    denied = await context.new_page()
    try:
        await denied.goto(harness.url)
        await _wait(
            denied,
            "document.getElementById('audio-owner').textContent.includes('拿不到麦克风')",
        )
        # 壳来了。走生产那条路：broker.claim 自己去调前任登记的 close。
        await harness.broker.claim(
            "shell", send=lambda pcm: None, close=lambda: None, flush=lambda: None
        )
        harness.hub.broadcast(ServerEvent.AUDIO_OWNER, {"owner": "shell"})
        await _wait(
            denied,
            "!document.getElementById('audio-owner').textContent.includes('拿不到麦克风')",
        )
        text = await denied.locator("#audio-owner").inner_text()
        assert "拿不到麦克风" not in text, f"壳在好好录音，面板还在报本窗口的旧账：{text}"
    finally:
        await context.close()


@pytest.mark.ui_browser
async def test_a_page_older_than_its_server_says_so_instead_of_failing_quietly(
    browser: Browser, harness: Harness
) -> None:
    """hello carries a protocol number, and until now nobody read it.

    A page cannot work out for itself whether its code still matches the server
    that answered. The shell does not hot reload its main process — this session
    hit exactly that, a stale window looking completely normal while the thing
    being tested had moved on. The symptom of a vocabulary mismatch is a feature
    that silently does nothing, which is the worst kind to debug.
    """
    harness.hello_override = {"protocol": 99}
    # bypass_csp like every other browser test here: our own default-src 'self'
    # correctly refuses Playwright's wait_for_function, and the header itself is
    # pinned by test_ui_server rather than by leaving it on in these.
    context = await browser.new_context(bypass_csp=True)
    page = await context.new_page()
    try:
        await page.goto(harness.url)
        await _wait(page, "document.getElementById('panel') !== null")
        # notice() writes into the log pane, which is not the tab the panel
        # opens on — so read the DOM rather than the visible text, and check the
        # tab flag that exists to point at it.
        await _wait(
            page,
            "document.getElementById('loglines').textContent.includes('页面和后端版本对不上')",
        )
        line = await page.locator("#loglines").text_content()
        assert line is not None and "99" in line and "刷新" in line, f"提示没说清：{line}"
        await page.click("#corner")
        flagged = await page.locator("[data-tab='logs']").get_attribute("data-alert")
        assert flagged == "1", "版本对不上却没给日志页签打提醒角标"
    finally:
        await context.close()


async def test_the_panic_button_says_what_it_actually_does(page: Page, harness: Harness) -> None:
    """It stops HER, and it never touches the microphone.

    The label used to read 「紧急闭麦」. 闭麦 means muting your own microphone,
    which is the opposite of what the button does — and the same word was
    already spoken for by `--mute-while-speaking`, which really does mute the
    microphone. One word, two opposite meanings, in one program.
    """
    await _wait(page, "document.title.includes('豆腐')")
    label = await page.locator("#p-panic").inner_text()
    assert label == "紧急叫停", f"按钮文案又变回去了：{label}"
    assert "闭麦" not in label
    title = await page.locator("#p-panic").get_attribute("title")
    assert title is not None and "不碰麦克风" in title, "没说清它不关麦克风"

    # The button lives in the panel header, which is off-screen until opened.
    await page.click("#corner")
    await _wait(page, "document.getElementById('panel').classList.contains('open')")
    await page.click("#p-panic")
    for _ in range(100):
        hit = [
            data
            for event, data in harness.calls
            if event is ClientEvent.PANEL_SET and "panic_mute" in data
        ]
        if hit:
            assert hit[0]["panic_mute"] is True
            return
        await asyncio.sleep(0.02)
    raise AssertionError("点了叫停，服务端什么也没收到")


async def test_an_empty_log_pane_explains_itself(page: Page, harness: Harness) -> None:
    """A healthy session logs almost nothing, so blank is the normal state.

    Only 15 call sites in src log at info, and the per-turn detail a streamer
    watches — dispatch verdicts, context pushes, barge-ins — goes to the
    terminal through print() and never enters the logging stream. Three real
    sessions measured one JSON line each against 14 to 99 printed ones. A blank
    box reads as a broken feature; this one says where to look instead.
    """
    await _wait(page, "document.title.includes('豆腐')")
    empty = await page.locator("#loglines .empty").inner_text()
    assert "「对话」页" in empty, f"空状态没指路：{empty}"

    harness.hub.broadcast(
        ServerEvent.LOG_LINE,
        {
            "line": '{"ts":"2026-08-25T06:00:00+0800","level":"warning",'
            '"event":"probe.something_broke","logger":"probe","error_text":"人话原因"}'
        },
    )
    await _wait(page, "document.querySelectorAll('#loglines .logline').length > 0")
    assert await page.locator("#loglines .empty").count() == 0, "来了日志，空状态没让位"
    text = await page.locator("#loglines").inner_text()
    assert "人话原因" in text, f"error_text 又被脱敏吃掉了：{text}"


@pytest.mark.ui_browser
async def test_the_log_pane_says_where_the_record_outlives_it(
    browser: Browser, harness: Harness
) -> None:
    """The pane keeps 500 lines and dies with the tab.

    「昨天那次她为什么没说话」 is only answerable from the file, so the path is on
    screen rather than in a document somewhere. The 「打开目录」 button stays
    hidden in a browser tab: a tab cannot open a directory, and a button that
    does nothing is worse than no button.
    """
    harness.hello_override = {"log_path": "/tmp/probe/bilisama/logs/dev-talk.jsonl"}
    context = await browser.new_context(bypass_csp=True)
    page = await context.new_page()
    try:
        await page.goto(harness.url)
        await _wait(page, "document.getElementById('log-path') !== null")
        await _wait(page, "!document.getElementById('log-path').hidden")
        shown = await page.locator("#log-path").inner_text()
        assert "dev-talk.jsonl" in shown, f"日志页没说文件在哪：{shown}"
        assert await page.locator("#log-reveal").is_hidden(), "浏览器标签页里不该有打开目录的按钮"
    finally:
        await context.close()


async def _wait_for_call(harness: Harness, event: ClientEvent, match: Any) -> None:
    """Poll until the recorder holds a matching client call, or fail loudly."""
    for _ in range(400):
        if any(evt is event and match(data) for evt, data in harness.calls):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到 {event} 帧：{harness.calls[-5:]}")


@pytest.mark.ui_browser
async def test_pet_controls_strip_offers_pause_and_the_exit_dialog(
    page: Page, harness: Harness
) -> None:
    """The control-centre rework: four quick controls beside the pet, pause
    asks the server (panel.set {paused}), and right-clicking the pet opens
    the exit dialog whose confirm sends app.quit."""
    await _wait(page, "document.title.includes('豆腐')")
    for control in ("voice-input-toggle", "voice-output-toggle", "pause-toggle", "corner"):
        await _wait(page, f"document.getElementById('{control}') !== null")

    await page.click("#pause-toggle")
    await _wait_for_call(harness, ClientEvent.PANEL_SET, lambda d: d.get("paused") is True)

    # The server's echo drives the button state, not the click itself.
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {"paused": True, "audio": {"input_enabled": True, "output_enabled": True}, "speak": {}},
    )
    await _wait(page, "document.getElementById('pause-toggle').classList.contains('active')")
    await _wait(page, "document.getElementById('voice-input-toggle').disabled === true")

    await page.click("#pet-mount", button="right")
    await _wait(page, "document.getElementById('exit-dialog').hidden === false")
    await page.click("#exit-confirm")
    await _wait_for_call(harness, ClientEvent.APP_QUIT, lambda _d: True)


@pytest.mark.ui_browser
async def test_system_page_room_card_connects_and_streams_room_events(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await _wait(page, "document.getElementById('room-id') !== null")
    await page.fill("#room-id", "9617619")
    await page.click("#room-connect")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("room") or {}).get("action") == "connect"
        and (d.get("room") or {}).get("room_id") == 9617619,
    )

    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "danmaku",
            "name": "阿强",
            "text": "显存怎么算",
            "guard_level": "captain",
            "user_level": 12,
            "medal": {"name": "豆腐", "level": 7, "this_room": True},
        },
    )
    await _wait(page, "document.querySelector('#room-events .room-event') !== null")
    await _wait(
        page,
        "document.querySelector('#room-events .room-event .body')" ".textContent.includes('舰长')",
    )


@pytest.mark.ui_browser
async def test_reply_reference_names_the_danmaku_it_answers(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='chat']")
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "reply",
            "status": "completed",
            "text": "显存按参数量乘精度算",
            "source": "danmaku",
            "reference": {"kind": "danmaku", "name": "阿强", "text": "显存怎么算"},
        },
    )
    await _wait(page, "document.querySelector('#timeline .reply-reference') !== null")
    await _wait(
        page,
        "document.querySelector('#timeline .reply-reference')" ".textContent.includes('阿强')",
    )


@pytest.mark.ui_browser
async def test_batch_reference_lists_candidates_without_claiming_all_were_answered(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='chat']")
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "reply",
            "status": "completed",
            "source": "danmaku",
            "text": "本地更方便控制数据，云端则省去显卡投入。",
            "reference": {
                "kind": "danmaku",
                "events": [
                    {"name": "小松", "text": "本地更保护隐私"},
                    {"name": "小月", "text": "云端不用买显卡"},
                ],
            },
        },
    )
    await _wait(page, "document.querySelector('#timeline .reply-reference') !== null")
    reference = await page.locator("#timeline .reply-reference").text_content()
    assert reference is not None
    assert "候选弹幕" in reference
    assert "小松：本地更保护隐私" in reference
    assert "小月：云端不用买显卡" in reference


@pytest.mark.ui_browser
async def test_assistant_page_switches_after_a_confirm(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "panicked": False,
            "speak": {},
            "assistants": [
                {
                    "id": "tofu",
                    "name": "豆腐",
                    "description": "暖白方块",
                    "identity": "# 豆腐",
                    "personality": "软",
                    "current": True,
                },
                {
                    "id": "hanako",
                    "name": "花子",
                    "description": "别的性子",
                    "identity": "# 花子",
                    "personality": "利落",
                    "current": False,
                },
            ],
        },
    )
    await _wait(page, "document.querySelectorAll('#assistant-cards .assistant-card').length === 2")
    await page.click("#assistant-cards .assistant-card:not(.current)")
    await _wait(page, "document.getElementById('confirm-dialog').hidden === false")
    await page.click("#confirm-accept")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("assistant") or {}).get("action") == "select"
        and (d.get("assistant") or {}).get("id") == "hanako",
    )
    # The editor shows the clicked card; typing arms the save button.
    await _wait(page, "document.getElementById('assistant-editor').hidden === false")
    await page.fill("#assistant-identity", "# 花子\n今晚换个说法")
    await _wait(page, "document.getElementById('assistant-save').disabled === false")


async def test_ordinary_event_replies_stay_out_of_the_bubble_when_voice_is_on(
    page: Page, harness: Harness
) -> None:
    """With voice fully on, a danmaku-lane reply is heard, not ballooned — a
    bubble per danmaku is noise. Voice-turn replies (no source) still bubble;
    the previous test suite pins that half."""
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"text": "谢谢弹幕", "source": "danmaku", "reply_id": "r-dm"}
    )
    await page.wait_for_timeout(200)
    assert await page.evaluate("document.getElementById('bubble').hidden") is True
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"text": "谢谢老板的舰", "source": "guard_buy", "reply_id": "r-gb"}
    )
    await _wait(page, "!document.getElementById('bubble').hidden")


async def test_the_judgment_row_actually_hides_when_marked_hidden(page: Page) -> None:
    """CSS regression: `.test-judge { display:flex }` outranked the UA's
    [hidden] rule, so panel.js's judge.hidden toggle painted the row on every
    card all the time."""
    hidden_display = await page.evaluate("""() => {
          const probe = document.createElement('div');
          probe.className = 'test-judge';
          probe.hidden = true;
          document.body.appendChild(probe);
          const display = getComputedStyle(probe).display;
          probe.remove();
          return display;
        }""")
    assert hidden_display == "none"


async def test_quick_key_toggles_the_mic_through_the_shared_channel(
    page: Page, harness: Harness
) -> None:
    """The pet's voice-input key: ack-gated ask, applied via apply_panel_edits,
    repainted from the panel.state echo — the end-to-end path that was
    structurally untestable while the audio shape lived only in dev-talk."""
    await _wait(page, "document.title.includes('豆腐')")
    assert harness.settings.audio.input_enabled is True
    await page.click("#voice-input-toggle")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("audio") or {}).get("input_enabled") is False,
    )
    # A plain-bool local: mypy narrowed the attribute to Literal[True] above.
    applied: bool = harness.settings.audio.input_enabled
    assert applied is False
    # The echo repaints the key into its off state and lifts the ack gate.
    await _wait(
        page,
        "document.getElementById('voice-input-toggle').getAttribute('aria-pressed') === 'false'"
        " && !document.getElementById('voice-input-toggle').disabled",
    )


async def test_dismissing_a_pending_quit_revives_the_confirm_button(
    page: Page, harness: Harness
) -> None:
    """Esc during the 「正在退出…」 wait must hand the button back: a stuck
    disabled confirm made the pet unquittable for the rest of the session."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.dispatch_event("#pet-mount", "contextmenu")
    await _wait(page, "document.getElementById('exit-dialog').hidden === false")
    await page.click("#exit-confirm")
    await _wait(page, "document.getElementById('exit-confirm').disabled === true")
    await page.keyboard.press("Escape")
    await _wait(page, "document.getElementById('exit-dialog').hidden === true")
    await page.dispatch_event("#pet-mount", "contextmenu")
    await _wait(
        page,
        "document.getElementById('exit-confirm').disabled === false"
        " && document.getElementById('exit-confirm').textContent !== '正在退出…'",
    )


async def test_a_transient_bubble_outlives_the_next_voice_state(
    page: Page, harness: Harness
) -> None:
    """The one-shot line runs on its own timer: a listening frame arriving
    right after used to clear that timer and pin the text forever."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.evaluate("window.__bilisamaBubbleProbe = true")
    # The reply.done fallback path shows the whole line at once.
    harness.hub.broadcast(
        ServerEvent.REPLY_DONE,
        {"status": "completed", "text": "整段补显的一句", "reply_id": "r-t", "source": "guard_buy"},
    )
    await _wait(page, "!document.getElementById('bubble').hidden")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "listening"})
    # Its own linger (4s) still dismisses it despite the voice-state noise.
    await _wait(page, "document.getElementById('bubble').hidden", timeout_ms=6000)


async def test_no_voice_switches_render_pinned_off(page: Page, harness: Harness) -> None:
    """follow/like/share have no speaking intent behind them (#50): the matrix
    shows them grey and off instead of promising speech that never comes."""
    await _wait(page, "document.title.includes('豆腐')")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {"panicked": False, "speak": {"danmaku": True, "follow": True, "like": False}},
    )
    await _wait(page, "document.querySelectorAll('#speak-matrix input').length >= 3")
    pinned = await page.evaluate("""() => {
          const boxes = [...document.querySelectorAll('#speak-matrix label')];
          const byName = Object.fromEntries(
            boxes.map((l) => [l.textContent.trim(), l.querySelector('input')])
          );
          return {
            follow: { disabled: byName['关注']?.disabled, checked: byName['关注']?.checked },
            danmaku: { disabled: byName['普通弹幕']?.disabled },
          };
        }""")
    assert pinned["follow"] == {"disabled": True, "checked": False}
    assert pinned["danmaku"] == {"disabled": False}


async def test_a_clean_disconnect_releases_the_room_pending_latch(
    page: Page, harness: Harness
) -> None:
    """connected=false with an EMPTY error is what a successful disconnect
    looks like; keyed on connected||error, the pending latch never released
    and the connect button stayed dead until a reload."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='system']")
    await page.fill("#room-id", "21452505")
    await page.click("#room-connect")
    await _wait(page, "document.getElementById('room-status').textContent.includes('已连接')")
    await page.click("#room-disconnect")
    await _wait(page, "document.getElementById('room-status').textContent.includes('尚未连接')")
    assert await page.evaluate("document.getElementById('room-connect').disabled") is False
    # And the hint machinery is alive again, not frozen at 「正在断开…」.
    hint = await page.text_content("#room-info-hint")
    assert hint is not None and "正在断开" not in hint


async def test_an_unfocused_dirty_field_survives_a_state_frame(
    page: Page, harness: Harness
) -> None:
    """Focus is not the whole story: text typed and clicked away from is still
    the operator's until saved. A re-broadcast identical state frame used to
    clobber it (and the retired shared saving flag used to clobber the OTHER
    field on any save)."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='system']")
    state: dict[str, Any] = {
        "panicked": False,
        "speak": {},
        "room": {
            "connected": False,
            "active_room_id": 0,
            "error": "",
            "streamer_name": "阿强",
            "stream_intro": "老主题",
        },
    }
    harness.hub.broadcast(ServerEvent.PANEL_STATE, state)
    await _wait(page, "document.getElementById('stream-intro').value === '老主题'")
    await page.fill("#stream-intro", "新草稿")
    await page.click("#p-name")  # blur: the field is dirty AND unfocused
    harness.hub.broadcast(ServerEvent.PANEL_STATE, state)
    await page.wait_for_timeout(200)
    assert await page.evaluate("document.getElementById('stream-intro').value") == "新草稿"
    # The save landing (server echoes the typed value) converges the compare…
    state["room"]["stream_intro"] = "新草稿"
    harness.hub.broadcast(ServerEvent.PANEL_STATE, dict(state))
    await page.wait_for_timeout(200)
    # …after which a genuine server-side change may overwrite again.
    state["room"]["stream_intro"] = "服务器新值"
    harness.hub.broadcast(ServerEvent.PANEL_STATE, dict(state))
    await _wait(page, "document.getElementById('stream-intro').value === '服务器新值'")


async def test_a_second_confirm_ask_supersedes_the_first(page: Page, harness: Harness) -> None:
    """Two pending confirmAction promises used to settle on one click; the
    newer ask must retire the older one and own the dialog alone."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "panicked": False,
            "speak": {},
            "assistants": [
                {
                    "id": "tofu",
                    "name": "豆腐",
                    "description": "",
                    "identity": "# 豆腐",
                    "personality": "软",
                    "current": True,
                },
                {
                    "id": "hanako",
                    "name": "花子",
                    "description": "",
                    "identity": "# 花子",
                    "personality": "利",
                    "current": False,
                },
                {
                    "id": "ming",
                    "name": "小明",
                    "description": "",
                    "identity": "# 小明",
                    "personality": "直",
                    "current": False,
                },
            ],
        },
    )
    await _wait(page, "document.querySelectorAll('#assistant-cards .assistant-card').length === 3")
    await page.click("#assistant-cards .assistant-card:nth-child(2)")
    await _wait(page, "document.getElementById('confirm-dialog').hidden === false")
    first_title = await page.text_content("#confirm-title")
    # element.click() bypasses the overlay's hit-testing — the programmatic
    # double-ask the settle guard exists for.
    await page.evaluate("document.querySelectorAll('#assistant-cards .assistant-card')[2].click()")
    await _wait(page, "document.getElementById('confirm-dialog').hidden === false")
    second_title = await page.text_content("#confirm-title")
    assert first_title != second_title, "the dialog must now speak for the SECOND ask"
    assert second_title is not None and "小明" in second_title
    await page.click("#confirm-accept")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("assistant") or {}).get("id") == "ming",
    )
    selected = [
        (d.get("assistant") or {}).get("id")
        for evt, d in harness.calls
        if evt is ClientEvent.PANEL_SET and (d.get("assistant") or {}).get("action") == "select"
    ]
    assert selected == ["ming"], f"the superseded ask must not fire: {selected}"


async def test_reconnecting_to_a_fresh_backend_resets_room_rows_and_test_state(
    page: Page, harness: Harness
) -> None:
    """panel.reset() on reconnect: a restarted dev-talk replays nothing, so
    stale room-event rows and a test card stuck on 运行中 would be last
    session's ghosts."""
    from bilisama.clock import SystemClock as _SystemClock
    from bilisama.ui.hub import UiHub as _UiHub

    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='system']")
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {"kind": "entry", "name": "路人甲", "room_id": 777, "ts_ms": 1000},
    )
    await _wait(page, "document.querySelectorAll('#room-events .room-event').length === 1")

    old_hub, old_server = harness.hub, harness.server
    await old_server.stop()
    fresh = _UiHub(_SystemClock())
    harness.hub = fresh
    _build_server(fresh, [harness], port=harness.port)
    for _ in range(200):
        if harness.server.started:
            break
        await asyncio.sleep(0.01)
    try:
        # ws.js reconnects on its own; the fresh hub replays nothing, so the
        # reset must leave the room stream empty rather than doubled or stale.
        await _wait(
            page,
            "document.querySelectorAll('#room-events .room-event').length === 0",
            timeout_ms=10000,
        )
        assert await page.evaluate("document.querySelectorAll('.test-card.running').length") == 0
    finally:
        await old_hub.aclose()


async def test_live_mock_restores_the_topic_when_the_room_id_returns(
    page: Page, harness: Harness
) -> None:
    """Backspacing one digit and retyping it used to leave the topic cleared
    and flagged edited — 检测 then persisted an empty stream_intro for a room
    that never changed."""
    harness.hello_override = {
        "panel": {
            "speak": {"danmaku": True},
            "room": {"room_id": 12345, "stream_intro": "今天聊AI"},
        }
    }
    await page.goto(harness.url + "live-mock")
    await _wait(page, "document.getElementById('connection-pill').dataset.state === 'online'")
    await _wait(page, "document.getElementById('stream-intro').value === '今天聊AI'")
    await page.click("#room-id")
    await page.keyboard.press("End")
    await page.keyboard.press("Backspace")  # 12345 -> 1234: an intermediate id
    await _wait(page, "document.getElementById('stream-intro').value === ''")
    await page.keyboard.type("5")  # back to the configured room
    await _wait(page, "document.getElementById('stream-intro').value === '今天聊AI'")


async def test_the_skin_selector_lists_packs_and_marks_the_current_one(
    page: Page, harness: Harness
) -> None:
    """The 「形象与声音」 card on the assistants page: built-in tofu leads and
    reads as current on a fresh config; kirby (internal preview, #31) is not
    advertised."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "panicked": False,
            "speak": {},
            "appearance": {
                "skins": list_skin_packs(packaged_skins_root(), None),
                "avatar": {"renderer": "sprite", "model_id": ""},
            },
        },
    )
    await _wait(page, "document.querySelectorAll('#skin-cards .skin-card').length >= 1")
    cards = await page.evaluate(
        """() => [...document.querySelectorAll('#skin-cards .skin-card')].map((c) => ({
             name: c.querySelector('.assistant-name').textContent,
             current: c.classList.contains('current'),
           }))"""
    )
    assert cards[0]["name"] == "豆腐（内置）" and cards[0]["current"] is True
    assert all("kirby" not in card["name"] for card in cards)


async def test_picking_a_skin_remounts_the_pet_live(
    page: Page, harness: Harness, tmp_path: Any
) -> None:
    """The whole chain: a user pack under the mounted skins root, a click on
    its card, the config edit through the shared channel, and the pet
    remounting from the panel.state echo — no reconnect anywhere. The copied
    kirby assets are 128px frames against tofu's 156, so the canvas width is
    the proof of the swap."""
    import shutil

    user_root = tmp_path / "skins"
    user_root.mkdir()
    shutil.copytree(packaged_skins_root() / "kirby", user_root / "candy")
    # The static /skins mount is fixed at server build; rebuild on the same
    # port with the root in place (the reconnect test's pattern) so the page
    # can actually fetch skins/candy/pet.json.
    # This is a live-swap test: finish the initial mount before taking its
    # server away. Otherwise an in-flight tofu request fails and leaves the
    # intentional CSS fallback, which an unchanged reconnect does not remount.
    await _wait(page, "document.querySelector('#pet-mount canvas')?.width === 156")
    harness.user_skins_root = user_root
    await harness.server.stop()
    _build_server(harness.hub, [harness], port=harness.port)
    for _ in range(500):  # rebinding the same port can take a while under gate load
        if harness.server.started:
            break
        await asyncio.sleep(0.01)

    await _wait(page, "document.title.includes('豆腐')", timeout_ms=10000)
    # Same allowance as the final wait below: the page is reconnecting to a
    # server rebuilt on the same port, and the tofu mount only follows the
    # hello. At the 5 s default this timed out in two of three full gate runs
    # (2026-09-03) while passing every time on its own (ledger #96).
    await _wait(
        page, "document.querySelector('#pet-mount canvas')?.width === 156", timeout_ms=15000
    )
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    # Seed the card list the way production does; the CLICK then exercises the
    # real sendConfig -> apply_panel_edits -> panel.state echo round trip.
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "panicked": False,
            "speak": {},
            "appearance": {
                "skins": list_skin_packs(packaged_skins_root(), user_root),
                "avatar": {"renderer": "sprite", "model_id": ""},
            },
        },
    )
    await _wait(page, "document.querySelectorAll('#skin-cards .skin-card').length >= 2")
    await page.evaluate("""() => [...document.querySelectorAll('#skin-cards .skin-card')]
              .find((c) => c.querySelector('.assistant-name').textContent === 'candy')
              .click()""")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("config") or {}).get("path") == "avatar.model_id"
        and (d.get("config") or {}).get("value") == "candy",
    )
    assert harness.settings.avatar.model_id == "candy"
    # The echo's appearance.avatar reaches main.js and remounts: kirby frames.
    # Generous: the click lands right after a server rebuild, and under gate
    # load the ws reconnect + user-pack fetch race can take a few seconds.
    await _wait(
        page, "document.querySelector('#pet-mount canvas')?.width === 128", timeout_ms=15000
    )


async def test_picking_a_voice_sends_the_config_edit(page: Page, harness: Harness) -> None:
    """The voice half of the card: a hand-fed dashscope snapshot renders a
    select, and choosing sends the edit through the shared config channel.
    Frame only — the harness gate is LIVE-only, and the RECONNECT hook is
    covered at the link layer (test_hosted_link's reconfigure tests)."""
    await _wait(page, "document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "panicked": False,
            "speak": {},
            "voices": {
                "mode": "select",
                "path": "speech.dashscope.voice",
                "current": "longanlingxin",
                "options": [
                    {"id": "longanlingxin", "hint": "242Hz"},
                    {"id": "longanlufeng", "hint": "150Hz"},
                ],
                "hint": "数字是实测基频",
            },
        },
    )
    await _wait(page, "document.querySelector('#voice-card select') !== null")
    await page.select_option("#voice-card select", "longanlufeng")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda d: (d.get("config") or {}).get("path") == "speech.dashscope.voice"
        and (d.get("config") or {}).get("value") == "longanlufeng",
    )


# ------------------------------------------------------- the intent strip


async def _gate_strip(page: Page, which: str) -> tuple[str, str]:
    """(state, text) of one strip, once it has left its loading state."""
    sel = f"#gate-strip-{which}"
    await _wait(page, f"document.querySelector('{sel}').dataset.state !== 'unknown'")
    state: str = await page.evaluate(f"document.querySelector('{sel}').dataset.state")
    text: str = await page.locator(sel).inner_text()
    return state, text


async def test_the_intent_strip_answers_both_questions_on_both_pages(
    page: Page, harness: Harness
) -> None:
    """Is the gate on, and is it doing anything — on the two pages a streamer reads.

    Those are different questions and the panel could answer neither. The
    cumulative skip count is dominated by however the session opened, and a
    gate that never fires logs nothing at all, so 「开着但一条都没拦」 and
    「关着」 looked identical. Both were misread during the 2026-09-09
    investigation, twice.
    """
    await page.click("#corner")
    for which in ("system", "chat"):
        state, text = await _gate_strip(page, which)
        assert state == "working", f"{which} 页状态不对：{state}"
        assert "只接对我说的" in text, text
        assert "最近 20 轮拦下 5 条" in text, text


async def test_a_gate_that_holds_nothing_looks_different_from_a_gate_that_is_off(
    page: Page, harness: Harness
) -> None:
    harness.gate_health = {**harness.gate_health, "recent_turns": 20, "recent_skipped": 0}
    await page.click("#corner")
    state, text = await _gate_strip(page, "chat")
    assert state == "idle", "开着却一条都没拦，必须和正常态区分开"
    assert "一条都没拦下" in text, text

    harness.gate_health = {**harness.gate_health, "mode": "always"}
    await _wait(page, "document.querySelector('#gate-strip-chat').dataset.state === 'off'")
    _, off_text = await _gate_strip(page, "chat")
    assert "每句都接" in off_text, off_text


async def test_a_young_session_is_not_reported_as_collapsed(page: Page, harness: Harness) -> None:
    """Three turns with no skip is a session that just started, not a failure."""
    harness.gate_health = {**harness.gate_health, "recent_turns": 3, "recent_skipped": 0}
    await page.click("#corner")
    state, _ = await _gate_strip(page, "system")
    assert state == "working", "窗口还没满就报警会天天狼来了"
