"""Viewer-to-viewer addressing facts: a typed @, and who was @'d a moment ago.

No semantics here. The typed-@ rule is a weak fact (a convention, not
platform metadata — LiveEvent.reply_to_* stays platform-only), so it only
fires on the unambiguous shape: the danmaku OPENS with @someone who is
neither the host nor her, and the body never turns back to the host, her or
the room. The thread ledger only annotates; the model still judges.
"""

from __future__ import annotations

import pytest

from bilisama.clock import FakeClock
from bilisama.director.viewer_threads import ViewerThreads, leading_mention, viewer_chat_target
from bilisama.ingest.events import EventKind, LiveEvent, Viewer

HOST = ("AI代码侠土豆", "AI代码侠土豆")
HER = ("豆腐", "Doufu")


def _danmaku(name: str, text: str, *, uid: int = 1, **fields: object) -> LiveEvent:
    return LiveEvent(
        kind=EventKind.DANMAKU,
        event_id=f"t:{uid}:{text}",
        viewer=Viewer(uid=uid, name=name),
        text=text,
        **fields,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@白团 你那个按钮是不是也越修越歪？", ("白团", "你那个按钮是不是也越修越歪？")),
        ("＠白团，笑死", ("白团", "笑死")),
        ("  @白团：来了", ("白团", "来了")),
        ("@白团你那个按钮呢", None),
        ("@白团", None),
        ("白团 你那个按钮呢", None),
        ("@", None),
        ("@ 白团", None),
        ("看看 @白团 怎么说", None),
    ],
)
def test_a_leading_mention_is_read_off_the_head_only(
    text: str, expected: tuple[str, str] | None
) -> None:
    assert leading_mention(text) == expected


def test_a_typed_at_to_another_viewer_is_the_weak_fact() -> None:
    event = _danmaku("小路", "@白团 你那个按钮是不是也越修越歪？")
    assert viewer_chat_target(event, host_names=HOST, assistant_names=HER) == "白团"


@pytest.mark.parametrize(
    "text",
    [
        "@土豆 这个怎么弄",  # part of the host's name
        "@AI代码侠土豆 在吗",
        "@豆腐 你觉得呢",
        "@doufu 你觉得呢",  # case-insensitive ASCII
        "@白团 主播刚才说的对吧",  # turns back to the host
        "@白团 豆腐你评评理",  # turns to her
        "@白团 大家觉得呢",  # turns to the room
        "@白团你那个按钮呢",  # no separator: where the name ends is a guess
        "@土豆这个怎么弄",  # same shape, and this one IS for the host
        "你那个按钮是不是也越修越歪？",  # no @ at all
    ],
)
def test_the_weak_fact_stays_silent_on_every_ambiguous_shape(text: str) -> None:
    event = _danmaku("小路", text)
    assert viewer_chat_target(event, host_names=HOST, assistant_names=HER) is None


def test_the_platform_target_is_the_hard_fact() -> None:
    to_viewer = _danmaku(
        "小路", "你那个按钮呢", reply_to_uid=42, reply_to_name="白团", reply_to_anchor=False
    )
    assert viewer_chat_target(to_viewer, host_names=HOST, assistant_names=HER) == "白团"
    to_host = _danmaku(
        "小路", "你那个按钮呢", reply_to_uid=7, reply_to_name="土豆", reply_to_anchor=True
    )
    assert viewer_chat_target(to_host, host_names=HOST, assistant_names=HER) is None
    unknown = _danmaku(
        "小路", "@白团 你那个按钮呢", reply_to_uid=42, reply_to_name="白团", reply_to_anchor=None
    )
    assert (
        viewer_chat_target(unknown, host_names=HOST, assistant_names=HER) == "白团"
    ), "owner UID unknown: the typed @ still decides"
    turned = _danmaku(
        "小路", "主播这个怎么弄", reply_to_uid=42, reply_to_name="白团", reply_to_anchor=False
    )
    assert viewer_chat_target(turned, host_names=HOST, assistant_names=HER) is None


def test_the_anchor_never_counts_as_a_viewer_target() -> None:
    event = _danmaku("小路", "@主播 在吗")
    assert viewer_chat_target(event, host_names=("",), assistant_names=HER) is None


async def test_a_viewer_who_was_just_mentioned_is_annotated_when_they_write_back() -> None:
    clock = FakeClock()
    threads = ViewerThreads(clock, window_s=90.0)
    threads.note(_danmaku("小路", "@白团 你那个按钮是不是也越修越歪？", uid=1), target="白团")
    await clock.advance(6)
    reply = _danmaku("白团", "哈哈哈哈哈哈笑死，笨蛋deepseek", uid=2)
    assert threads.reply_context(reply) == ("小路", 6)
    assert threads.reply_context(_danmaku("路人", "笑死", uid=3)) is None
    assert (
        threads.reply_context(_danmaku("小路", "怎么不说话", uid=1)) is None
    ), "the one who @'d is not the one who was @'d"
    await clock.advance(90)
    assert threads.reply_context(reply) is None, "the window closed"


async def test_a_mention_of_the_host_or_her_opens_no_thread() -> None:
    clock = FakeClock()
    threads = ViewerThreads(clock)
    threads.note(_danmaku("小路", "@豆腐 你觉得呢", uid=1), target=None)
    assert threads.reply_context(_danmaku("豆腐", "…", uid=9)) is None
