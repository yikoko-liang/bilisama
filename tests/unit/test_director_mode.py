"""Director-mode plumbing and the ported openhanako personas.

The persona ports are checked as data: all four load through the store, the
variables substitute, and each hanako port carries its own proactive prompt
(the adapted yuan) while mia falls back to the global one.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from bilisama.cli import main
from bilisama.dev_talk import _Fanout, _parse_console_event
from bilisama.ingest.events import EventKind
from bilisama.persona.loader import PersonaStore
from bilisama.realtime import link

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
GLOBAL_PROACTIVE = CONFIG_DIR / "prompts" / "proactive.md"


# ------------------------------------------------------------ ported personas


@pytest.mark.parametrize("pid", ["mia", "hanako", "ming", "butter"])
def test_every_shipped_persona_loads_and_substitutes(tmp_path: Path, pid: str) -> None:
    store = PersonaStore(tmp_path / "live", CONFIG_DIR / "personas" / pid)
    anchors = store.anchors({"userName": "主播", "agentName": pid})
    assert anchors.identity.strip(), f"{pid} identity is empty"
    assert anchors.personality.strip(), f"{pid} personality is empty"
    assert "{{" not in anchors.identity, f"{pid} left a variable unsubstituted"


@pytest.mark.parametrize(
    ("pid", "marker"),
    [("hanako", "温度"), ("ming", "冷静"), ("butter", "温暖")],
)
def test_the_hanako_ports_keep_their_original_voices(tmp_path: Path, pid: str, marker: str) -> None:
    """Verbatim ports: the ishiki text arrived intact, not paraphrased."""
    store = PersonaStore(tmp_path / "live", CONFIG_DIR / "personas" / pid)
    assert marker in store.anchor("personality")
    assert "人格定义" in store.anchor("personality"), "the original heading survived"


@pytest.mark.parametrize(
    ("pid", "scaffold"),
    [("hanako", "MOOD"), ("ming", "沉思"), ("butter", "PULSE")],
)
def test_each_port_thinks_topics_in_its_own_scaffold(
    tmp_path: Path, pid: str, scaffold: str
) -> None:
    """The adapted yuan: per-persona proactive prompt wins over the global."""
    store = PersonaStore(tmp_path / "live", CONFIG_DIR / "personas" / pid)
    prompt = store.proactive_prompt(GLOBAL_PROACTIVE, {"agentName": pid, "userName": "主播"})
    assert scaffold in prompt
    assert "只输出那一句话本身" in prompt, "the output contract stays ours"
    assert "{{" not in prompt


def test_mia_falls_back_to_the_global_proactive_prompt(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "live", CONFIG_DIR / "personas" / "mia")
    prompt = store.proactive_prompt(GLOBAL_PROACTIVE)
    assert "主动话题" in prompt
    assert "MOOD" not in prompt


def test_a_streamer_copy_of_the_proactive_prompt_wins(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    (live / "proactive.md").write_text("我自己的话题提示词", encoding="utf-8")
    store = PersonaStore(live, CONFIG_DIR / "personas" / "hanako")
    assert store.proactive_prompt(GLOBAL_PROACTIVE) == "我自己的话题提示词"


# ------------------------------------------------------------ console events


def test_console_line_becomes_a_danmaku_from_the_test_viewer() -> None:
    event = _parse_console_event("你好呀", 1)
    assert event is not None
    assert event.kind is EventKind.DANMAKU
    assert event.viewer.name == "测试观众"
    assert event.text == "你好呀"


def test_console_named_danmaku_and_stable_identity() -> None:
    a = _parse_console_event("阿强:键盘不错", 1)
    b = _parse_console_event("阿强：又来了", 2)
    assert a is not None and b is not None
    assert a.viewer.name == "阿强" and b.viewer.name == "阿强"
    assert a.viewer.identity == b.viewer.identity, "same name, same memory row"
    assert a.event_id != b.event_id


def test_console_super_chat_and_gift_carry_value() -> None:
    sc = _parse_console_event("/sc 阿强 30 主播今天玩什么", 1)
    assert sc is not None
    assert sc.kind is EventKind.SUPER_CHAT
    assert sc.value_cny == 30.0
    assert sc.text == "主播今天玩什么"

    gift = _parse_console_event("/gift 老板 52", 2)
    assert gift is not None
    assert gift.kind is EventKind.GIFT
    assert gift.value_cny == 52.0
    assert gift.gift is not None and gift.gift.is_paid


@pytest.mark.parametrize("bad", ["", "/sc 阿强", "/gift 老板", "/sc 阿强 abc 话", "/unknown x"])
def test_console_garbage_returns_none_instead_of_a_broken_event(bad: str) -> None:
    assert _parse_console_event(bad, 1) is None


# ------------------------------------------------------------ fanout


class _StubLink:
    """Feeds a fixed set of events; records passthrough calls."""

    def __init__(self, events: list[link.LinkEvent]) -> None:
        self._events = events
        self.pushed: list[bytes] = []

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    async def set_context(self, instructions: str) -> None: ...

    async def push_audio(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    async def add_context_item(self, text: str, *, role: str = "user") -> None: ...

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        return link.ReplyHandle()

    async def cancel(self, handle: link.ReplyHandle) -> None: ...

    def events(self) -> object:
        async def gen() -> object:
            for event in self._events:
                yield event
            await asyncio.sleep(3600)  # stay open like a live socket

        return gen()


async def test_fanout_gives_every_consumer_every_event() -> None:
    events: list[link.LinkEvent] = [
        link.SpeechStarted(audio_ms=0),
        link.SpeechStopped(audio_ms=400),
    ]
    fan = _Fanout(_StubLink(events))  # type: ignore[arg-type]
    view_a, view_b = fan.events(), fan.events()
    fan.start()

    async def take(view: object, n: int) -> list[link.LinkEvent]:
        out = []
        async for event in view:  # type: ignore[attr-defined]
            out.append(event)
            if len(out) == n:
                break
        return out

    got_a, got_b = await asyncio.wait_for(
        asyncio.gather(take(view_a, 2), take(view_b, 2)), timeout=2.0
    )
    assert got_a == events
    assert got_b == events, "both consumers see the full stream — scheduler and playback"
    await fan.aclose()


async def test_fanout_passthrough_reaches_the_inner_link() -> None:
    stub = _StubLink([])
    fan = _Fanout(stub)  # type: ignore[arg-type]
    await fan.push_audio(b"\x00\x01")
    assert stub.pushed == [b"\x00\x01"]
    await fan.aclose()


# ------------------------------------------------------------ persona list CLI


def test_persona_list_shows_all_and_marks_the_active_one(tmp_path: Path) -> None:
    config = tmp_path / "bilisama.toml"
    config.write_text(
        '[speech.s2s]\nllm_model = "m"\n[persona]\nid = "hanako"\n'
        f'data_dir = "{tmp_path / "live"}"\n',
        encoding="utf-8",
    )
    # Point the config at the real shipped personas.
    import shutil

    shutil.copytree(CONFIG_DIR / "personas", tmp_path / "personas")

    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["persona", "list", "--config", str(config)]) == 0
    text = out.getvalue()
    assert "＊ hanako" in text
    for pid in ("mia", "ming", "butter"):
        assert pid in text
    assert "专属话题提示词" in text and "话题提示词用全局默认" in text
