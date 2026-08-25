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
from pathlib import Path
from typing import Any

import pytest

from bilisama.clock import SystemClock
from bilisama.config.schema import Settings
from bilisama.obs.health import HealthRegistry
from bilisama.ui.config_edit import apply_panel_edits
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import UiServer, bind_ui_socket, create_ui_app
from bilisama.ui.test_runner import load_test_catalog

try:
    from playwright.async_api import Browser, Page, async_playwright
except ImportError:  # pragma: no cover - the gate reports this out loud
    pytest.skip("playwright 未安装：uv pip install playwright", allow_module_level=True)

pytestmark = pytest.mark.ui_browser

_TOKEN = "uitest0token0000deadbeef"
_ROOT = Path(__file__).resolve().parents[2]
_TEST_CATALOG = load_test_catalog(_ROOT / "config" / "testsets").public()


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
    input_enabled: bool = True
    output_enabled: bool = True
    paused: bool = False
    _sock_port: int = 0

    def hello(self) -> dict[str, Any]:
        return {
            "protocol": 1,
            "persona": {"id": "mia", "name": "米娅"},
            "provider": "s2s",
            "room_connected": False,
            "avatar": self.avatar,
            "panel": {
                "paused": self.paused,
                "panicked": False,
                "audio": {
                    "input_enabled": self.input_enabled,
                    "output_enabled": self.output_enabled,
                    "noise_sensitivity": 50,
                    "signal_level": 0,
                },
                "speak": {
                    "danmaku": True,
                    "gift": False,
                    "super_chat": True,
                    "guard_buy": True,
                    "vip_enter": True,
                    "entry": True,
                    "follow": False,
                    "like": False,
                    "share": False,
                    "proactive": True,
                    "background_result": False,
                },
                "interaction": {
                    "chattiness": "medium",
                    "reply_length": "medium",
                    "danmaku_window_s": 20,
                    "gift_battery_medium": 100,
                    "gift_battery_high": 1000,
                    "entry_welcome": {"ordinary": True, "naval": True, "ranking": True},
                },
                "room": {
                    "room_id": 0,
                    "stream_intro": "今晚测试多模态模型",
                    "connected": False,
                    "requested_room_id": 0,
                    "active_room_id": 0,
                    "error": "",
                },
                "persona": {"id": "mia", "name": "米娅", "streamer_name": "主播"},
                "assistants": [
                    {
                        "id": "mia",
                        "name": "mia",
                        "description": "默认伴播助手",
                        "avatar": {"renderer": "tofu", "model_id": ""},
                        "current": True,
                        "profiles": [
                            {
                                "id": "default",
                                "name": "默认人设",
                                "description": "Mia 当前人设",
                                "identity": "mia identity",
                                "personality": "mia personality",
                                "current": True,
                            },
                            {
                                "id": "modified",
                                "name": "改动人设",
                                "description": "更丰富的元气伴播人设",
                                "identity": "modified identity",
                                "personality": "modified personality",
                                "current": False,
                            },
                        ],
                    },
                ],
            },
            "tests": _TEST_CATALOG,
            "test_state": {"status": "idle", "case_id": ""},
            "live_mock": {
                "enabled": True,
                "status": "idle",
                "error": "",
                "room_id": 0,
                "real_room_id": 0,
                "can_start": False,
                "running": False,
                "checks": {
                    "backend": {"ok": True, "label": "伴播后端", "detail": "已连接"},
                    "screen": {"ok": False, "label": "共享画面", "detail": "尚未选择"},
                    "audio": {"ok": False, "label": "共享音轨", "detail": "尚未选择"},
                    "room": {"ok": False, "label": "真实直播间流", "detail": "尚未检测"},
                },
                "events_forwarded": 0,
                "audio_frames": 0,
            },
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
            if isinstance(data.get("paused"), bool):
                harness_ref[0].paused = data["paused"]
            audio = data.get("audio")
            if isinstance(audio, dict):
                if isinstance(audio.get("input_enabled"), bool):
                    harness_ref[0].input_enabled = audio["input_enabled"]
                if isinstance(audio.get("output_enabled"), bool):
                    harness_ref[0].output_enabled = audio["output_enabled"]
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
                    "paused": harness_ref[0].paused,
                    "panicked": False,
                    "audio": {
                        "input_enabled": harness_ref[0].input_enabled,
                        "output_enabled": harness_ref[0].output_enabled,
                    },
                    "speak": {n: bool(getattr(speak, n)) for n in type(speak).model_fields},
                },
            )

        handlers[ClientEvent.PANEL_SET] = panel_set

        record_app_quit = handlers[ClientEvent.APP_QUIT]

        async def app_quit(data: dict[str, Any]) -> None:
            await record_app_quit(data)
            hub.broadcast(ServerEvent.APP_EXITING, {})

        handlers[ClientEvent.APP_QUIT] = app_quit
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
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 1, "source": "gift", "text": "今晚"}
    )
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 1, "source": "gift", "text": "打两把"}
    )
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
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 1, "source": "gift", "text": "第一句"}
    )
    harness.hub.broadcast(
        ServerEvent.REPLY_DONE,
        {"reply_id": 1, "source": "gift", "status": "completed", "text": "第一句"},
    )
    await _wait(page, "document.getElementById('bubble').textContent.includes('第一句')")
    # Idle starts the 1.5s linger; the next reply lands well inside it.
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "idle"})
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 2, "source": "gift", "text": "第二句"}
    )
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
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 1, "source": "gift", "text": "这句会被掐断"}
    )
    await _wait(page, "!document.getElementById('bubble').hidden")
    harness.hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": "barge_in"})
    # The barge-in's listening edge lands inside the shatter window.
    harness.hub.broadcast(ServerEvent.VOICE_STATE, {"state": "listening"})
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA, {"reply_id": 2, "source": "gift", "text": "新的一句"}
    )
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
    for name in ("system", "chat", "assistants", "tests", "logs"):
        await page.click(f"[data-tab='{name}']")
        await _wait(
            page,
            f"document.getElementById('tab-{name}').classList.contains('active')"
            f" && document.querySelector(\"[data-tab='{name}']\")"
            ".getAttribute('aria-selected') === 'true'"
            " && document.querySelectorAll('.tab-page.active').length === 1",
        )


async def test_emergency_mute_is_removed_and_pause_is_the_top_level_gate(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    assert await page.locator("#p-panic").count() == 0
    assert "紧急闭麦" not in (await page.locator("body").inner_text())
    await page.click("#pause-toggle")
    await _wait(
        page,
        "document.getElementById('pause-toggle').getAttribute('aria-pressed') === 'true'",
    )
    assert (ClientEvent.PANEL_SET, {"paused": True}) in harness.calls
    assert await page.locator("#voice-input-toggle").is_disabled()
    assert await page.locator("#voice-output-toggle").is_disabled()
    assert await page.get_attribute("#pause-toggle", "data-tooltip") == "恢复伴播助手"

    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA,
        {"reply_id": 90, "source": "gift", "text": "暂停时不能显示"},
    )
    await asyncio.sleep(0.1)
    assert await page.locator("#bubble").is_hidden()

    await page.click("#pause-toggle")
    await _wait(
        page,
        "document.getElementById('pause-toggle').getAttribute('aria-pressed') === 'false'",
    )
    assert (ClientEvent.PANEL_SET, {"paused": False}) in harness.calls
    assert await page.locator("#voice-input-toggle").is_enabled()


async def test_bubble_policy_uses_microphone_state_and_reply_source(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA,
        {"reply_id": 1, "source": "danmaku", "text": "弹幕回复不冒泡"},
    )
    harness.hub.broadcast(
        ServerEvent.REPLY_DONE,
        {"reply_id": 1, "source": "danmaku", "status": "completed", "text": "弹幕回复不冒泡"},
    )
    await asyncio.sleep(0.1)
    assert await page.locator("#bubble").is_hidden()

    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA,
        {"reply_id": 2, "source": "super_chat", "text": "SC 回复要冒泡"},
    )
    await _wait(page, "document.getElementById('bubble').textContent.includes('SC 回复要冒泡')")
    harness.hub.broadcast(ServerEvent.PLAYBACK_CLEAR, {"reason": "test"})
    await _wait(page, "document.getElementById('bubble').hidden", timeout_ms=3000)

    await page.click("#voice-input-toggle")
    await _wait(
        page,
        "document.getElementById('voice-input-toggle').getAttribute('aria-pressed') === 'false'",
    )
    harness.hub.broadcast(
        ServerEvent.REPLY_DELTA,
        {"reply_id": 3, "source": "voice", "text": "麦克风关闭后全部冒泡"},
    )
    await _wait(
        page,
        "document.getElementById('bubble').textContent.includes('麦克风关闭后全部冒泡')",
    )


async def test_pet_click_sends_a_poke(page: Page, harness: Harness) -> None:
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
    await page.click("#pet-mount")
    for _ in range(100):
        if any(event is ClientEvent.PET_POKE for event, _ in harness.calls):
            break
        await asyncio.sleep(0.05)
    assert any(event is ClientEvent.PET_POKE for event, _ in harness.calls)
    assert await page.text_content("#bubble") == "喂，戳我干嘛!"
    assert not await page.is_hidden("#bubble")


async def test_quick_controls_toggle_audio_and_keep_text_when_output_is_off(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    assert await page.get_attribute("#voice-input-toggle", "aria-pressed") == "true"
    assert await page.get_attribute("#voice-output-toggle", "aria-pressed") == "true"
    assert await page.get_attribute("#voice-input-toggle", "data-tooltip") == "点击关闭语音输入"
    assert await page.get_attribute("#voice-output-toggle", "data-tooltip") == "点击关闭语音播报"

    await page.click("#voice-input-toggle")
    await _wait(
        page,
        "document.getElementById('voice-input-toggle').getAttribute('aria-pressed') === 'false'",
    )
    assert (ClientEvent.PANEL_SET, {"audio": {"input_enabled": False}}) in harness.calls
    assert await page.get_attribute("#voice-input-toggle", "data-tooltip") == "点击开启语音输入"

    await page.click("#voice-output-toggle")
    await _wait(
        page,
        "document.getElementById('voice-output-toggle').getAttribute('aria-pressed') === 'false'",
    )
    assert (ClientEvent.PANEL_SET, {"audio": {"output_enabled": False}}) in harness.calls
    assert await page.get_attribute("#voice-output-toggle", "data-tooltip") == "点击开启语音播报"

    harness.hub.broadcast(ServerEvent.REPLY_DELTA, {"text": "这句只显示在气泡里"})
    await _wait(
        page, "document.getElementById('bubble').textContent.includes('这句只显示在气泡里')"
    )


async def test_settings_button_reflects_the_panel_open_state(page: Page) -> None:
    await _wait(page, "document.title.includes('米娅')")
    assert await page.get_attribute("#corner", "aria-pressed") == "false"
    await page.click("#corner")
    await _wait(page, "document.getElementById('corner').getAttribute('aria-pressed') === 'true'")
    await page.click("#scrim")
    await _wait(page, "document.getElementById('corner').getAttribute('aria-pressed') === 'false'")


async def test_pet_right_click_requires_confirmation_before_quit(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.querySelector('#pet-mount canvas') !== null")
    await page.click("#pet-mount", button="right")
    await _wait(page, "!document.getElementById('exit-dialog').hidden")
    assert "退出 BiliSama" in (await page.text_content("#exit-dialog"))  # type: ignore[operator]
    await page.click("#exit-cancel")
    await _wait(page, "document.getElementById('exit-dialog').hidden")
    assert not any(event is ClientEvent.APP_QUIT for event, _ in harness.calls)

    await page.click("#pet-mount", button="right")
    await page.click("#exit-confirm")
    for _ in range(100):
        if any(event is ClientEvent.APP_QUIT for event, _ in harness.calls):
            break
        await asyncio.sleep(0.05)
    assert any(event is ClientEvent.APP_QUIT for event, _ in harness.calls)
    await page.wait_for_url("about:blank")


async def test_test_console_runs_one_case_and_records_manual_judgment(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='tests']")
    await _wait(page, "document.querySelectorAll('#test-cases .test-card').length === 20")
    assert "普通弹幕窗口选择" in (await page.text_content("#test-cases"))  # type: ignore[operator]

    card = page.locator("[data-case-id='func.danmaku.select']")
    await card.locator(".test-run").click()
    for _ in range(100):
        if (ClientEvent.TEST_RUN, {"case_id": "func.danmaku.select"}) in harness.calls:
            break
        await asyncio.sleep(0.05)
    assert (ClientEvent.TEST_RUN, {"case_id": "func.danmaku.select"}) in harness.calls

    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "test",
            "status": "event",
            "case_id": "func.danmaku.select",
            "index": 1,
            "total": 4,
        },
    )
    await _wait(
        page, "[...document.querySelectorAll('.test-run')].every(button => button.disabled)"
    )
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "test",
            "status": "completed",
            "case_id": "func.danmaku.select",
            "text": "事件已注入，请按预期人工判断",
        },
    )
    await _wait(
        page,
        "!document.querySelector(\"[data-case-id='func.danmaku.select'] .test-judge\").hidden",
    )
    await card.locator(".test-pass").click()
    await _wait(
        page,
        "document.querySelector(\"[data-case-id='func.danmaku.select'] .test-result\")"
        ".textContent.includes('通过')",
    )


async def test_business_set_can_filter_by_candidate(page: Page) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='tests']")
    await page.click("#test-set-switch .test-set:nth-child(2)")
    await _wait(page, "!document.getElementById('test-candidate').hidden")
    assert await page.input_value("#test-candidate") == "ai-code-tudou"
    assert "AI代码侠土豆" in await page.locator("#test-candidate").inner_text()
    assert await page.locator("#test-cases .test-card").count() == 6
    assert "ComfyUI" in (await page.text_content("#test-cases"))  # type: ignore[operator]
    assert "主播画像" in (await page.text_content("#test-cases"))  # type: ignore[operator]


async def test_live_mock_page_unlocks_start_only_after_preflight(
    page: Page, harness: Harness
) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator.mediaDevices, "getDisplayMedia", {
          configurable: true,
          value: async () => {
            const canvas = document.createElement("canvas");
            canvas.width = 320; canvas.height = 180;
            canvas.getContext("2d").fillRect(0, 0, 320, 180);
            const video = canvas.captureStream(15).getVideoTracks()[0];
            const ctx = new AudioContext();
            const dest = ctx.createMediaStreamDestination();
            const oscillator = ctx.createOscillator();
            const gain = ctx.createGain();
            gain.gain.value = 0.01;
            oscillator.connect(gain).connect(dest);
            oscillator.start();
            window.__uiMockCapture = {ctx, oscillator};
            return new MediaStream([video, dest.stream.getAudioTracks()[0]]);
          },
        });
        """)
    await page.goto(harness.url + "live-mock")
    await _wait(page, "document.title.includes('直播 Mock')")
    assert await page.get_by_label("进房欢迎", exact=True).count() == 1
    assert await page.get_by_label("VIP 进房", exact=True).count() == 0
    assert await page.get_by_label("批量欢迎", exact=True).count() == 0
    assert await page.is_disabled("#start-button")
    await page.click("#share-button")
    await _wait(
        page,
        "(/音轨有声音|音轨已收到/).test(document.getElementById('audio-state').textContent)"
        " && !document.getElementById('share-stop').disabled",
    )
    await page.get_by_label("普通弹幕", exact=True).click()
    await page.get_by_label("进房欢迎", exact=True).click()
    assert await page.input_value("#stream-intro") == "今晚测试多模态模型"
    await page.fill("#room-id", "123")
    assert await page.input_value("#stream-intro") == ""
    await page.fill("#stream-intro", "新房间的机器人演示")
    await page.click("#check-button")
    for _ in range(100):
        if (
            ClientEvent.PANEL_SET,
            {"speak": {"entry": False, "vip_enter": False}},
        ) in harness.calls:
            break
        await asyncio.sleep(0.05)
    assert (ClientEvent.PANEL_SET, {"speak": {"danmaku": False}}) in harness.calls
    assert (
        ClientEvent.PANEL_SET,
        {"speak": {"entry": False, "vip_enter": False}},
    ) in harness.calls
    assert any(
        event is ClientEvent.LIVE_MOCK_CHECK
        and data.get("room_id") == 123
        and data.get("stream_intro") == "新房间的机器人演示"
        and data.get("speak")
        == {
            "danmaku": False,
            "gift": True,
            "super_chat": True,
            "guard_buy": True,
            "entry": False,
            "vip_enter": False,
        }
        and data.get("capture", {}).get("video_live") is True
        and data.get("capture", {}).get("audio_live") is True
        for event, data in harness.calls
    ), [
        {
            "room_id": data.get("room_id"),
            "stream_intro": data.get("stream_intro"),
            "speak": data.get("speak"),
            "video_live": data.get("capture", {}).get("video_live"),
            "audio_live": data.get("capture", {}).get("audio_live"),
        }
        for event, data in harness.calls
        if event is ClientEvent.LIVE_MOCK_CHECK
    ]
    checks = {
        name: {"ok": True, "label": label, "detail": "通过"}
        for name, label in {
            "backend": "伴播后端",
            "screen": "共享画面",
            "audio": "共享音轨",
            "room": "真实直播间流",
        }.items()
    }
    harness.hub.broadcast(
        ServerEvent.LIVE_MOCK_STATE,
        {
            "enabled": True,
            "status": "ready",
            "error": "",
            "room_id": 123,
            "real_room_id": 456,
            "can_start": True,
            "running": False,
            "checks": checks,
            "events_forwarded": 0,
            "audio_frames": 0,
        },
    )
    await _wait(page, "!document.getElementById('start-button').disabled")
    await page.click("#start-button")
    for _ in range(100):
        if (ClientEvent.LIVE_MOCK_START, {}) in harness.calls:
            break
        await asyncio.sleep(0.05)
    assert (ClientEvent.LIVE_MOCK_START, {}) in harness.calls

    harness.hub.broadcast(
        ServerEvent.LIVE_MOCK_EVENT,
        {
            "kind": "gift",
            "name": "老船长",
            "identity": "uid:7",
            "user_level": 10,
            "wealth_level": 3,
            "guard_level": "captain",
            "medal": {"name": "本房牌", "level": 8, "this_room": True},
            "gift": {"name": "情书", "num": 2, "total_battery": 104},
            "value_cny": 10.4,
        },
    )
    await _wait(page, "document.getElementById('monitor-feed').textContent.includes('104 电池')")
    monitor = await page.text_content("#monitor-feed")
    assert monitor is not None
    assert "舰长" in monitor and "本房牌 Lv.8" in monitor and "老船长" in monitor

    harness.hub.broadcast(
        ServerEvent.REPLY_DONE,
        {
            "reply_id": 9,
            "source": "gift",
            "status": "completed",
            "text": "船长来啦，今天这场有你更稳了。",
            "reference": {
                "kind": "gift",
                "name": "老船长",
                "gift": {"name": "情书", "num": 2, "total_battery": 104},
            },
        },
    )
    await _wait(page, "document.getElementById('monitor-feed').textContent.includes('引用礼物')")


async def test_system_page_exposes_only_the_requested_voice_and_strategy_controls(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='system']")
    assert await page.locator("#audio-signal-meter").count() == 1
    assert await page.is_visible(".signal-track")
    assert await page.is_visible("#noise-sensitivity")
    assert await page.is_visible("#chattiness")
    assert await page.is_visible("#reply-length")
    assert await page.is_visible("#stream-intro")
    assert await page.is_visible("#streamer-name-save")
    assert await page.is_visible("#room-info-save")
    assert await page.locator("text=停顿多久算说完").count() == 0
    for label in ("关注", "点赞", "分享"):
        control = page.get_by_label(label, exact=True)
        assert await control.is_disabled()
        assert not await control.is_checked()
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {"speak": {"follow": True, "like": True, "share": True}},
    )
    await asyncio.sleep(0.05)
    for label in ("关注", "点赞", "分享"):
        assert not await page.get_by_label(label, exact=True).is_checked()
    assert "电池" in (await page.text_content("details.advanced"))  # type: ignore[operator]
    assert "高等级粉丝牌用户" in (await page.text_content("details.advanced"))  # type: ignore[operator]
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {"kind": "danmaku", "name": "旧房观众", "text": "旧房间弹幕"},
    )
    await _wait(page, "document.getElementById('room-events').textContent.includes('旧房间弹幕')")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "room": {
                "room_id": 999,
                "stream_intro": "新主题",
                "connected": False,
                "active_room_id": 0,
                "error": "",
            }
        },
    )
    await _wait(
        page,
        "document.getElementById('room-events').textContent.includes('等待新直播间事件')",
    )
    assert "旧房间弹幕" not in (await page.text_content("#room-events"))  # type: ignore[operator]


async def test_system_page_sends_every_runtime_control_to_the_backend(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='system']")

    await page.get_by_label("打开语音输入").click()
    await page.locator("#noise-sensitivity").fill("72")
    await page.get_by_label("进房", exact=True).click()

    await page.fill("#room-id", "abc")
    await page.click("#room-connect")
    assert "请输入有效" in (await page.text_content("#room-status"))  # type: ignore[operator]
    await page.fill("#room-id", "123456")
    await page.click("#room-connect")

    await page.fill("#streamer-name", "小梁")
    assert not await page.is_disabled("#streamer-name-save")
    await page.click("#streamer-name-save")
    await page.fill("#stream-intro", "今晚测试 Agent 状态机和长期记忆")
    assert not await page.is_disabled("#room-info-save")
    await page.click("#room-info-save")
    await page.select_option("#chattiness", "high")
    await page.select_option("#reply-length", "low")
    await page.locator("details.advanced summary").click()
    await page.locator("#danmaku-window").fill("75")
    await page.fill("#gift-medium", "200")
    await page.locator("#gift-medium").press("Tab")
    await page.fill("#gift-high", "2000")
    await page.locator("#gift-high").press("Tab")
    await page.locator("[data-entry-group='ordinary']").click()
    assert not await page.locator(".event-inject").get_attribute("open")
    await page.locator(".event-inject > summary").click()
    await page.fill("#inject-input", "/gift 测试观众 1000")
    await page.locator("#inject button").click()

    expected = [
        (ClientEvent.PANEL_SET, {"audio": {"input_enabled": False}}),
        (ClientEvent.PANEL_SET, {"audio": {"noise_sensitivity": 72}}),
        (ClientEvent.PANEL_SET, {"speak": {"entry": False, "vip_enter": False}}),
        (
            ClientEvent.PANEL_SET,
            {"room": {"action": "connect", "room_id": 123456}},
        ),
        (
            ClientEvent.PANEL_SET,
            {
                "room": {
                    "action": "save_info",
                    "streamer_name": "小梁",
                    "stream_intro": "今晚测试多模态模型",
                }
            },
        ),
        (
            ClientEvent.PANEL_SET,
            {
                "room": {
                    "action": "save_info",
                    "streamer_name": "小梁",
                    "stream_intro": "今晚测试 Agent 状态机和长期记忆",
                }
            },
        ),
        (
            ClientEvent.PANEL_SET,
            {"config": {"path": "interaction.chattiness", "value": "high"}},
        ),
        (
            ClientEvent.PANEL_SET,
            {"config": {"path": "interaction.reply_length", "value": "low"}},
        ),
        (
            ClientEvent.PANEL_SET,
            {"config": {"path": "interaction.danmaku.window_s", "value": 75}},
        ),
        (
            ClientEvent.PANEL_SET,
            {"config": {"path": "interaction.gift_battery_medium", "value": 200}},
        ),
        (
            ClientEvent.PANEL_SET,
            {"config": {"path": "interaction.gift_battery_high", "value": 2000}},
        ),
        (
            ClientEvent.PANEL_SET,
            {
                "config": {
                    "path": "interaction.entry_welcome.ordinary",
                    "value": False,
                }
            },
        ),
        (
            ClientEvent.CONSOLE_LINE,
            {"text": "/gift 测试观众 1000", "as_live": True},
        ),
    ]
    for call in expected:
        for _ in range(100):
            if call in harness.calls:
                break
            await asyncio.sleep(0.01)
        assert call in harness.calls


async def test_room_event_stream_and_chat_page_keep_event_details_and_tags(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {"kind": "transcript", "text": "我先看一下日志", "ts": "2026-08-22T20:00:00"},
    )
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "danmaku",
            "name": "阿强",
            "text": "这个节点怎么连？",
            "identity": "uid:42",
            "user_level": 50,
            "wealth_level": 22,
            "is_admin": True,
            "guard_level": "captain",
            "medal": {"name": "代码侠", "level": 12, "this_room": True},
            "ts": "2026-08-22T20:00:01",
        },
    )
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "gift",
            "name": "奶油泡芙",
            "gift": {"name": "能量石", "num": 10},
            "value_cny": 10,
            "ts": "2026-08-22T20:00:02",
        },
    )
    harness.hub.broadcast(
        ServerEvent.EVENT_FEED,
        {
            "kind": "reply",
            "source": "danmaku",
            "status": "completed",
            "text": "先看节点输入输出类型。",
            "reference": {
                "kind": "danmaku",
                "name": "阿强",
                "text": "这个节点怎么连？",
            },
            "ts": "2026-08-22T20:00:03",
        },
    )
    await page.click("[data-tab='chat']")
    await _wait(page, "document.querySelectorAll('#timeline .entry').length === 4")
    chat = await page.text_content("#timeline")
    assert chat is not None
    assert all(
        text in chat
        for text in (
            "主播语音",
            "弹幕",
            "礼物",
            "这个节点怎么连？",
            "能量石 ×10",
            "引用 弹幕 · 阿强：这个节点怎么连？",
        )
    )

    await page.click("[data-tab='system']")
    await _wait(page, "document.querySelectorAll('#room-events .room-event').length === 2")
    stream = await page.text_content("#room-events")
    assert stream is not None
    assert all(
        text in stream
        for text in (
            "阿强",
            "uid:42",
            "用户 Lv.50",
            "财富 Lv.22",
            "房管",
            "舰长",
            "本房粉丝牌 代码侠 Lv.12",
            "奶油泡芙",
            "能量石 ×10",
        )
    )


async def test_room_disconnect_keeps_microphone_enabled(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    harness.hub.broadcast(
        ServerEvent.PANEL_STATE,
        {
            "room": {
                "room_id": 123456,
                "connected": True,
                "requested_room_id": 123456,
                "active_room_id": 123456,
                "error": "",
            },
        },
    )
    await _wait(page, "!document.getElementById('room-disconnect').disabled")
    assert await page.get_attribute("#voice-input-toggle", "aria-pressed") == "true"
    await page.click("#room-disconnect")
    assert (
        ClientEvent.PANEL_SET,
        {"room": {"action": "disconnect"}},
    ) in harness.calls
    assert await page.get_attribute("#voice-input-toggle", "aria-pressed") == "true"


async def test_config_edit_round_trips_to_the_settings_object(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='system']")
    box = page.get_by_label("普通弹幕", exact=True)
    await box.wait_for()

    # Read through a call so mypy cannot narrow the attribute to Literal[True]
    # at the first assert and call the later False-checks unreachable.
    def danmaku() -> bool:
        return harness.settings.interaction.speak.danmaku

    assert danmaku() is True
    await box.click()
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
    # ...and the pushed panel.state keeps the checkbox on the applied value.
    await page.click("[data-tab='system']")
    await _wait(page, "document.querySelector('#speak-matrix input').checked === false")


async def test_mia_card_switches_two_editable_personas_without_assistant_switch(
    page: Page, harness: Harness
) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='assistants']")
    await _wait(page, "document.querySelectorAll('.assistant-card').length === 1")
    assert (
        await page.locator(".assistant-card[data-assistant-id='mia'] .current-tag").inner_text()
        == "当前"
    )
    assert await page.locator(".assistant-card[data-assistant-id='miku']").count() == 0

    await page.click(".assistant-card[data-assistant-id='mia']")
    assert (
        await page.locator(".assistant-card.selected").get_attribute("data-assistant-id") == "mia"
    )
    await _wait(page, "document.querySelectorAll('.assistant-profile').length === 2")
    assert (
        await page.locator(
            ".assistant-profile[data-profile-id='default'] .current-tag"
        ).inner_text()
        == "当前"
    )
    assert await page.locator("#assistant-save").is_disabled()
    assert "skins/tofu/spritesheet.png" in await page.locator(".assistant-preview.mia").evaluate(
        "el => getComputedStyle(el, '::before').backgroundImage"
    )
    assert await page.get_by_role("button", name="设为当前助手").count() == 0

    profile_call = (
        ClientEvent.PANEL_SET,
        {
            "assistant": {
                "action": "select_profile",
                "id": "mia",
                "profile": "modified",
            }
        },
    )
    await page.click(".assistant-profile[data-profile-id='modified']")
    await _wait(page, "!document.getElementById('confirm-dialog').hidden")
    assert profile_call not in harness.calls
    await page.click("#confirm-cancel")
    assert await page.locator("#confirm-dialog").is_hidden()
    assert profile_call not in harness.calls

    await page.click(".assistant-profile[data-profile-id='modified']")
    await page.click("#confirm-accept")
    assert await page.locator("#assistant-identity").input_value() == "modified identity"
    assert profile_call in harness.calls

    await page.fill("#assistant-identity", "新的改动人设 identity")
    await page.fill("#assistant-personality", "新的改动人设 personality")
    assert await page.locator("#assistant-save").is_enabled()
    identity_save = (
        ClientEvent.PANEL_SET,
        {
            "assistant": {
                "action": "save",
                "id": "mia",
                "profile": "modified",
                "anchor": "identity",
                "text": "新的改动人设 identity",
            }
        },
    )
    await page.click("#assistant-save")
    await _wait(page, "!document.getElementById('confirm-dialog').hidden")
    assert identity_save not in harness.calls
    await page.click("#confirm-cancel")
    assert await page.locator("#assistant-save").is_enabled()
    assert identity_save not in harness.calls

    await page.click("#assistant-save")
    await page.click("#confirm-accept")
    assert identity_save in harness.calls
    assert (
        ClientEvent.PANEL_SET,
        {
            "assistant": {
                "action": "save",
                "id": "mia",
                "profile": "modified",
                "anchor": "personality",
                "text": "新的改动人设 personality",
            }
        },
    ) in harness.calls
    assert await page.locator("#assistant-save").is_disabled()

    updated = harness.hello()["panel"]["assistants"]
    assert isinstance(updated, list)
    mia = updated[0]
    assert isinstance(mia, dict)
    profiles = mia["profiles"]
    assert isinstance(profiles, list)
    for profile in profiles:
        assert isinstance(profile, dict)
        profile["current"] = profile["id"] == "modified"
    harness.hub.broadcast(ServerEvent.PANEL_STATE, {"assistants": updated})
    await _wait(
        page,
        "document.querySelector(\".assistant-profile[data-profile-id='modified'] .current-tag\")?"
        ".textContent === '当前'",
    )
    assert (
        await page.locator(".assistant-profile[data-profile-id='default'] .current-tag").count()
        == 0
    )


async def test_audio_level_frame_updates_the_system_meter(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
    harness.hub.broadcast(ServerEvent.AUDIO_LEVEL, {"level": 73})
    await _wait(page, "document.getElementById('audio-signal-value').textContent === '73'")
    assert await page.locator("#audio-signal-meter").evaluate("el => el.style.width") == "73%"


async def test_logs_page_filters_levels_and_pause_control(page: Page, harness: Harness) -> None:
    await _wait(page, "document.title.includes('米娅')")
    await page.click("#corner")
    await page.click("[data-tab='logs']")
    harness.hub.broadcast(
        ServerEvent.LOG_LINE,
        {"line": '{"ts":"2026-08-22T20:00:00","level":"info","event":"room.ok"}'},
    )
    harness.hub.broadcast(
        ServerEvent.LOG_LINE,
        {
            "line": '{"ts":"2026-08-22T20:00:01","level":"error",'
            '"event":"room.failed","room_id":123}',
        },
    )
    await _wait(page, "document.querySelectorAll('#loglines .logline').length === 2")
    await page.select_option("#log-level", "error")
    assert await page.locator("#loglines .logline.info").is_hidden()
    assert await page.locator("#loglines .logline.error").is_visible()
    assert "room_id=123" in (await page.text_content("#loglines"))  # type: ignore[operator]
    await page.click("#log-pause")
    assert await page.locator("#log-pause").get_attribute("data-paused") == "true"
    assert await page.locator("#log-pause").inner_text() == "继续滚动"
    await page.click("#log-pause")
    assert await page.locator("#log-pause").inner_text() == "暂停滚动"


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
