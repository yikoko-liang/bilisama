"""Persona layer: fallback chain, growth budgets, prompt assembly order.

The one property that outranks the rest: no call in here, other than the
human-invoked promote(), may change an anchor file. The distiller-level twin
of that assertion (a full distill cycle leaves anchor bytes identical) lives
in test_distill.py; here it is pinned at the store level.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilisama.persona import growth as g
from bilisama.persona.loader import PersonaAnchors, PersonaStore
from bilisama.persona.prompt import (
    LIVE_RULES,
    DynamicContext,
    assemble,
    dynamic_tail,
    static_prefix,
)

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "personas" / "mia"


@pytest.fixture()
def store(tmp_path: Path) -> PersonaStore:
    return PersonaStore(tmp_path / "live", TEMPLATE_ROOT)


# ------------------------------------------------------------ fallback chain


def test_template_backs_an_empty_data_dir(store: PersonaStore) -> None:
    """Fresh install: no live copies exist, the shipped template answers."""
    anchors = store.anchors({"userName": "主播"})
    assert "米娅" in anchors.identity
    assert "{{userName}}" not in anchors.identity, "variables must be substituted"
    assert "性格" in anchors.personality


def test_live_copy_wins_over_the_template(store: PersonaStore, tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "identity.md").write_text("# 我是谁\n我是测试人设。", encoding="utf-8")

    anchors = store.anchors()
    assert "测试人设" in anchors.identity
    assert "性格" in anchors.personality, "the other anchor still falls back"


def test_a_blank_live_copy_falls_back_instead_of_erasing_the_persona(
    store: PersonaStore, tmp_path: Path
) -> None:
    """A streamer who empties a file to 'reset' it should get the template
    back, not an assistant with no identity."""
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "identity.md").write_text("   \n", encoding="utf-8")

    assert "米娅" in store.anchor("identity")


def test_missing_template_reports_the_path_in_chinese(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "live", tmp_path / "no-such-template")
    with pytest.raises(FileNotFoundError, match="人设文件缺失"):
        store.anchor("identity")


def test_unknown_variables_stay_visible(store: PersonaStore) -> None:
    """A typo'd {{name}} should read as a typo, not vanish."""
    text = store.anchor("identity", {"wrongName": "x"})
    assert "{{userName}}" in text


# ------------------------------------------------------------ growth files


def test_growth_roundtrip_and_tolerant_parse(store: PersonaStore) -> None:
    store.write_growth("voice", ["这把稳了", "蚌埠住了"])
    assert store.growth_entries("voice") == ["这把稳了", "蚌埠住了"]

    # Hand-edited file: headers, blanks and prose are dropped, bullets kept.
    store.growth_path("relationship").write_text(
        "# 共同经历\n\n随手写的一行注释\n- 观众给主播起了外号\n- 第二条\n",
        encoding="utf-8",
    )
    assert store.growth_entries("relationship") == ["观众给主播起了外号", "第二条"]


def test_growth_of_a_fresh_install_is_empty(store: PersonaStore) -> None:
    assert store.growth_entries("voice") == []
    assert store.growth_entries("relationship") == []


def test_store_reads_never_create_or_touch_anchor_files(
    store: PersonaStore, tmp_path: Path
) -> None:
    """Reading and growth writes must leave the anchors exactly as shipped."""
    before = {p: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")}

    store.anchors()
    store.write_growth("voice", ["一句口癖"])
    store.growth_entries("voice")

    assert {p: p.read_bytes() for p in TEMPLATE_ROOT.glob("*.md")} == before
    assert not (tmp_path / "live" / "identity.md").exists()
    assert not (tmp_path / "live" / "personality.md").exists()


# ------------------------------------------------------------ promotion


def test_promote_moves_a_line_into_the_live_personality(
    store: PersonaStore, tmp_path: Path
) -> None:
    store.write_growth("voice", ["这把稳了", "蚌埠住了"])
    store.promote("voice", "这把稳了")

    live = (tmp_path / "live" / "personality.md").read_text(encoding="utf-8")
    assert "长出来的性格" in live
    assert "- 这把稳了" in live
    assert store.growth_entries("voice") == ["蚌埠住了"], "promoted line leaves the growth file"

    template = (TEMPLATE_ROOT / "personality.md").read_text(encoding="utf-8")
    assert "这把稳了" not in template, "the shipped template stays pristine"


def test_promote_refuses_an_entry_that_is_not_there(store: PersonaStore) -> None:
    store.write_growth("voice", ["在的"])
    with pytest.raises(ValueError, match="没有这条"):
        store.promote("voice", "不在的")


# ------------------------------------------------------------ merge policy


def test_relationship_budget_drops_oldest_first() -> None:
    existing = [f"事{i}" for i in range(g.RELATIONSHIP_MAX_ENTRIES)]
    merged = g.merge_relationship(existing, ["新事"])
    assert len(merged) == g.RELATIONSHIP_MAX_ENTRIES
    assert merged[-1] == "新事"
    assert "事0" not in merged


def test_relationship_char_budget_holds() -> None:
    long = "长" * 300
    merged = g.merge_relationship([], [long, long, long])
    assert sum(len(e) for e in merged) <= g.RELATIONSHIP_MAX_CHARS


def test_voice_swap_rate_is_capped_per_call() -> None:
    """One stream may not replace the box — style creeps, it does not lurch."""
    merged = g.merge_voice(["旧1", "旧2"], ["新1", "新2", "新3", "新4"])
    assert merged == ["旧1", "旧2", "新1", "新2"]


def test_voice_dedupes_and_respects_line_budget() -> None:
    existing = [f"句{i}" for i in range(g.VOICE_MAX_LINES)]
    merged = g.merge_voice(existing, ["句3", "新句"])
    assert merged.count("句3") == 1
    assert len(merged) <= g.VOICE_MAX_LINES
    assert merged[-1] == "新句"


def test_merges_ignore_empty_strings() -> None:
    assert g.merge_relationship([], ["", "有货"]) == ["有货"]
    assert g.merge_voice([], ["", "有货"]) == ["有货"]


# ------------------------------------------------------------ prompt assembly


_ANCHORS = PersonaAnchors(identity="# 我是谁\n身份文本", personality="# 性格\n性格文本")


def test_static_prefix_order_is_identity_personality_rules() -> None:
    prefix = static_prefix(_ANCHORS)
    assert (
        prefix.index("身份文本") < prefix.index("性格文本") < prefix.index("直播规则")
    ), "cache-boundary order is the contract"


def test_static_prefix_is_byte_stable_across_calls() -> None:
    assert static_prefix(_ANCHORS) == static_prefix(_ANCHORS)


def test_live_rules_carry_all_three_memory_rules_and_the_speaker_lock() -> None:
    """Plan section 4.6: copy the whole block, not just the middle rule."""
    assert "不是主播说的话" in LIVE_RULES  # speaker-identity lock
    assert "不要复述" in LIVE_RULES  # rule 1: silent participation
    assert "察觉" in LIVE_RULES  # rule 2: never reveal memory
    assert "当前对话永远优先" in LIVE_RULES  # rule 3: conversation wins


def test_dynamic_tail_orders_slowest_changing_first() -> None:
    ctx = DynamicContext(
        voice_lines=("这把稳了",),
        relationship=("观众给主播起了外号",),
        pinned="今晚不聊工作",
        streamer_facts="主播在写编译器",
        session_progress="刚修完一个 bug",
        regulars="阿强（第 5 次来）",
        clock_line="开播 1 小时 47 分，现在 23:14，本周第 3 场",
    )
    tail = dynamic_tail(ctx)
    order = [
        tail.index("这把稳了"),
        tail.index("外号"),
        tail.index("今晚不聊工作"),
        tail.index("编译器"),
        tail.index("修完"),
        tail.index("阿强"),
        tail.index("23:14"),
    ]
    assert order == sorted(
        order
    ), "voice → relationship → pinned → facts → progress → regulars → clock"


def test_empty_segments_leave_no_headers_behind() -> None:
    tail = dynamic_tail(DynamicContext(clock_line="开播 5 分钟"))
    assert "共同经历" not in tail
    assert "置顶" not in tail
    assert tail.startswith("# 时间")

    assert assemble("前缀", DynamicContext()) == "前缀", "an all-empty tail adds nothing"


def test_assemble_puts_the_tail_after_the_prefix() -> None:
    text = assemble(static_prefix(_ANCHORS), DynamicContext(clock_line="开播 5 分钟"))
    assert text.index("直播规则") < text.index("开播 5 分钟")


# ------------------------------------------------------------ template variables


def test_template_variables_come_from_config() -> None:
    """The streamer's own name is the whole point of {{userName}}: with it set,
    the persona addresses a person instead of announcing 「主播」."""
    from bilisama.config.schema import PersonaConfig
    from bilisama.persona.loader import template_variables

    cfg = PersonaConfig.model_validate({"id": "hanako", "streamer_name": "阿强"})
    assert template_variables(cfg) == {"userName": "阿强", "agentName": "hanako"}

    named = PersonaConfig.model_validate({"id": "hanako", "display_name": "花子"})
    assert template_variables(named)["agentName"] == "花子", "display_name wins over the id"
    assert template_variables(named)["userName"] == "主播", "the neutral default still works"


@pytest.mark.parametrize("persona_id", ["mia", "hanako", "ming", "butter"])
def test_no_shipped_template_leaks_a_raw_placeholder(persona_id: str) -> None:
    """Every {{name}} any shipped persona uses must be one template_variables
    supplies. A missing key is silent: the raw {{agentName}} simply sits in the
    system prompt for the model to read out."""
    from bilisama.config.schema import PersonaConfig
    from bilisama.persona.loader import PersonaStore, template_variables

    cfg = PersonaConfig.model_validate({"id": persona_id})
    store = PersonaStore(Path("/nonexistent-live-dir"), TEMPLATE_ROOT.parent / persona_id)
    anchors = store.anchors(template_variables(cfg))
    assert "{{" not in anchors.identity, anchors.identity
    assert "{{" not in anchors.personality
    prompt = store.proactive_prompt(Path("/nonexistent-global.md"), template_variables(cfg))
    assert "{{" not in prompt
