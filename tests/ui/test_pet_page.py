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
from bilisama.ui.config_edit import apply_panel_edits
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import UiServer, bind_ui_socket, create_ui_app

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
    _sock_port: int = 0

    def hello(self) -> dict[str, Any]:
        return {
            "protocol": 1,
            "persona": {"id": "mia", "name": "米娅"},
            "provider": "s2s",
            "room_connected": False,
            "avatar": self.avatar,
            "panel": {"panicked": False, "speak": {"danmaku": True, "gift": False}},
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
                },
            )

        handlers[ClientEvent.PANEL_SET] = panel_set
    app = create_ui_app(
        hub=hub,
        registry=registry,
        settings=settings,
        token=_TOKEN,
        origin=origin,
        handlers=handlers,
        hello=harness_ref[0].hello if harness_ref else dict,
    )
    server = UiServer(app, sock)
    if harness_ref:
        harness_ref[0].server = server
        harness_ref[0].port = real_port
        harness_ref[0].url = f"{origin}/{_TOKEN}/"
        harness_ref[0].origin = origin
        harness_ref[0].calls = recorder.calls
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
        avatar={"renderer": "tofu", "model_id": ""},
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
            launched = await pw.chromium.launch()
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
    await _wait(page, "document.title.includes('米娅')")
    # The built-in skin rides the sprite pipeline: a canvas, not CSS divs.
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")


async def test_voice_state_drives_the_stage(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "thinking"})
    await _wait(page, "document.getElementById('stage').dataset.visual === 'thinking'")
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "speaking"})
    await _wait(page, "document.getElementById('stage').dataset.visual === 'speaking'")


# ------------------------------------------------------------ bubble


async def test_bubble_streams_then_lingers_then_hides(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='config']")
    # A live field renders a control...
    await _wait(
        page,
        "document.querySelector(\"[data-path='interaction.speak.danmaku'] input[type=checkbox]\")"
        " !== null",
    )
    # ...a frozen field renders greyed with its reload badge, and no control.
    await _wait(
        page,
        "(() => { const row = document.querySelector(\"[data-path='interaction.chattiness']\");"
        " return row && !row.querySelector('input,select')"
        " && row.querySelector('.cfg-badge').textContent === '重启生效'; })()",
    )


async def test_config_edit_round_trips_to_the_settings_object(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
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
    await _wait(page, "document.title.includes('米娅')")
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
        await _wait(dark_page, "document.title.includes('米娅')")
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
