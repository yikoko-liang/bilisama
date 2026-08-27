"""Browser-level tests for the audio half of the page: the socket that carries
the microphone, the capture chain behind it, and the playback receipts.

Separate file from test_pet_page.py because these need a page instrumented
BEFORE it loads: an add_init_script recorder around WebSocket and around
getUserMedia. Both are recorders, not fakes — every call goes through to the
real implementation. What they add is a handle for things that are otherwise
invisible from outside the module: how many audio sockets the page opened, and
whether the microphone streams it was granted are still live.

Same tier as the pet page: marked ui_browser, deselected by default, run by the
gate when chromium is installed (`python -m playwright install chromium`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from bilisama.clock import SystemClock
from bilisama.config.schema import Settings
from bilisama.obs.health import HealthRegistry
from bilisama.ui.audio import AudioBroker
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import Handler, UiServer, bind_ui_socket, create_ui_app

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:  # pragma: no cover - the gate reports this out loud
    pytest.skip("playwright 未安装：uv pip install playwright", allow_module_level=True)

pytestmark = pytest.mark.ui_browser

_TOKEN = "uitest0audio0000deadbeef"

# 0.25s of 24 kHz silence — one downlink chunk, therefore one scheduled source
# and one pair of receipts.
_SEGMENT = b"\x00\x00" * 6000

# Installed before any page script runs. `window.__sockets` is what lets a test
# drop the audio socket the way `socket.onerror -> close()` does in production
# (audio.js:113), and count what the page opens in response; `window.__streams`
# and `window.__gum` are how a test can see a microphone the page forgot to let
# go of, which no DOM assertion can reach.
_SPY = """
window.__sockets = [];
window.__streams = [];
window.__gum = [];
class RecordingSocket extends WebSocket {
  constructor(...args) {
    super(...args);
    window.__sockets.push(this);
  }
}
window.WebSocket = RecordingSocket;
const media = navigator.mediaDevices;
const realGetUserMedia = media.getUserMedia.bind(media);
media.getUserMedia = async (constraints) => {
  window.__gum.push(JSON.parse(JSON.stringify(constraints)));
  const granted = await realGetUserMedia(constraints);
  window.__streams.push(granted);
  return granted;
};
"""

_AUDIO_SOCKETS = "window.__sockets.filter((s) => s.url.includes('/audio'))"
_LIVE_TRACKS = (
    "window.__streams.flatMap((s) => s.getTracks())"
    ".filter((t) => t.readyState === 'live').length"
)


@dataclass
class Harness:
    """The server half: the same UiServer the shell talks to, plus the two
    ledgers these tests read — client events and uplink frames."""

    hub: UiHub
    url: str
    server: UiServer
    broker: AudioBroker
    calls: list[tuple[ClientEvent, dict[str, Any]]]
    uplink: list[bytes]

    def count(self, event: ClientEvent) -> int:
        return sum(1 for seen, _ in self.calls if seen is event)

    def payloads(self, event: ClientEvent) -> list[dict[str, Any]]:
        return [data for seen, data in self.calls if seen is event]


def _hello() -> dict[str, Any]:
    return {
        "protocol": 1,
        "persona": {"id": "tofu", "name": "豆腐"},
        "provider": "s2s",
        "room_connected": False,
        "avatar": {"renderer": "tofu", "model_id": ""},
        "panel": {"panicked": False, "speak": {"danmaku": True, "gift": False}},
    }


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    hub = UiHub(SystemClock())
    calls: list[tuple[ClientEvent, dict[str, Any]]] = []
    uplink: list[bytes] = []

    def recorder(event: ClientEvent) -> Handler:
        async def handle(data: dict[str, Any]) -> None:
            calls.append((event, data))

        return handle

    async def on_audio(chunk: bytes) -> None:
        uplink.append(chunk)

    broker = AudioBroker()
    sock = bind_ui_socket(0)
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    registry = HealthRegistry()
    registry.register("assembly", lambda: {"events_seen": 1})
    app = create_ui_app(
        hub=hub,
        registry=registry,
        settings=Settings(),
        token=_TOKEN,
        origin=origin,
        handlers={event: recorder(event) for event in ClientEvent},
        hello=_hello,
        broker=broker,
        on_audio=on_audio,
    )
    server = UiServer(app, sock)
    server.start()
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    yield Harness(
        hub=hub,
        url=f"{origin}/{_TOKEN}/",
        server=server,
        broker=broker,
        calls=calls,
        uplink=uplink,
    )
    await hub.aclose()
    await server.stop()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            launched = await pw.chromium.launch(
                args=[
                    # A synthetic microphone, granted without a prompt: headless
                    # chromium has no capture device, and without one every test
                    # here would assert against a getUserMedia that always
                    # rejects — green for the wrong reason.
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ]
            )
        except Exception:
            pytest.skip("chromium 未装：.venv/bin/python -m playwright install chromium")
        yield launched
        await launched.close()


async def _open(browser: Browser, harness: Harness) -> tuple[BrowserContext, Page]:
    """One instrumented tab, loaded and ready to be asked questions."""
    # bypass_csp for the same reason test_pet_page needs it: the shipped
    # default-src 'self' blocks the eval wait_for_function runs on. The header
    # itself is pinned by test_ui_server.
    context = await browser.new_context(bypass_csp=True, permissions=["microphone"])
    await context.add_init_script(_SPY)
    page = await context.new_page()
    await page.goto(harness.url)
    return context, page


@pytest.fixture
async def audio_page(browser: Browser, harness: Harness) -> AsyncIterator[Page]:
    context, page = await _open(browser, harness)
    yield page
    await context.close()


async def _wait(page: Page, expr: str, *, timeout_ms: int = 8000) -> None:
    await page.wait_for_function(expr, timeout=timeout_ms)


async def _until(check: Callable[[], bool], *, what: str, timeout: float = 8.0) -> None:
    """Poll a server-side condition. asyncio.sleep yields, so this does not
    stall the loop the server itself is running on (ruff ASYNC110 is off here
    for exactly this shape)."""
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(what)


async def _ready(page: Page, harness: Harness) -> None:
    """The page holds the devices, the microphone is flowing, and the CONTROL
    socket is attached to the hub.

    That last one is not implied by the first two: the audio socket is opened
    first (measured — it is ahead of /ws in window.__sockets), so a broadcast
    sent on the strength of the claim alone can go out before anyone is
    subscribed and simply vanish. 「已接管」 in the panel is the sticky
    audio.owner frame having made the round trip to this page, which is proof
    that the next broadcast will land.
    """
    await _until(lambda: harness.broker.owner == "browser", what="页面没拿到设备")
    await _until(lambda: bool(harness.uplink), what="麦克风没有把帧送上来")
    await _wait(page, "document.getElementById('audio-owner').textContent.includes('已接管')")


# ------------------------------------------------------------ the socket


async def test_a_dropped_audio_socket_reconnects_once_and_the_microphone_survives(
    audio_page: Page, harness: Harness
) -> None:
    """One drop must produce one reconnect — not two.

    The page hears its own close and schedules a backoff retry; the server
    releases the devices a millisecond later and broadcasts audio.owner{null},
    which makes the control socket call retry() straight away. When the backoff
    timer nobody cancelled fires on top of that, the third socket is refused
    (4409) and its onclose used to tear down the capture chain the SECOND one
    was using — microphone dead, receipts dead, panel still saying 「已接管」.
    """
    await _ready(audio_page, harness)
    # The way `onerror -> close()` ends a connection (audio.js:113): the page
    # sees the close first, the server learns about it after.
    await audio_page.evaluate(f"{_AUDIO_SOCKETS}.at(-1).close()")
    await _wait(audio_page, f"{_AUDIO_SOCKETS}.length >= 2")
    # Past the first backoff window (350-650ms), which is when the stray timer
    # used to open the third one.
    await asyncio.sleep(1.2)

    opened = await audio_page.evaluate(f"{_AUDIO_SOCKETS}.length")
    assert opened == 2, f"断一次开出了 {opened} 条音频连接"
    assert harness.broker.owner == "browser", "重连之后服务端不认为页面持有设备"
    before = len(harness.uplink)
    await asyncio.sleep(0.4)
    assert len(harness.uplink) > before, "重连之后麦克风没有继续上行"
    # Receipts ride the same socket; a stale binding kills them silently.
    harness.broker.play(_SEGMENT)
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_STARTED) >= 1,
        what="重连之后播放回执发进了空气，说话闸门会一直关着",
    )


async def test_a_stale_socket_closing_late_does_not_take_the_live_one_down(
    audio_page: Page, harness: Harness
) -> None:
    """Ledger #59: the `if (socket !== ws) return` guard in audio.js's onclose.

    stopCapture() and onOwner() are shared across connections, so an old
    socket's close notification arriving after `socket` has moved on tears down
    the capture chain the CURRENT one is using: microphone dead, uplink dead,
    and — because the server still holds the claim — a panel that goes on
    saying 「已接管」 over a window that is deaf.

    The close event is injected rather than raced for, and that is the honest
    part of this test. Ledger #59 recorded five green runs with the guard taken
    out: on this machine the first socket always reaches CLOSED before the
    replacement exists, so the ordering the guard defends against cannot be
    produced by timing here. Dispatching the event puts the page in exactly
    that state — a stale connection's onclose running while a newer one is
    live — which is the condition the guard reads, not a simulation of it.
    4409 is the code the ledger's third socket came back with.
    """
    await _ready(audio_page, harness)
    # A second socket, the ordinary way: drop the first and let the page
    # reconnect (the path test_a_dropped_audio_socket_reconnects pins).
    await audio_page.evaluate(f"{_AUDIO_SOCKETS}.at(-1).close()")
    await _wait(audio_page, f"{_AUDIO_SOCKETS}.length >= 2")
    await _wait(audio_page, f"{_AUDIO_SOCKETS}.at(-1).readyState === WebSocket.OPEN")
    await _until(lambda: harness.broker.owner == "browser", what="重连之后页面没拿回设备")
    await _until(lambda: bool(harness.uplink), what="重连之后麦克风没有恢复上行")

    await audio_page.evaluate(
        f"{_AUDIO_SOCKETS}[0].dispatchEvent(new CloseEvent('close', {{ code: 4409 }}))"
    )
    before = len(harness.uplink)
    await asyncio.sleep(0.5)

    live = await audio_page.evaluate(_LIVE_TRACKS)
    assert live == 1, f"旧连接的 close 把在用的采集链拆了，还剩 {live} 条麦克风"
    assert len(harness.uplink) > before, "旧连接的 close 之后，麦克风不再上行"
    assert harness.broker.owner == "browser", "服务端这边的持有者也被旧连接带走了"
    owner_text = await audio_page.locator("#audio-owner").inner_text()
    assert "已接管" in owner_text, f"面板被旧连接改口了：{owner_text}"


async def test_a_refused_tab_lets_go_of_the_microphone(
    browser: Browser, harness: Harness, audio_page: Page
) -> None:
    """A second tab is refused the devices — AudioBroker.claim turns down an
    equal rank, so two plain tabs are enough, no shell needed (ui/audio.py).
    The refusal lands while this tab's getUserMedia is still in flight, so the
    stopCapture() on the way out has nothing to stop. It must not walk away
    holding a live microphone."""
    await _ready(audio_page, harness)
    context, second = await _open(browser, harness)
    try:
        await _wait(second, "window.__gum.length >= 1")
        await _wait(
            second,
            f"{_AUDIO_SOCKETS}.length >= 1 && {_AUDIO_SOCKETS}.every("
            "(s) => s.readyState === WebSocket.CLOSED)",
        )
        # The grant and the refusal both land well inside this; the fake device
        # opens in tens of milliseconds.
        await asyncio.sleep(0.6)
        live = await second.evaluate(_LIVE_TRACKS)
        assert live == 0, f"被拒的标签页还开着 {live} 条麦克风"
        # ...and it took nothing away from the tab that does hold them.
        assert harness.broker.owner == "browser", "旁观的标签页把持有者挤掉了"
        before = len(harness.uplink)
        await asyncio.sleep(0.4)
        assert len(harness.uplink) > before, "第二个标签页一开，持有者的麦克风就停了"
    finally:
        await context.close()


async def test_the_tab_that_stood_aside_takes_the_devices_when_the_holder_leaves(
    browser: Browser, harness: Harness, audio_page: Page
) -> None:
    """The other side of the refusal, and the reason letting go has to be
    clean rather than merely quiet: standing aside is temporary. Not a bug
    repro — a guard, so that handing the microphone back on a refusal cannot
    leave the tab unable to ever open one again."""
    await _ready(audio_page, harness)
    context, second = await _open(browser, harness)
    try:
        await _wait(
            second,
            f"{_AUDIO_SOCKETS}.length >= 1 && {_AUDIO_SOCKETS}.every("
            "(s) => s.readyState === WebSocket.CLOSED)",
        )
        await audio_page.close()  # the holder walks away
        await _wait(second, f"{_AUDIO_SOCKETS}.some((s) => s.readyState === WebSocket.OPEN)")
        await _until(lambda: harness.broker.owner == "browser", what="没有窗口接手设备")
        before = len(harness.uplink)
        await asyncio.sleep(0.5)
        assert len(harness.uplink) > before, "接手的标签页没把麦克风接上"
        live = await second.evaluate(_LIVE_TRACKS)
        assert live == 1, f"接手之后有 {live} 条采集流"
    finally:
        await context.close()


async def test_two_microphone_switches_in_a_row_leave_one_capture_chain(
    audio_page: Page, harness: Harness
) -> None:
    """Picking one microphone and then another before the first has opened.

    The window is exactly as wide as getUserMedia plus addModule — tens of
    milliseconds on this fake device, seconds on a Bluetooth headset changing
    profile. Two chains means every 20ms frame goes up twice, interleaved, and
    one MediaStream nobody can reach to stop.
    """
    await _ready(audio_page, harness)
    inputs: list[str] = await audio_page.evaluate(
        "async () => (await navigator.mediaDevices.enumerateDevices())"
        ".filter((d) => d.kind === 'audioinput').map((d) => d.deviceId)"
    )
    assert len(inputs) >= 2, f"假设备不够换：{inputs}"
    asked: int = await audio_page.evaluate("window.__gum.length")
    for device in inputs[:2]:
        harness.hub.broadcast(ServerEvent.AUDIO_COMMAND, {"what": "use_input", "id": device})
    await _wait(audio_page, f"window.__gum.length >= {asked + 2}")
    await asyncio.sleep(0.6)

    live = await audio_page.evaluate(_LIVE_TRACKS)
    assert live == 1, f"换了两次麦克风，留下 {live} 条采集流"
    before = len(harness.uplink)
    await asyncio.sleep(1.0)
    frames = len(harness.uplink) - before
    # 20ms frames: one chain is ~50 a second, two chains ~100.
    assert frames < 75, f"一秒上行 {frames} 帧，两条采集链都在往上送"


# ------------------------------------------------------------ receipts


async def test_every_segment_reports_started_and_ended_exactly_once(
    audio_page: Page, harness: Harness
) -> None:
    """The receipts the floor gate counts (ledger #41, PlaybackTally).

    Both directions are failures. A missing ended leaves the count above zero
    and she never gets the floor back; a duplicated started leaves an offset
    that stops the gate closing at all.
    """
    await _ready(audio_page, harness)
    harness.broker.play(_SEGMENT)
    harness.broker.play(_SEGMENT)
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_ENDED) >= 2,
        what="两段音频没有报完 playback.ended —— 说话闸门会一直关着",
    )
    await asyncio.sleep(0.3)  # a duplicate would land inside this
    assert harness.count(ClientEvent.PLAYBACK_STARTED) == 2, "开始回执的条数对不上段数"
    assert harness.count(ClientEvent.PLAYBACK_ENDED) == 2, "结束回执的条数对不上段数"


async def test_a_barge_in_on_the_audio_socket_stops_the_sound(
    audio_page: Page, harness: Harness
) -> None:
    """The stop that travels WITH the audio, not on the control socket.

    That is the whole point of commit a99cd2c/9eaa388: the marker is queued
    behind every sample it is meant to stop, so the page has already seen the
    stale audio by the time it reads the stop. Nothing here touches the control
    socket — if the page stopped honouring the audio-socket marker, playback
    would go back to carrying on for a beat after the text stopped.
    """
    await _ready(audio_page, harness)
    harness.broker.play(b"\x00\x00" * 48000)  # two seconds
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_STARTED) >= 1, what="音频没开始播就谈不上打断"
    )
    harness.broker.flush()
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_CANCELLED) >= 1,
        what="音频套接字上的 playback.clear 没能让页面停下来",
    )
    heard = harness.payloads(ClientEvent.PLAYBACK_CANCELLED)[0]
    assert "played_ms" in heard, heard
    assert 0 <= float(heard["played_ms"]) < 2000, f"打断了却报出整段的时长：{heard}"


async def test_nothing_reports_ended_for_what_the_barge_in_cut_off(
    audio_page: Page, harness: Harness
) -> None:
    """A segment nobody heard the end of must not report one.

    cancelled() zeroes the count; an ended arriving after it drives a second
    on_playback(False) + notify(), and a straggler landing after the next reply
    has started would open the floor over her.
    """
    await _ready(audio_page, harness)
    harness.broker.play(b"\x00\x00" * 48000)  # two seconds, cut off almost at once
    await _until(lambda: harness.count(ClientEvent.PLAYBACK_STARTED) >= 1, what="音频没开始播")
    harness.broker.flush()
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_CANCELLED) >= 1, what="打断之后没有回执"
    )
    await asyncio.sleep(0.6)  # onended fires right after stop(); this covers it
    assert harness.count(ClientEvent.PLAYBACK_ENDED) == 0, "被打断的那一段还回执了 ended"


# ------------------------------------------------------------ the panel's half


async def test_a_device_switch_that_fails_says_so_and_re_sends_the_list(
    audio_page: Page, harness: Harness
) -> None:
    """The error path commit b436391 fixed, with the regression net it never got.

    An unplugged (here: never-existing) speaker makes setSinkId reject. Without
    the catch, the reply never goes out at all: no error line, and — worse — no
    fresh device list, so the dropdown sits on a device that is not playing
    anything and the panel says nothing about it.
    """
    await _ready(audio_page, harness)
    harness.hub.broadcast(
        ServerEvent.AUDIO_COMMAND, {"what": "use_output", "id": "no-such-speaker"}
    )
    await _until(
        lambda: any("error" in one for one in harness.payloads(ClientEvent.AUDIO_REPORT)),
        what="换扬声器失败了，面板一声不吭",
    )
    failed = next(one for one in harness.payloads(ClientEvent.AUDIO_REPORT) if "error" in one)
    assert failed["devices"], f"报错时没把设备列表重发回来：{failed}"
    await _wait(
        audio_page,
        "document.getElementById('audio-owner').textContent.includes('切换设备没成功')",
    )


async def test_capture_asks_the_browser_to_cancel_the_echo(
    audio_page: Page, harness: Harness
) -> None:
    """Why audio moved into the page at all.

    Chromium's canceller only subtracts what Chromium itself rendered, so this
    constraint is the difference between the streamer wearing headphones and
    not. The panel's 「回声消除已请求」 is a constant string — it would keep
    saying it with the constraint gone, which is why this asserts on the
    request that actually reached getUserMedia.
    """
    await _ready(audio_page, harness)
    asked: list[dict[str, Any]] = await audio_page.evaluate("window.__gum")
    assert asked, "页面根本没要过麦克风"
    wanted = asked[0]["audio"]
    assert wanted.get("echoCancellation") is True, f"没请求回声消除：{wanted}"
    assert wanted.get("noiseSuppression") is True, f"没请求降噪：{wanted}"
    assert wanted.get("autoGainControl") is True, f"没请求自动增益：{wanted}"


async def test_a_microphone_switch_that_fails_says_so_too(
    audio_page: Page, harness: Harness
) -> None:
    """The other half of the same error path, which used to be unreachable.

    startCapture() reports its own failure and returns instead of throwing —
    it has to, because ws.onopen calls it too and a rejection there has nowhere
    to land. That left useInput() succeeding on paper: command() replied with a
    plain device list, the panel put the dropdown on the microphone that had
    just been asked for, and nothing was being captured at all. Picking a
    microphone that is gone or blocked is the ordinary way in, so the failure
    now travels back out of useInput().
    """
    await _ready(audio_page, harness)
    # 一个不存在的设备 id。exact 约束下浏览器只能拒绝，正是「拔掉了正在用的那只」
    # 在真机上的形状。
    harness.hub.broadcast(ServerEvent.AUDIO_COMMAND, {"what": "use_input", "id": "no-such-mic"})
    await _until(
        lambda: any("error" in one for one in harness.payloads(ClientEvent.AUDIO_REPORT)),
        what="换麦克风失败了，面板一声不吭",
    )
    failed = next(one for one in harness.payloads(ClientEvent.AUDIO_REPORT) if "error" in one)
    assert failed["devices"], f"报错时没把设备列表重发回来：{failed}"
