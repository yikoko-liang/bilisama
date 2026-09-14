"""Export shipped interaction prompts without accessing live persona or memory.

Generated documentation deliberately retains template variables. Source AST
extraction covers inline runtime prompts without creating a service or model.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

from bilisama.clock import FakeClock
from bilisama.config.enums import Chattiness
from bilisama.config.schema import InteractionConfig, PersonaConfig
from bilisama.director.intents import (
    EVENT_DECISION_RULES,
    _candidate_focus,
    _event_ref,
    anchor_danmaku_context_item,
    burst_welcome_intent,
    entry_welcome_intent,
    gift_combo_intent,
    intent_for,
    observed_events_context_item,
)
from bilisama.director.interaction_state import REPORT_RULES, InteractionState, report_tool_spec
from bilisama.ingest.bilibili.safety import aggregate_gift_events
from bilisama.ingest.events import EventKind, Gift, GuardLevel, LiveEvent, Viewer
from bilisama.persona.loader import scene_marker_lines, template_variables
from bilisama.persona.prompt import LIVE_RULES
from bilisama.proactive_opportunities import ProactiveOpportunities

_ROOT = Path(__file__).resolve().parents[1]
_PROACTIVE = "src/bilisama/proactive.py"
_GIFT_SAFETY = "src/bilisama/ingest/bilibili/safety.py"
_SOURCE_FILES = (
    "config/personas/tofu/identity.md",
    "config/personas/tofu/personality.md",
    "config/personas/live/voice_responses.md",
    "config/personas/live/voice_addressing.md",
    "config/personas/live/event_responses.md",
    "config/prompts/proactive.md",
    "src/bilisama/persona/prompt.py",
    "src/bilisama/persona/loader.py",
    "src/bilisama/director/intents.py",
    "src/bilisama/director/interaction_state.py",
    "src/bilisama/proactive_opportunities.py",
    _GIFT_SAFETY,
    _PROACTIVE,
)
_VARIABLES = {
    "unanswered": "unanswered_count",
    "self._unanswered_count": "unanswered_count",
    "self._assistant_label": "agentName",
    "summary or '（刚开播，还没有进展）'": "session_progress_or_empty",
    "chr(10).join(dialogue) or '（还没有）'": "recent_dialogue_or_empty",
    "chr(10).join(events) or '（还没有）'": "recent_events_or_empty",
    "opportunities or '（还没有）'": "opportunities_or_empty",
}
_PROMPT_CONSTANTS = {"EVENT_DECISION_RULES": ""}


def _template(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                expression = ast.unparse(value.value)
                parts.append("{{" + _VARIABLES.get(expression, expression) + "}}")
            else:
                parts.append(_template(value))
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _template(node.left) + _template(node.right)
    if isinstance(node, ast.Name) and node.id in _PROMPT_CONSTANTS:
        # Common prefixes are exported in their own block; omit them from a
        # per-intent snapshot rather than duplicating the full text.
        return _PROMPT_CONSTANTS[node.id]
    raise ValueError(f"无法安全导出 Prompt 表达式：{ast.unparse(node)}")


def _function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise ValueError(f"Prompt 来源函数不唯一：{path}:{name}")
    return matches[0]


def _keyword(path: str, function: str, keyword: str) -> str:
    matches = [
        node.value
        for node in ast.walk(_function(path, function))
        if isinstance(node, ast.keyword) and node.arg == keyword
    ]
    if len(matches) != 1:
        raise ValueError(f"Prompt 参数不唯一：{path}:{function}:{keyword}")
    return _template(matches[0])


def _assignments(path: str, function: str, variable: str) -> Iterator[str]:
    for node in ast.walk(_function(path, function)):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == variable for target in node.targets
        ):
            yield _template(node.value)


def _block(title: str, source: str, text: str, *, language: str = "text") -> str:
    # A four-backtick fence preserves any three-backtick examples verbatim.
    suffix = "" if text.endswith("\n") else "\n"
    return f"### {title}\n\n来源：`{source}`。\n\n````{language}\n{text}{suffix}````\n\n"


def _public_event(
    kind: EventKind,
    *,
    guard: GuardLevel = GuardLevel.NONE,
    batteries: int = 1,
    anchor: bool = False,
    suffix: str = "",
) -> LiveEvent:
    return LiveEvent(
        kind=kind,
        viewer=Viewer(name="{{viewer_name}}", guard_level=guard, is_anchor=anchor),
        event_id=f"public-prompt:{kind.value}:{guard.value}:{batteries}:{anchor}:{suffix}",
        text="{{event_text}}",
        gift=Gift(name="{{gift_name}}", unit_battery=batteries) if kind is EventKind.GIFT else None,
    )


def _variables(text: str, events: tuple[LiveEvent, ...]) -> str:
    for index, event in enumerate(events, 1):
        text = text.replace(_event_ref(event), "{{event_ref_" + str(index) + "}}")
    return text.replace("时间戳 0", "时间戳 {{ts_ms}}").replace("UID 0", "UID {{uid}}")


def _event_sections() -> str:
    tiers = InteractionConfig()
    variants: list[tuple[str, LiveEvent]] = [
        ("弹幕（单条与多条候选使用相同要求）", _public_event(EventKind.DANMAKU)),
        ("SC", _public_event(EventKind.SUPER_CHAT)),
        ("普通礼物", _public_event(EventKind.GIFT)),
        ("中额礼物", _public_event(EventKind.GIFT, batteries=tiers.gift_battery_medium)),
        ("高额礼物", _public_event(EventKind.GIFT, batteries=tiers.gift_battery_high)),
    ]
    for kind, prefix in ((EventKind.GUARD_BUY, "上舰"), (EventKind.VIP_ENTER, "VIP 进房")):
        for guard, label in (
            (GuardLevel.CAPTAIN, "舰长"),
            (GuardLevel.ADMIRAL, "提督"),
            (GuardLevel.GOVERNOR, "总督"),
        ):
            variants.append((f"{prefix}：{label}", _public_event(kind, guard=guard)))
    variants.append(("VIP 进房：高等级本房粉丝牌", _public_event(EventKind.VIP_ENTER)))
    result = ""
    for title, event in variants:
        intent = intent_for(event, now=0)
        if intent is None or intent.injection.reply.instructions is None:
            raise ValueError(f"事件 Prompt 构建失败：{title}")
        rules = intent.injection.reply.instructions.removeprefix(EVENT_DECISION_RULES)
        result += _block(title, "src/bilisama/director/intents.py", _variables(rules, (event,)))
    entry = _public_event(EventKind.ENTRY)
    for title, events in (
        ("普通进房：单人", (entry,)),
        ("普通进房：多人合并", (entry, _public_event(EventKind.ENTRY, suffix="second"))),
    ):
        intent = entry_welcome_intent(events, now=0)
        rules = (intent.injection.reply.instructions or "").removeprefix(EVENT_DECISION_RULES)
        result += _block(title, "src/bilisama/director/intents.py", _variables(rules, events))
    burst = burst_welcome_intent(2, now=0)
    result += _block(
        "旧版人数聚合欢迎入口（保留分支）",
        "src/bilisama/director/intents.py:burst_welcome_intent",
        (burst.injection.reply.instructions or "").removeprefix(EVENT_DECISION_RULES),
    )
    return result


def _observation_sections() -> str:
    audience = _public_event(EventKind.DANMAKU)
    host = _public_event(EventKind.DANMAKU, anchor=True)
    clock = FakeClock()
    ledger = InteractionState(clock)
    ledger.observe(audience)
    ledger.observe(host)
    sections = (
        (
            "近期直播事件观察",
            "observed_events_context_item",
            observed_events_context_item((audience,)),
        ),
        ("主播本人打字观察", "anchor_danmaku_context_item", anchor_danmaku_context_item(host)),
        ("共享事件状态数据（初始 pending 示例）", "InteractionState.context", ledger.context()),
    )
    return "".join(
        _block(title, source, _variables(body, (audience, host)))
        for title, source, body in sections
    )


def _gift_combo_sections() -> str:
    first = _public_event(EventKind.GIFT, suffix="first-hit")
    second = _public_event(EventKind.GIFT, suffix="second-hit")
    prefix = dataclasses.replace(
        first,
        event_id="gift-combo-prefix:public-template",
        text=_keyword(_GIFT_SAFETY, "contributions", "text"),
    )
    result = (
        "礼物聚合复用上面的三档感谢正文；聚合不表示又发生一次送礼。"
        "以下只展开新增的成员事实和候选范围，报告要对应成员记录，不能拿展示合计替代原始身份。"
        "带压缩前缀的输入会明确说明逐笔明细不完整，不能从某一笔已答谢推断整个合计已处理。\n\n"
    )
    for title, members in (
        ("礼物连击聚合：原始成员事实", (first, second)),
        ("礼物连击聚合：含压缩前缀的事实", (prefix, second)),
    ):
        aggregate = aggregate_gift_events(members)
        combo = gift_combo_intent(aggregate, members, now=0)
        single = intent_for(aggregate, now=0)
        if single is None or single.injection.reply.instructions is None:
            raise ValueError("礼物聚合的基础 Prompt 缺失")
        base = single.injection.reply.instructions.removesuffix(_candidate_focus((aggregate,)))
        instructions = combo.injection.reply.instructions or ""
        if not instructions.startswith(base):
            raise ValueError("礼物聚合正文已变化，请更新导出范围，不能只导出候选成员")
        result += _block(
            title,
            "src/bilisama/director/intents.py:gift_combo_item_text",
            _variables(combo.injection.item_text or "", members),
        )
        result += _block(
            title + "对应的候选范围",
            "src/bilisama/director/intents.py:gift_combo_intent",
            _variables(instructions.removeprefix(base), members),
        )
    return result


def _opportunity_sections() -> str:
    clock = FakeClock()
    opportunities = ProactiveOpportunities(clock)
    event = _public_event(EventKind.DANMAKU)
    opportunities.note_event(event)
    opportunities.note_interrupted(
        "public-interrupted", "{{source}}", "{{background}}", "{{partial_text}}"
    )
    opportunities.discard_events({"{{handled_or_revoked_ref}}"})
    result = _block(
        "有效讨论与中断候选数据模板",
        "src/bilisama/proactive_opportunities.py:material",
        _variables(opportunities.material(), (event,)),
    )
    opportunities.collect_opinions(
        "{{discussion_topic}}", started_at=-opportunities.collection_window_s
    )
    summary = opportunities.due_collection()
    if summary is None:
        raise ValueError("观点征集 Prompt 构建失败")
    result += _block(
        "观点征集输入模板（此处的记录只用于展示格式）",
        "src/bilisama/proactive_opportunities.py:due_collection",
        _variables(summary.item_text, (event,)),
    )
    return result


def build_snapshot() -> str:
    document = (
        "# 当前互动 Prompt（公开模板快照）\n\n"
        "由 `tools/export_interaction_prompts.py` 从仓库自带模板和生产构建函数导出。"
        "这里只展示豆腐公共人设和互动规则；不读取本机人设成长、私人记忆、直播间设置或密钥，"
        "也不调用模型。运行时已有的共享上下文继续保留，不在这份公开快照中展开。\n\n"
        "这是当前实现的文字快照，不代表真实模型已经准确遵循所有规则。"
        "程序回归与真实语义效果请分别查看本轮联动验收文档。\n\n"
        "## 1. 变量与拼接方式\n\n"
        "`{{agentName}}` 在豆腐人设下填豆腐；`{{username}}` 与 `{{userName}}` 都指当前主播，"
        "昵称为空时使用主播。`{{replyLength}}` 根据当次配置动态选择，以下三个档位互斥，"
        "不会把短档同时注入中档或长档。事件编号、事件正文和运行时间也都保留占位符。\n\n"
        "语音和事件使用同一公共人设与共享历史，分别追加语音或事件回合规则。"
        "直播事件再追加共同判断规则和对应的本次事件要求；本文将重复的共同部分只列一次。"
        "启用后台报告时，REPORT_RULES 放在语音或事件回合规则之后，"
        "不改写语音规则原有的输出块。"
        "同一 Realtime 生成维持原正文或 `[SKIP]`；仅在需要改变后台状态时使用独立函数报告，"
        "无变化不要求空报告或全 keep。程序不凭正文推断持续静默已经解除。"
        "不增加一次意图判定模型调用。最后的函数参数不是口播内容。\n\n"
    )
    for length in Chattiness:
        wording = template_variables(PersonaConfig(), reply_length=length)["replyLength"]
        document += _block(
            f"reply_length={length.value}", "src/bilisama/persona/loader.py", wording
        )
    document += "## 2. 豆腐公共人设\n\n"
    for relative in _SOURCE_FILES[:2]:
        document += _block(
            Path(relative).name, relative, (_ROOT / relative).read_text(encoding="utf-8")
        )
    document += _block("公共直播规则", "src/bilisama/persona/prompt.py:LIVE_RULES", LIVE_RULES)
    document += "## 3. 语音回合规则与意图判断\n\n"
    for relative in _SOURCE_FILES[2:4]:
        document += _block(
            Path(relative).name, relative, (_ROOT / relative).read_text(encoding="utf-8")
        )
    document += _block(
        "sceneMarkers 的运行时展开", "src/bilisama/persona/loader.py", scene_marker_lines()
    )
    document += "## 4. 直播事件通用规则\n\n"
    path = _SOURCE_FILES[4]
    document += _block("事件回合规则", path, (_ROOT / path).read_text(encoding="utf-8"))
    document += _block(
        "每次事件的共同判断前缀", "src/bilisama/director/intents.py", EVENT_DECISION_RULES
    )
    document += "## 5. 各类事件的本次要求\n\n" + _event_sections() + _gift_combo_sections()
    document += "## 6. 观察和状态输入（数据，不是新的指令）\n\n" + _observation_sections()
    document += "## 7. 主动话题与新增契机\n\n"
    path = _SOURCE_FILES[5]
    document += _block("原有后台候选生成规则", path, (_ROOT / path).read_text(encoding="utf-8"))
    document += _block(
        "原有后台候选的动态输入", _PROACTIVE, _keyword(_PROACTIVE, "_refresh", "user")
    )
    document += _block(
        "Realtime 主动开口要求", _PROACTIVE, _keyword(_PROACTIVE, "_speak", "instructions")
    )
    for index, material in enumerate(_assignments(_PROACTIVE, "_speak", "topic_material"), 1):
        document += _block(f"topic_material 分支 {index}", _PROACTIVE, material)
    document += _block(
        "观点征集到期总结要求（同一次 Realtime 生成）",
        _PROACTIVE,
        _keyword(_PROACTIVE, "_submit_opinion_summary", "instructions"),
    )
    document += _block(
        "主播委托的热议弹幕总结要求",
        _PROACTIVE,
        _keyword(_PROACTIVE, "_danmaku_summary_intent", "instructions"),
    )
    document += _opportunity_sections()
    document += "## 8. 不播报的互动状态报告\n\n"
    document += _block(
        "REPORT_RULES（原样）", "src/bilisama/director/interaction_state.py", REPORT_RULES
    )
    tool = report_tool_spec()
    document += _block(
        "函数声明与参数 schema",
        "src/bilisama/director/interaction_state.py:report_tool_spec",
        json.dumps(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
            ensure_ascii=False,
            indent=2,
        ),
        language="json",
    )
    document += (
        "## 9. 快照来源校验\n\n以下 SHA-256 用于确认快照对应哪版源码，不包含运行时数据。\n\n"
    )
    document += "| 来源 | SHA-256 |\n|---|---|\n"
    for source in _SOURCE_FILES:
        digest = hashlib.sha256((_ROOT / source).read_bytes()).hexdigest()
        document += f"| `{source}` | `{digest}` |\n"
    return document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="导出仓库自带的公开互动 Prompt，不读取本机私人上下文"
    )
    parser.add_argument(
        "--output", type=Path, default=_ROOT / "docs/current-interaction-prompts.md"
    )
    parser.add_argument("--check", action="store_true", help="只检查快照是否匹配当前源码，不写文件")
    args = parser.parse_args()
    output: Path = args.output
    content = build_snapshot()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != content:
            print("公开 Prompt 快照缺失或已过期，请重新运行导出命令。")
            return 1
        print("公开 Prompt 快照与当前源码一致。")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"已导出公开 Prompt：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
