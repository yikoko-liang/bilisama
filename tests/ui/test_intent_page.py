"""Intent test console rendering and run-scoped judgment, without model calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import pytest

from bilisama.ui.events import ClientEvent, ServerEvent
from tests.ui.test_pet_page import Harness
from tests.ui.test_pet_page import (
    browser as browser,
)
from tests.ui.test_pet_page import (
    harness as harness,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page

pytestmark = pytest.mark.ui_browser


def _case(case_id: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "title": "等待助手说完再继续",
        "group": "TO_ME",
        "duration_s": 20,
        "operator": "自动发送预制语音，无需照读。",
        "events": [],
        "expected": ["先回应主播，再等待下一轮。"],
        "context": ["主播正在比较两个工具。"],
        "expected_intent": "TO_ME",
        "execution": "voice",
        "classification_available": False,
        "steps": [
            {
                "id": "voice-1",
                "kind": "voice",
                "text": "豆腐，你更喜欢哪个？",
                "after": "delay",
                "delay_s": 0,
                "timeout_s": 10,
                "observe_s": 2,
                "expected": "回答主播，不转问观众。",
                "expected_intent": "TO_ME",
                "source_row": 9,
                "events_during": [
                    {
                        "offset_s": 0.3,
                        "event": {
                            "kind": "danmaku",
                            "viewer": {"name": "观众甲"},
                            "text": "这个工具免费吗？",
                        },
                        "source_row": 9,
                    }
                ],
            },
            {
                "id": "voice-2",
                "kind": "voice",
                "text": "好，接着比较。",
                "after": "reply_finished",
                "reply_from_row": 9,
                "delay_s": 1,
                "timeout_s": 15,
                "observe_s": 3,
                "expected": "可以沉默。",
                "source_row": 10,
            },
        ],
    }


@pytest.fixture
async def intent_page(browser: Browser, harness: Harness) -> AsyncIterator[Page]:
    harness.hello_override = {
        "tests": {
            "sets": [
                {
                    "id": "simple",
                    "title": "简单意图测试",
                    "description": "检查七类意图。",
                    "cases": [_case("simple-1"), _case("simple-2")],
                },
                {
                    "id": "hard",
                    "title": "困难多轮测试",
                    "description": "检查多轮时机。",
                    "cases": [_case("hard-1")],
                },
            ]
        },
        "test_state": {"status": "idle", "case_id": ""},
    }
    context = await browser.new_context(bypass_csp=True)
    page = await context.new_page()
    await page.goto(harness.url)
    await page.wait_for_selector("#corner")
    await page.click("#corner")
    await page.click("[data-tab='tests']")
    await page.wait_for_selector(".test-card")
    yield page
    await context.close()


async def _state(page: Page, harness: Harness, **state: Any) -> None:
    harness.hub.broadcast(ServerEvent.EVENT_FEED, {"kind": "test", **state})
    await page.wait_for_function(
        "s => document.querySelector(`[data-case-id='${s.case_id}']`)"
        "?.dataset.status === s.status",
        arg=state,
    )


async def _wait_call(harness: Harness, event: ClientEvent) -> dict[str, Any]:
    for _ in range(100):
        calls = [data for kind, data in harness.calls if kind is event]
        if calls:
            return calls[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有收到 {event.value}")


def _observation(*, status: str = "observed") -> dict[str, Any]:
    return {
        "step_id": "voice-1",
        "asr": ["豆腐，你更喜欢哪个？"],
        "replies": ["我喜欢第二个。"],
        "audio_chunks": 4,
        "expected_intent": "TO_ME",
        "actual_intent": None,
        "intent_status": "unavailable",
        "status": status,
    }


async def test_intent_catalog_renders_voice_steps_and_keeps_live_mock(
    intent_page: Page,
) -> None:
    page = intent_page
    assert await page.get_by_role("tab", name="简单意图测试").count() == 1
    assert await page.get_by_role("tab", name="困难多轮测试").count() == 1
    card = page.locator("[data-case-id='simple-1']")
    assert await card.get_by_role("button", name="自动运行", exact=True).count() == 1
    context = card.locator(".test-context")
    assert await context.get_attribute("open") is None
    await context.locator("summary").click()
    assert await context.get_by_text("主播正在比较两个工具。", exact=True).is_visible()
    assert "等待助手播报结束" in (await card.inner_text())
    assert "Excel 第 9 行" in (await card.inner_text())
    assert "观众甲 · 弹幕：这个工具免费吗？" in (await card.inner_text())
    assert "当前后端未提供" in (await card.inner_text())
    notice = await page.locator("#test-audio-notice").inner_text()
    assert all(
        word in notice
        for word in (
            "Seed TTS 2.0",
            "首次会将台词发送到火山语音服务",
            "复用本地缓存",
            "不是操作者的真实声音",
            "播出预制台词",
            "实时送入语音模型",
            "屏蔽麦克风",
            "断开",
            "Live Mock",
        )
    )
    assert "离线 TTS" not in notice
    await page.evaluate("window.bilisamaShell = {openLiveMock: () => {window.mockOpened = true;}}")
    await page.click("#live-mock-open")
    assert await page.evaluate("window.mockOpened === true")


async def test_preparing_locks_other_runs_and_stop_uses_existing_protocol(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    await page.locator("[data-case-id='simple-1'] .test-run").click()
    assert await _wait_call(harness, ClientEvent.TEST_RUN) == {"case_id": "simple-1"}
    assert await page.locator("[data-case-id='simple-2'] .test-run").is_disabled()
    await _state(page, harness, case_id="simple-1", run_id="run-a", status="preparing")
    assert "准备测试音频" in (await page.locator(".test-status").first.inner_text())
    assert await page.locator("[data-case-id='simple-1'] .test-case-stop").is_enabled()
    assert await page.locator("[data-case-id='simple-2'] .test-case-stop").is_disabled()
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-a",
        status="preparing",
        text="正在清空历史会话并加载本例背景",
    )
    await page.wait_for_function(
        "document.querySelector('.test-status')?.textContent.includes('清空历史会话')"
    )
    assert await page.locator("[data-case-id='simple-2'] .test-run").is_disabled()
    await page.click("#test-stop")
    assert await _wait_call(harness, ClientEvent.TEST_STOP) == {}
    await _state(page, harness, case_id="simple-1", run_id="run-a", status="stopped")
    assert await page.locator("[data-case-id='simple-2'] .test-run").is_enabled()
    assert await page.locator("[data-case-id='simple-1'] .test-pass").is_hidden()


async def test_each_case_has_stop_and_only_current_case_can_stop(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    current = page.locator("[data-case-id='simple-1']")
    other = page.locator("[data-case-id='simple-2']")
    assert await current.get_by_role("button", name="停止", exact=True).is_disabled()
    assert await other.get_by_role("button", name="停止", exact=True).is_disabled()
    await _state(page, harness, case_id="simple-1", run_id="run-a", status="running")
    assert await current.get_by_role("button", name="停止", exact=True).is_enabled()
    assert await other.get_by_role("button", name="停止", exact=True).is_disabled()
    await current.get_by_role("button", name="停止", exact=True).click()
    assert await _wait_call(harness, ClientEvent.TEST_STOP) == {}
    await _state(page, harness, case_id="simple-1", run_id="run-a", status="stopped")
    assert await current.get_by_role("button", name="停止", exact=True).is_disabled()
    await page.get_by_role("tab", name="困难多轮测试").click()
    assert await page.locator(".test-card").count() == await page.locator(".test-case-stop").count()


async def test_completed_is_not_a_pass_and_judgment_is_scoped_to_run(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    card = page.locator("[data-case-id='simple-1']")
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-a",
        status="completed",
        text="这是后台的完成文案，不能当作判定通过。",
        observations=[_observation()],
    )
    assert "执行完成，待人工判定" in (await card.locator(".test-status").inner_text())
    assert await card.get_attribute("data-judgment") is None
    observed = await card.locator(".test-observations").inner_text()
    assert all(text in observed for text in ("豆腐，你更喜欢哪个？", "我喜欢第二个。", "4"))
    assert "实际分类：当前后端未提供" in observed
    await card.locator(".test-pass").click()
    assert await card.get_attribute("data-judgment") == "pass"
    await card.locator(".test-run").click()
    assert await card.get_attribute("data-judgment") is None
    await _state(
        page, harness, case_id="simple-1", run_id="run-b", status="step", phase="observing"
    )
    assert await card.locator(".test-judge").is_hidden()
    assert "我喜欢第二个。" not in (await card.locator(".test-observations").inner_text())


async def test_other_case_and_tab_changes_keep_latest_observations(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-a",
        status="completed",
        observations=[_observation()],
    )
    await page.locator("[data-case-id='simple-1'] .test-fail").click()
    await _state(
        page, harness, case_id="simple-2", run_id="run-b", status="failed", text="音频准备失败"
    )
    await page.get_by_role("tab", name="困难多轮测试").click()
    await page.get_by_role("tab", name="简单意图测试").click()
    card = page.locator("[data-case-id='simple-1']")
    assert "我喜欢第二个。" in (await card.locator(".test-observations").inner_text())
    assert "执行完成，待人工判定" in (await card.locator(".test-status").inner_text())
    assert await card.get_attribute("data-judgment") == "fail"
    assert "音频准备失败" in (
        await page.locator("[data-case-id='simple-2'] .test-status").inner_text()
    )


async def test_incomplete_and_silence_do_not_become_intent_success(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    observed = {**_observation(status="timeout"), "asr": [], "replies": [], "audio_chunks": 0}
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-timeout",
        status="incomplete",
        text="等待助手开口超时，后续步骤未执行",
        observations=[observed],
    )
    card = page.locator("[data-case-id='simple-1']")
    assert await card.locator(".test-pass").is_disabled()
    assert "未收到回复" in (await card.locator(".test-observations").inner_text())
    assert "实际分类：当前后端未提供" in (await card.locator(".test-observations").inner_text())
    await card.locator(".test-incomplete").click()
    assert await card.get_attribute("data-judgment") == "incomplete"
    assert "本轮：未完成" in (await card.locator(".test-result").inner_text())


async def test_step_updates_show_generation_status_and_treat_text_as_text(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    observation = {
        **_observation(),
        "status": "observing",
        "replies": ["<img src=x onerror=alert(1)>"],
        "reply_details": [{"handle_id": 3, "status": "cancelled", "done": True, "audio_chunks": 4}],
    }
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-live",
        status="step",
        phase="observing",
        index=1,
        total=2,
        observations=[observation],
    )
    card = page.locator("[data-case-id='simple-1']")
    assert "观察回复 1/2" in (await card.locator(".test-status").inner_text())
    assert "已打断" in (await card.locator(".test-observations").inner_text())
    assert "<img src=x onerror=alert(1)>" in (await card.locator(".test-observations").inner_text())
    assert await card.locator(".test-observations img").count() == 0
    assert await card.locator(".test-judge").is_hidden()
    assert await page.locator("[data-case-id='simple-2'] .test-run").is_disabled()


async def test_failed_start_unlocks_ui_without_reusing_previous_pass(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    card = page.locator("[data-case-id='simple-1']")
    await _state(page, harness, case_id="simple-1", run_id=1, status="completed")
    await card.locator(".test-pass").click()
    await card.locator(".test-run").click()
    await _state(
        page,
        harness,
        case_id="simple-1",
        status="failed",
        text="请先断开真实直播间，再运行测试",
    )
    assert await card.locator(".test-run").is_enabled()
    assert await page.locator("#test-stop").is_disabled()
    assert await card.locator(".test-judge").is_hidden()
    assert await card.get_attribute("data-judgment") is None
    assert "请先断开真实直播间" in (await card.locator(".test-status").inner_text())


async def test_event_wait_and_unavailable_proactive_steps_are_visible(
    intent_page: Page, harness: Harness
) -> None:
    case = _case("edge-1")
    case["steps"] = [
        {
            "id": "gift-1",
            "kind": "event",
            "after": "reply_started",
            "delay_s": 0,
            "timeout_s": 12,
            "observe_s": 1,
            "event": {
                "kind": "gift",
                "viewer": {"name": "小禾"},
                "gift": {"name": "能量石", "num": 2, "unit_battery": 100},
            },
            "expected": "让主播先说完。",
            "source_row": 20,
        },
        {
            "id": "wait-1",
            "kind": "wait",
            "after": "delay",
            "observe_s": 5,
            "expected": "暂不插话。",
            "source_row": 21,
        },
        {
            "id": "ask-1",
            "kind": "proactive",
            "after": "delay",
            "observe_s": 5,
            "expected": "等待主播拒绝。",
            "source_row": 22,
        },
    ]
    harness.hello_override["tests"]["sets"][0]["cases"] = [case]
    page = intent_page
    await page.reload()
    await page.click("#corner")
    await page.click("[data-tab='tests']")
    card = page.locator("[data-case-id='edge-1']")
    text = await card.inner_text()
    assert "等待助手开始播报且仍在播报（最多等 12 秒）" in text
    assert "小禾 · 礼物：能量石 ×2 · 200 电池" in text
    assert "静默观察" in text
    assert "本步会标为未完成" in text


async def test_unattributed_records_are_visible_without_guessing_a_step(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-unattributed",
        status="completed",
        observations=[],
        unmatched_asr=["<img src=x onerror=alert(1)>还在吗？"],
        unattributed_replies=[
            {
                "handle_id": 7,
                "text": "<script>alert(1)</script>我在。",
                "audio_chunks": 2,
                "done": True,
                "status": "completed",
            }
        ],
    )
    card = page.locator("[data-case-id='simple-1']")
    records = card.locator(".test-unattributed")
    text = await records.inner_text()
    assert "未能关联到具体轮次的记录" in text
    assert "不推断回应对象或意图" in text
    assert "<img src=x onerror=alert(1)>还在吗？" in text
    assert "<script>alert(1)</script>我在。" in text
    assert "2 个音频分片" in text
    assert await records.locator("img, script").count() == 0
    assert await card.locator(".test-observation:not(.test-unattributed)").count() == 0
    await page.get_by_role("tab", name="困难多轮测试").click()
    await page.get_by_role("tab", name="简单意图测试").click()
    assert "还在吗？" in (await records.inner_text())
    await card.locator(".test-run").click()
    assert await card.locator(".test-unattributed").count() == 0


async def test_wait_target_and_unmatched_reply_warning_are_explicit(
    intent_page: Page, harness: Harness
) -> None:
    page = intent_page
    card = page.locator("[data-case-id='simple-1']")
    assert "等待Excel第9行引发的回复" in (await card.locator(".test-steps").inner_text())
    await _state(
        page,
        harness,
        case_id="simple-1",
        run_id="run-unknown",
        status="completed",
        observations=[{**_observation(), "asr": [], "replies": [], "audio_chunks": 0}],
        unmatched_asr=["这里有一条未匹配的识别结果"],
        unattributed_replies=[
            {"handle_id": 8, "text": "我听到了。", "audio_chunks": 1, "status": "cancelled"}
        ],
    )
    row = card.locator(".test-observation:not(.test-unattributed)")
    text = await row.inner_text()
    assert "本步未关联到回复；另有未归因记录，见下方" in text
    assert "本步未关联到识别回执；另有未匹配记录，见下方" in text
    assert "实际分类：当前后端未提供" in text
    assert "我听到了。" not in text
    assert "已打断" in (await card.locator(".test-unattributed").inner_text())
