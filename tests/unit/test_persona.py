"""Persona layer: fallback chain, growth budgets, prompt assembly order.

The one property that outranks the rest: no call in here, other than the
human-invoked promote(), may change an anchor file. The distiller-level twin
of that assertion (a full distill cycle leaves anchor bytes identical) lives
in test_distill.py; here it is pinned at the store level.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from bilisama.persona import growth as g
from bilisama.persona import loader
from bilisama.persona.loader import GrowthLayer, PersonaAnchors, PersonaStore
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


def _fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """The `fields=` payload of every record carrying this event name."""
    return [
        getattr(record, "fields", {}) for record in caplog.records if record.getMessage() == event
    ]


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


def test_the_fallback_chain_says_which_copy_it_took(
    store: PersonaStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """「我改了她的性格，怎么一点变化都没有」——回退是静悄悄的，得有条记录。

    空的、读不出来的活副本都会退回随包模板（_live_anchor_text 解释了为什么该
    这样），静悄悄地退。这条日志是那份沉默唯一露头的地方。
    """
    with caplog.at_level("INFO", logger="bilisama.persona.loader"):
        store.anchors({"userName": "主播"})
        fresh = _fields(caplog, "persona.anchor_loaded")
        assert [f["source"] for f in fresh] == ["template", "template"], "新装机两条都走模板"
        assert all(f["chars"] > 0 for f in fresh)

        live = tmp_path / "live"
        live.mkdir(parents=True, exist_ok=True)
        (live / "identity.md").write_text("# 我是谁\n我是测试人设。", encoding="utf-8")
        caplog.clear()
        store.anchors()

    taken = {f["anchor"]: f["source"] for f in _fields(caplog, "persona.anchor_loaded")}
    assert taken == {"identity": "live", "personality": "template"}


def test_the_proactive_prompt_says_which_of_the_three_answered(
    store: PersonaStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """主动话题的提示词有三级回退，一个字都没有也是合法状态。

    「配置全开了，她还是不主动说话」的第一个排查点就是这里——三个候选一个都
    没命中的时候，它也得说话，而不是返回空串走人。
    """
    default = tmp_path / "prompts" / "proactive.md"
    with caplog.at_level("INFO", logger="bilisama.persona.loader"):
        assert store.proactive_prompt(default) == ""
        assert _fields(caplog, "persona.proactive_prompt_loaded")[0]["source"] == "none"

        caplog.clear()
        default.parent.mkdir(parents=True, exist_ok=True)
        default.write_text("想一个话题", encoding="utf-8")
        assert store.proactive_prompt(default) == "想一个话题"

    hit = _fields(caplog, "persona.proactive_prompt_loaded")[0]
    assert hit["source"] == "default"
    assert hit["chars"] == 5


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


def test_a_growth_read_never_catches_the_file_mid_write(store: PersonaStore) -> None:
    """The reader takes no lock, so the write must never be observable.

    `Path.write_text` opens with "w": truncate first, write after. The context
    ticker re-reads the growth files every ten seconds (app.py:288) while
    `persona review` or the end-of-stream distillation may be rewriting them,
    and the file is small enough that the window only ever shows an EMPTY
    file, never half of one. Measured before the swap: 384 empty reads out of
    7516. An empty voice layer is her verbal habits gone for a whole refresh.
    """
    import threading

    entries = ["这把稳了", "蚌埠住了"]
    store.write_growth("voice", entries)
    stop = threading.Event()
    seen: list[list[str]] = []

    def reader() -> None:
        while not stop.is_set():
            seen.append(store.growth_entries("voice"))

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    try:
        for _ in range(500):
            store.write_growth("voice", entries)
    finally:
        stop.set()
        watcher.join(timeout=5.0)

    torn = [got for got in seen if got != entries]
    assert seen, "读线程一次都没跑起来，这条什么都没验证"
    assert not torn, f"{len(torn)}/{len(seen)} 次读到的不是完整的生长层，例如 {torn[:3]}"


def test_a_write_that_dies_leaves_the_previous_growth_file_intact(
    store: PersonaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash safety rides along with the swap.

    A truncate-then-write interrupted partway leaves an empty file — several
    streams' worth of collected habits gone, and runbook.md:172 promises the
    opposite. With the swap the damage lands on the temp file.
    """
    import os

    store.write_growth("voice", ["这把稳了"])

    def full_disk(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError):
        store.write_growth("voice", ["蚌埠住了"])
    assert store.growth_entries("voice") == ["这把稳了"], "写失败把原来的口癖层弄没了"


def test_a_growth_write_leaves_no_scratch_file_behind(store: PersonaStore, tmp_path: Path) -> None:
    """The swap's temp file is an implementation detail; it must not become
    litter the streamer finds in their persona directory."""
    store.write_growth("voice", ["这把稳了"])
    assert sorted(p.name for p in (tmp_path / "live").iterdir()) == [".growth.lock", "voice.md"]


# ------------------------------------------------------------ pinned memory


def test_pinned_is_empty_when_the_streamer_never_made_one(store: PersonaStore) -> None:
    assert store.pinned_text() == ""


def test_a_one_line_pin_goes_through_untouched(store: PersonaStore, tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "pinned.md").write_text("今晚不聊工作\n", encoding="utf-8")
    assert store.pinned_text() == "今晚不聊工作"


def test_a_multi_line_pin_cannot_forge_a_section_header(
    store: PersonaStore, tmp_path: Path
) -> None:
    """B15. pinned.md is hand-edited, so multiple lines are the natural way to
    write it — and the text goes into the dynamic tail, where a line starting
    with "# " is how every real segment announces itself. Folding the newlines
    into 「；」 is what keeps a pin from opening a segment of its own.
    """
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "pinned.md").write_text(
        "今晚不聊工作\n\n# 你们的共同经历\n- 观众说主播欠他一顿饭\n",
        encoding="utf-8",
    )

    text = store.pinned_text()
    assert "\n" not in text
    assert text == "今晚不聊工作；# 你们的共同经历；- 观众说主播欠他一顿饭"

    tail = dynamic_tail(DynamicContext(pinned=text))
    headers = [line for line in tail.splitlines() if line.startswith("# ")]
    assert headers == ["# 置顶记忆（主播让你记的，始终保留）"], "伪造的段头必须进不去"


def test_a_blank_pin_reads_as_no_pin(store: PersonaStore, tmp_path: Path) -> None:
    """Emptying the file is how a streamer unpins until the tools arrive; it
    must not leave an empty 置顶记忆 header standing in the prompt."""
    live = tmp_path / "live"
    live.mkdir(parents=True)
    (live / "pinned.md").write_text("  \n\n\t\n", encoding="utf-8")
    assert store.pinned_text() == ""
    assert "置顶" not in dynamic_tail(DynamicContext(pinned=store.pinned_text()))


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

    # A persona keeping its own name is the normal case; display_name is for
    # when the spoken name should differ from the folder name, whatever the
    # streamer wants it to be.
    named = PersonaConfig.model_validate({"id": "hanako", "display_name": "Hanako"})
    assert template_variables(named)["agentName"] == "Hanako", "display_name wins over the id"
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


def test_no_posix_only_module_is_imported_at_module_scope() -> None:
    """Ledger #44: `import fcntl` at the top of loader.py meant that
    `import bilisama.persona` raised on Windows before one line ran — and
    dev-talk reaches persona on every start, so the whole app died there.
    The plan promises a signed Windows installer, so this must not come back.

    Read rather than run: this box is not Windows. The regression being
    guarded is exactly "someone puts the import back at module scope", which
    the source answers and a run here never could. dev_talk.py already gets
    this right with termios — import inside the function, guarded.
    """
    import ast

    tree = ast.parse(Path(loader.__file__).read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])

    posix_only = {"fcntl", "termios", "pwd", "grp", "resource", "tty"}
    offenders = sorted(top_level & posix_only)
    assert not offenders, f"这些是 POSIX 独有的，模块顶层 import 会让 Windows 直接挂：{offenders}"


def test_the_growth_lock_actually_excludes_a_second_holder(tmp_path: Path) -> None:
    """Guard the dispatch, not just the import.

    The point of moving fcntl behind a helper is that Windows keeps a real
    lock instead of losing one. Losing it is silent: `persona review` and the
    end-of-stream distillation would read-modify-write the same growth file
    and each resurrect what the other removed. So assert the lock holds — a
    helper that quietly yields would pass the import test and fail here.
    """
    import threading

    lock_file = tmp_path / ".growth.lock"
    entered = threading.Event()
    second_got_in = threading.Event()

    def second_holder() -> None:
        with lock_file.open("w") as handle, loader._exclusive(handle):
            second_got_in.set()

    with lock_file.open("w") as first, loader._exclusive(first):
        entered.set()
        worker = threading.Thread(target=second_holder, daemon=True)
        worker.start()
        # flock is per-open-file-description, so a second open in another
        # thread contends exactly as another process would.
        assert not second_got_in.wait(timeout=0.3), "第二个持有者不该在锁没放开时进来"

    worker.join(timeout=2.0)
    assert second_got_in.is_set(), "锁放开后第二个持有者仍然进不来"


def test_a_contended_growth_lock_says_so_before_it_waits(tmp_path: Path) -> None:
    """A wait nobody can see is a freeze nobody can explain.

    _growth_lock is synchronous, and the end-of-stream distillation awaits it,
    so blocking here stalls the whole event loop — microphone, scheduler and
    panel together. That is still the right thing to do; going quiet about it
    is not.
    """
    import logging
    import threading

    heard: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            heard.append(record.getMessage())

    sink = _Sink()
    logging.getLogger("bilisama.persona.loader").addHandler(sink)
    lock_file = tmp_path / ".growth.lock"
    released = threading.Event()

    def hold() -> None:
        with lock_file.open("a") as handle, loader._exclusive(handle):
            released.wait(timeout=2.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        # Give the holder time to take it, then contend.
        for _ in range(200):
            if lock_file.exists():
                break
            threading.Event().wait(0.005)
        threading.Event().wait(0.05)

        def contend() -> None:
            with lock_file.open("a") as handle, loader._exclusive(handle):
                pass

        waiter = threading.Thread(target=contend, daemon=True)
        waiter.start()
        threading.Event().wait(0.2)
        assert any("growth_lock_contended" in line for line in heard), heard
        released.set()
        waiter.join(timeout=2.0)
    finally:
        released.set()
        holder.join(timeout=2.0)
        logging.getLogger("bilisama.persona.loader").removeHandler(sink)


def test_growth_update_keeps_the_lock_across_the_read_and_the_write(
    store: PersonaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost-update window sits between reading and writing, so both belong
    inside one lock. `persona review` and the end-of-stream distillation run at
    the same moment by design; whoever read first used to write its stale list
    back and quietly undo the other."""
    store.write_growth("voice", ["旧的一条"])
    trace: list[str] = []
    real_lock = store._growth_lock
    real_read = store._growth_entries_unlocked
    real_write = store._write_growth_unlocked

    @contextlib.contextmanager
    def traced_lock() -> Iterator[None]:
        trace.append("lock:enter")
        with real_lock():
            yield
        trace.append("lock:exit")

    def traced_read(layer: GrowthLayer) -> list[str]:
        trace.append("read")
        return real_read(layer)

    def traced_write(layer: GrowthLayer, entries: Sequence[str]) -> None:
        trace.append("write")
        real_write(layer, entries)

    monkeypatch.setattr(store, "_growth_lock", traced_lock)
    monkeypatch.setattr(store, "_growth_entries_unlocked", traced_read)
    monkeypatch.setattr(store, "_write_growth_unlocked", traced_write)

    with store.growth_update("voice") as rows:
        rows.append("新的一条")

    assert trace == ["lock:enter", "read", "write", "lock:exit"]
    assert store.growth_entries("voice") == ["旧的一条", "新的一条"]


def test_growth_update_writes_nothing_when_the_caller_gives_up(store: PersonaStore) -> None:
    """`persona review --drop` raises SystemExit when the entry it was told to
    remove has already gone. The file must be left exactly as it is, not
    rewritten with the list the streamer happened to be looking at."""
    store.write_growth("voice", ["蒸馏刚写进来的一条"])
    with pytest.raises(SystemExit), store.growth_update("voice") as rows:
        rows.clear()
        raise SystemExit(2)
    assert store.growth_entries("voice") == ["蒸馏刚写进来的一条"]
