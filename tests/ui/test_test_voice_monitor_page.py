"""Test speech is audible in its own Web Audio lane, not assistant playback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

from bilisama.ui.events import ClientEvent
from tests.ui.test_audio_page import (
    _AUDIO_SOCKETS,
    _SPY,
    Harness,
    _ready,
    _until,
    _wait,
)
from tests.ui.test_audio_page import (
    browser as browser,
)
from tests.ui.test_audio_page import (
    harness as harness,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page

pytestmark = pytest.mark.ui_browser

_MONITOR_PCM = b"\x00\x20" * 16000
_ASSISTANT_PCM = b"\x00\x10" * 48000
_PLAYBACK_EVENTS = (
    ClientEvent.PLAYBACK_STARTED,
    ClientEvent.PLAYBACK_ENDED,
    ClientEvent.PLAYBACK_CANCELLED,
)

# Record real sources and socket receipts without replacing their behavior.
# Sample rates identify the two lanes without reaching into module internals.
_WEB_AUDIO_SPY = """
window.__testVoiceReceipts = [];
window.__testVoiceNodes = [];
const originalSend = WebSocket.prototype.send;
WebSocket.prototype.send = function(data) {
  if (typeof data === 'string') {
    try { window.__testVoiceReceipts.push(JSON.parse(data)); } catch {}
  }
  return originalSend.call(this, data);
};
const originalCreateSource = AudioContext.prototype.createBufferSource;
AudioContext.prototype.createBufferSource = function(...args) {
  const source = originalCreateSource.apply(this, args);
  const state = {
    rate: null, stopped: false, ended: false, samples: 0, first: null,
    start: null, duration: null,
  };
  const originalStart = source.start.bind(source);
  const originalStop = source.stop.bind(source);
  source.start = (...startArgs) => {
    state.rate = source.buffer.sampleRate;
    state.samples = source.buffer.length;
    state.first = source.buffer.getChannelData(0)[0];
    state.start = startArgs[0];
    state.duration = source.buffer.duration;
    window.__testVoiceNodes.push(state);
    return originalStart(...startArgs);
  };
  source.stop = (...stopArgs) => {
    state.stopped = true;
    return originalStop(...stopArgs);
  };
  source.addEventListener('ended', () => { state.ended = true; });
  return source;
};
"""


@pytest.fixture
async def monitored_page(browser: Browser, harness: Harness) -> AsyncIterator[Page]:
    context = await browser.new_context(bypass_csp=True, permissions=["microphone"])
    await context.add_init_script(_SPY + "\n" + _WEB_AUDIO_SPY)
    page = await context.new_page()
    await page.goto(harness.url)
    await _ready(page, harness)
    await _until(lambda: harness.broker.monitor_ready, what="页面没有确认测试语音监播就绪")
    yield page
    await context.close()


async def test_ready_monitor_uses_16khz_without_assistant_playback_receipts(
    monitored_page: Page, harness: Harness
) -> None:
    await _wait(
        monitored_page,
        "window.__testVoiceReceipts.some(r => r.event === 'test.voice.ready' "
        "&& r.data.version === 1 && r.data.ready === true)",
    )
    before = {event: harness.count(event) for event in _PLAYBACK_EVENTS}
    harness.broker.monitor_test_voice(_MONITOR_PCM)
    await _wait(monitored_page, "window.__testVoiceNodes.some(n => n.rate === 16000)")
    node = await monitored_page.evaluate("window.__testVoiceNodes.find(n => n.rate === 16000)")
    assert node["samples"] == 16000 and node["first"] == pytest.approx(0.25)
    harness.broker.clear_test_voice()
    await harness.broker.wait_test_voice_clear()
    await _wait(monitored_page, "window.__testVoiceNodes.find(n => n.rate === 16000).stopped")
    assert {event: harness.count(event) for event in _PLAYBACK_EVENTS} == before
    receipts = await monitored_page.evaluate(
        "window.__testVoiceReceipts.filter(r => r.event === 'test.voice.cleared')"
    )
    assert len(receipts) == 1 and isinstance(receipts[0]["data"]["seq"], int)


async def test_assistant_and_monitor_clear_only_their_own_sources(
    monitored_page: Page, harness: Harness
) -> None:
    harness.broker.monitor_test_voice(_MONITOR_PCM)
    harness.broker.play(_ASSISTANT_PCM)
    await _wait(monitored_page, "window.__testVoiceNodes.length === 2")
    await _until(
        lambda: harness.count(ClientEvent.PLAYBACK_STARTED) == 1,
        what="助手音频缺少自己的播放回执",
    )
    harness.broker.flush()
    await _wait(monitored_page, "window.__testVoiceNodes.find(n => n.rate === 24000).stopped")
    assert not await monitored_page.evaluate(
        "window.__testVoiceNodes.find(n => n.rate === 16000).stopped"
    )

    harness.broker.play(_ASSISTANT_PCM)
    await _wait(
        monitored_page, "window.__testVoiceNodes.filter(n => n.rate === 24000).length === 2"
    )
    harness.broker.clear_test_voice()
    await harness.broker.wait_test_voice_clear()
    await _wait(monitored_page, "window.__testVoiceNodes.find(n => n.rate === 16000).stopped")
    assert not await monitored_page.evaluate(
        "window.__testVoiceNodes.filter(n => n.rate === 24000).at(-1).stopped"
    )
    assert harness.count(ClientEvent.PLAYBACK_STARTED) == 2
    assert harness.count(ClientEvent.PLAYBACK_CANCELLED) == 1


async def test_socket_disconnect_stops_both_lanes_then_reestablishes_monitor_ready(
    monitored_page: Page, harness: Harness
) -> None:
    harness.broker.monitor_test_voice(_MONITOR_PCM)
    harness.broker.play(_ASSISTANT_PCM)
    await _wait(monitored_page, "window.__testVoiceNodes.length === 2")
    await monitored_page.evaluate(f"{_AUDIO_SOCKETS}.at(-1).close(4000)")
    await _wait(monitored_page, "window.__testVoiceNodes.every(n => n.stopped)")
    await _wait(monitored_page, f"{_AUDIO_SOCKETS}.length >= 2")
    await _until(lambda: harness.broker.monitor_ready, what="音频重连后监播未重新就绪")
    await _wait(
        monitored_page,
        "window.__testVoiceReceipts.filter(r => r.event === 'test.voice.ready' "
        "&& r.data.ready === true).length >= 2",
    )
    assert harness.broker.owner == "browser"


async def test_monitor_frames_keep_contiguous_source_start_times_with_delivery_jitter(
    monitored_page: Page, harness: Harness
) -> None:
    # Frames arrive slightly slower than their duration but stay within the
    # initial prebuffer. Adding a new cushion per frame would introduce gaps.
    frame = b"\x00\x20" * 512
    for _index in range(5):
        harness.broker.monitor_test_voice(frame)
        await asyncio.sleep(0.04)
    await _wait(
        monitored_page, "window.__testVoiceNodes.filter(n => n.rate === 16000).length === 5"
    )
    nodes = await monitored_page.evaluate("window.__testVoiceNodes.filter(n => n.rate === 16000)")
    for index, node in enumerate(nodes):
        assert node["duration"] == pytest.approx(0.032)
        if index:
            previous = nodes[index - 1]
            assert node["start"] == pytest.approx(
                previous["start"] + previous["duration"], abs=1e-7
            ), "每帧不能重新加预缓冲，否则会把一段语音切成断续片段"
    harness.broker.clear_test_voice()
    await harness.broker.wait_test_voice_clear()
