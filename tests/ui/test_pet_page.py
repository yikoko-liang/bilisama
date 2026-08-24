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
    broker: AudioBroker | None = None
    uplink: list[bytes] = field(default_factory=list)
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
