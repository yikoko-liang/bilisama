"""Edit duplex deadlines through the served panel without a model connection."""

from __future__ import annotations

import pytest

from bilisama.config import ProviderName
from bilisama.ui.events import ClientEvent

from .test_pet_page import Harness, _wait_for_call
from .test_pet_page import browser as browser
from .test_pet_page import harness as harness
from .test_pet_page import page as page

try:
    from playwright.async_api import Page, expect
except ImportError:  # pragma: no cover - the gate reports this explicitly
    pytest.skip("playwright 未安装：uv pip install playwright", allow_module_level=True)

pytestmark = pytest.mark.ui_browser

_EDGE = "speech.volcano_duplex.speech_edge_s"
_COMMIT = "speech.volcano_duplex.speech_commit_s"


async def test_duplex_deadlines_are_editable_and_commit_zero_disables(
    page: Page, harness: Harness
) -> None:
    # The fixture owns an in-memory Settings object and a loopback-only UI;
    # changing these values never writes the developer's config or opens ASR.
    harness.settings.speech.provider = ProviderName.VOLCANO_DUPLEX
    await page.wait_for_function("document.title.includes('豆腐')")
    await page.click("#corner")
    await page.click("[data-tab='config']")
    edge = page.locator(f"[data-path='{_EDGE}'] input[type=number]")
    commit = page.locator(f"[data-path='{_COMMIT}'] input[type=number]")
    await expect(edge).to_have_count(1, timeout=2000)
    await expect(commit).to_have_count(1, timeout=2000)
    await expect(edge).to_have_value("1")
    await expect(commit).to_have_value("0")
    await expect(commit).to_have_attribute("min", "0")
    assert await page.locator(f"[data-path='{_COMMIT}'] input[type=checkbox]").count() == 0

    for value in (3, 0):
        await commit.fill(str(value))
        await commit.press("Tab")
        await _wait_for_call(
            harness,
            ClientEvent.PANEL_SET,
            lambda data, expected=value: data == {"config": {"path": _COMMIT, "value": expected}},
        )
        assert harness.settings.speech.volcano_duplex.speech_commit_s == value
        # The canonical HTTP snapshot confirms the actual accepted value,
        # not merely the text that fill() left in the browser's input.
        response = await page.request.get(harness.url + "config")
        rows = {row["path"]: row for row in await response.json()}
        assert rows[_COMMIT]["value"] == value
        await expect(commit).to_have_value(str(value))

    commit_edits = [
        data["config"]["value"]
        for event, data in harness.calls
        if event is ClientEvent.PANEL_SET and data.get("config", {}).get("path") == _COMMIT
    ]
    assert commit_edits == [3, 0]

    await edge.fill("1.5")
    await edge.press("Tab")
    await _wait_for_call(
        harness,
        ClientEvent.PANEL_SET,
        lambda data: data == {"config": {"path": _EDGE, "value": 1.5}},
    )
    assert harness.settings.speech.volcano_duplex.speech_edge_s == 1.5
