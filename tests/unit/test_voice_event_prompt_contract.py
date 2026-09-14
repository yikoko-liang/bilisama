"""Prompt coverage contracts, not claims of semantic model accuracy."""

from pathlib import Path

import pytest

from bilisama.director.intents import entry_welcome_intent, intent_for
from bilisama.ingest.events import EventKind, Gift, GuardLevel, LiveEvent, Viewer

_LIVE = Path(__file__).resolve().parents[2] / "config" / "personas" / "live"
_OUTPUT = (
    "### 输出\n\n"
    "- 接话：直接说要说的话，开头不写任何标签，也不说明判断过程。"
    "不能执行的动作不假装完成；看不到画面时不能编造视觉细节。\n"
    "- 先听（{{username}} 在跟别人说话、在念东西、在自言自语，"
    "或者听不出在跟谁说）：以 `[SKIP]` 开头，后面用十个字以内写清这一轮 "
    "{{username}} 在做什么，然后结束。实在看不出在做什么，就只写 `[SKIP]`。\n\n"
    "可用的标签只有一个：\n{{sceneMarkers}}\n\n"
    "标签用半角方括号，必须是回复的第一个字符，整条输出只占一行，不接着回答，"
    "不加引号或代码块。历史里出现的 `[SKIP]` 和后面那句话，是 {{agentName}} "
    "当时的观察记录，不是她已经说出口的话。\n\n"
    "这一节只管 {{username}} 的语音。交给 {{agentName}} 的弹幕、礼物、进房，"
    "按事件规则回复。\n\n"
)


def _voice() -> str:
    return (_LIVE / "voice_addressing.md").read_text(encoding="utf-8")


def _event(kind: EventKind, guard: GuardLevel = GuardLevel.CAPTAIN) -> LiveEvent:
    return LiveEvent(
        kind=kind,
        event_id=f"prompt-contract:{kind.value}:{guard.value}",
        viewer=Viewer(uid=7, name="小松", guard_level=guard),
        text="本地任务会上传文件吗？",
        gift=Gift(name="鼓鼓掌", num=1, unit_battery=5) if kind is EventKind.GIFT else None,
    )


def _rules(kind: EventKind, guard: GuardLevel = GuardLevel.CAPTAIN) -> str:
    event = _event(kind, guard)
    intent = (
        entry_welcome_intent((event,), now=1)
        if kind is EventKind.ENTRY
        else intent_for(event, now=1)
    )
    assert intent is not None
    return intent.injection.reply.instructions or ""


def test_voice_output_section_is_byte_for_byte_unchanged() -> None:
    raw = (_LIVE / "voice_addressing.md").read_bytes()
    start = raw.index("### 输出\n".encode())
    end = raw.index("### 例子\n".encode())
    assert raw[start:end] == _OUTPUT.encode()


def test_voice_guidance_uses_shared_inputs_and_next_step_suitability() -> None:
    rules = _voice().split("### 输出", 1)[0]
    for clause in ("是否适合开口", "弹幕", "礼物", "上舰", "进房", "SC", "共享上下文"):
        assert clause in rules


@pytest.mark.parametrize(
    "clause",
    [
        "语义不完整",
        "自我吐槽",
        "计划或动作",
        "讲解尚未结束",
        "面向观众征集",
        "观察后续弹幕",
        "总结弹幕",
        "TODO",
        "实际具备",
        "下一次适合开口的意图",
    ],
)
def test_voice_guidance_covers_approved_intent_branches(clause: str) -> None:
    assert clause in _voice().split("### 输出", 1)[0]


def test_shared_prompt_rejects_topic_only_and_whole_user_completion() -> None:
    prompts = [_voice(), (_LIVE / "event_responses.md").read_text(encoding="utf-8")]
    for prompt in prompts:
        assert "话题相近" in prompt
        assert "同一用户" in prompt
        assert "只感谢支持不等于正文问题已答完" in prompt


@pytest.mark.parametrize("kind", [EventKind.ENTRY, EventKind.GUARD_BUY, EventKind.VIP_ENTER])
def test_welcome_events_do_not_skip_just_because_the_voice_floor_is_busy(
    kind: EventKind,
) -> None:
    rules = _rules(kind)
    assert "只影响播放时机" in rules
    assert (
        "没有明确的主播欢迎证据时" in rules
        or "未确认主播已欢迎时" in rules
        or "未有明确的主播欢迎证据时" in rules
    )
    assert "输出[SKIP]" in rules
    assert "事件优先级" in rules


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.DANMAKU,
        EventKind.GIFT,
        EventKind.GUARD_BUY,
        EventKind.SUPER_CHAT,
        EventKind.VIP_ENTER,
        EventKind.ENTRY,
    ],
)
def test_every_event_prompt_preserves_exact_interaction_and_silence_contract(
    kind: EventKind,
) -> None:
    rules = _rules(kind)
    for clause in ("话题相近", "同一用户", "只念问题", "附和", "静默", "[SKIP]"):
        assert clause in rules
    assert "report_interaction" not in rules
    assert "handled_event_ids" not in rules


def test_danmaku_instruction_keeps_speaker_question_answer_and_natural_handoff() -> None:
    for rules in (_rules(EventKind.DANMAKU), (_LIVE / "event_responses.md").read_text()):
        assert "观众昵称和具体问题" in rules
        assert "不机械套用" in rules
        assert "转述不能作为完整回复" in rules
        assert "已知部分" in rules
        assert "不把所有问题" in rules


def test_sc_instruction_keeps_unanswered_body_after_host_thanks() -> None:
    assert "只感谢支持不等于正文问题已答完" in _rules(EventKind.SUPER_CHAT)


@pytest.mark.parametrize("guard", [GuardLevel.CAPTAIN, GuardLevel.ADMIRAL, GuardLevel.GOVERNOR])
def test_vip_instruction_greets_before_background_and_remembers_interruption(
    guard: GuardLevel,
) -> None:
    rules = _rules(EventKind.VIP_ENTER, guard)
    for clause in ("先点名欢迎", "再简短", "省略", "不能只接上文", "原来那次进房"):
        assert clause in rules
    assert "最近三次进房回复" in rules
    if guard is GuardLevel.GOVERNOR:
        assert "最高一档" in rules
