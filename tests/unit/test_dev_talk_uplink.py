"""dev-talk's own seams: the model join, the task watchdog, connect advice, and
the two wiring points that decide what happens to the microphone.

Four of the five things pinned here are plain functions and are tested by
calling them. The fifth kind — how `run_director` wires the broker, the tally
and the teardown — cannot be: reaching those lines means a live provider, a
UI server and a real PortAudio device, and the last one is exactly what must
not be opened from a test run. So they are read instead, the way
tests/unit/test_dependency_direction.py and tests/unit/test_s2s_shim_structure.py
read their subjects, and every source-level check is also run over a planted
violation — a gate whose teeth are never exercised is a gate nobody can trust.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import io
import json
import logging
import shutil
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from bilisama import dev_talk
from bilisama.clock import FakeClock
from bilisama.config.enums import ProviderName
from bilisama.obs.health import LinkHealth
from bilisama.obs.logging import setup
from bilisama.realtime import link
from bilisama.realtime.providers import s2s as s2s_module
from bilisama.realtime.providers import with_model

_DEV_TALK_PY = Path(dev_talk.__file__)

# ------------------------------------------------------------- with_model


def test_a_model_is_appended_when_the_address_names_none() -> None:
    """The plain case, unchanged: config gives a bare address, the resolved
    model gets stapled on."""
    joined = with_model("wss://host/api-ws/v1/realtime", "qwen-flash")
    assert joined == "wss://host/api-ws/v1/realtime?model=qwen-flash"


def test_an_address_that_already_names_a_model_keeps_it() -> None:
    """Nobody on the command line said otherwise, so the address wins — it is
    the more specific of the two, and the resolved model here is only the
    registry default (realtime/providers/__init__.py:117-119)."""
    url = "wss://dashscope.example/api-ws/v1/realtime?model=qwen-omni-turbo-realtime"
    assert with_model(url, "qwen-audio-3.0-realtime-flash") == url


def test_the_command_line_model_beats_the_one_written_into_the_address() -> None:
    """The bug: `--model` ranks first in resolve_endpoint
    (realtime/providers/__init__.py:117-119) and then lost here, because an
    address carrying `?model=` was treated as finished. DashScope's own
    address shape carries one, so the flag was a no-op on the most common
    config there is.
    """
    url = "wss://dashscope.example/api-ws/v1/realtime?model=qwen-omni-turbo-realtime"
    joined = with_model(url, "qwen-audio-3.0-realtime-flash", explicit=True)
    assert "model=qwen-audio-3.0-realtime-flash" in joined
    assert "qwen-omni-turbo-realtime" not in joined
    assert joined.startswith("wss://dashscope.example/api-ws/v1/realtime?")


def test_overriding_the_model_leaves_the_rest_of_the_query_alone() -> None:
    """An address can carry more than the model; replacing one parameter must
    not drop the others."""
    url = "wss://host/api-ws/v1/realtime?region=cn&model=old&trace=1"
    joined = with_model(url, "new", explicit=True)
    assert "region=cn" in joined
    assert "trace=1" in joined
    assert "model=new" in joined
    assert "model=old" not in joined


def test_a_parameter_that_merely_ends_in_model_is_not_a_model() -> None:
    """`"model=" in url` matched `llm_model=`, `submodel=`, and any other
    parameter whose name happens to end that way — and then refused to add the
    model that was actually asked for."""
    joined = with_model("wss://host/v1/realtime?llm_model=x", "qwen-flash")
    assert "model=qwen-flash" in joined
    assert "llm_model=x" in joined


def test_no_model_at_all_leaves_the_address_untouched() -> None:
    """s2s resolves to an empty model; stapling `?model=` on would be a lie."""
    assert with_model("ws://127.0.0.1:8765/v1/realtime", "") == ("ws://127.0.0.1:8765/v1/realtime")
    assert with_model("ws://127.0.0.1:8765/v1/realtime", "", explicit=True) == (
        "ws://127.0.0.1:8765/v1/realtime"
    )


# ------------------------------------------------------------ _endpoint_line


def test_the_banner_names_the_model_and_keeps_the_query_off_screen() -> None:
    """The address on screen is the query-less one — it can carry a key — and
    the model lives in exactly that query, so it gets named separately.
    Otherwise nothing on screen says which model this session is talking to.
    """
    line = dev_talk._endpoint_line(
        "wss://dashscope.example/api-ws/v1/realtime?api_key=s3cret&model=qwen-flash"
    )
    assert line == "wss://dashscope.example/api-ws/v1/realtime，模型 qwen-flash"
    assert "s3cret" not in line


def test_the_banner_says_nothing_about_a_model_when_there_is_none() -> None:
    """s2s dials an address with no model in it; an empty 「模型 」 tail would
    be noise."""
    assert dev_talk._endpoint_line("ws://127.0.0.1:8765/v1/realtime") == (
        "ws://127.0.0.1:8765/v1/realtime"
    )
    assert dev_talk._endpoint_line("ws://127.0.0.1:8765/v1/realtime?x=1") == (
        "ws://127.0.0.1:8765/v1/realtime"
    )


# ------------------------------------------------------------ _connect_advice


def test_a_failure_with_no_message_still_names_what_went_wrong() -> None:
    """asyncio raises one exception with an empty str(): a TLS handshake that
    gets EOF instead of a record ends as a bare `ConnectionResetError()`
    (asyncio/sslproto.py:467,571) — which is what `wss://` against a plaintext
    port produces. The line used to end at the colon.
    """
    message = dev_talk._connect_advice(
        ConnectionResetError(), ProviderName.DASHSCOPE, "wss://127.0.0.1:8765/v1/realtime"
    )
    assert not message.rstrip().endswith("：")
    assert "ConnectionResetError" in message
    assert "wss://127.0.0.1:8765/v1/realtime" in message


def test_the_no_recipe_line_still_tells_the_streamer_what_to_do() -> None:
    """CLAUDE.md's error-wording rule: a reason without an action is half a
    message. The one action that fits every undiagnosed connect failure is
    「去核对配置里那行地址」."""
    message = dev_talk._connect_advice(
        ConnectionResetError(), ProviderName.DASHSCOPE, "wss://127.0.0.1:8765/v1/realtime"
    )
    assert "bilisama.toml" in message


def test_a_failure_that_does_have_a_message_keeps_it() -> None:
    """The fallback only fills in for an empty reason; it never replaces one."""
    message = dev_talk._connect_advice(
        OSError("no route to host"), ProviderName.S2S, "ws://10.0.0.9:8765/v1/realtime"
    )
    assert "no route to host" in message
    assert "OSError" not in message


# ------------------------------------------------------------ _watch

_QUIETED = ("websockets", "asyncio", "aiohttp", "httpx", "uvicorn")


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """The real setup()/formatter, writing into memory.

    Through the formatter rather than through caplog on purpose: the defect is
    what comes out the far end — a folded field and a traceback that reads
    "NoneType: None" — and a LogRecord shows neither. setup() clears the root
    handlers (obs/logging.py:220), pytest's own included, so this puts them
    back afterwards.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    quieted = {name: logging.getLogger(name).level for name in _QUIETED}
    stream = io.StringIO()
    setup(level="info", stream=stream)
    try:
        yield stream
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, saved in quieted.items():
            logging.getLogger(name).setLevel(saved)


def _deaths(stream: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [line for line in lines if line["event"] == "dev_talk.task_died"]


async def _run_watched(
    body: Literal["raise", "cancel", "return"], name: str = "director:mic"
) -> None:
    """Start one watched task, end it the requested way, and let the done
    callback run."""

    async def work() -> None:
        if body == "raise":
            raise RuntimeError("上行憋了 300 秒也没人收")
        if body == "cancel":
            await asyncio.sleep(3600)

    task = dev_talk._watch(asyncio.create_task(work(), name=name))
    if body == "cancel":
        await asyncio.sleep(0)
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # done callbacks run on the next loop pass


async def test_a_dead_task_logs_the_real_reason(log_stream: io.StringIO) -> None:
    """The reported failure: `director:mic` died and the whole record was
    {"event":"dev_talk.task_died","task":"director:mic","error_text":"...",
    "exc":"NoneType: None"}. log.exception reads sys.exc_info(), and a done
    callback runs with no active exception — so the one field that was
    supposed to say why carried the string "NoneType: None".
    """
    await _run_watched("raise")
    deaths = _deaths(log_stream)
    assert len(deaths) == 1
    assert deaths[0]["task"] == "director:mic"
    assert deaths[0]["error_type"] == "RuntimeError"
    # Read out of the traceback field rather than out of the whole line: the
    # reason has to survive on its own here, whatever the scrubber does to
    # neighbouring field names.
    assert "上行憋了 300 秒也没人收" in deaths[0]["traceback"]
    assert "test_dev_talk_uplink" in deaths[0]["traceback"]
    assert "NoneType: None" not in json.dumps(deaths[0], ensure_ascii=False)


async def test_a_dead_task_also_says_so_on_the_terminal(
    log_stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ten of the eleven background tasks have no other way to reach the
    streamer — a scheduler that stopped dispatching looks exactly like a quiet
    chat. The log is for afterwards; this is for the session it breaks.
    """
    await _run_watched("raise", name="director:scheduler")
    printed = capsys.readouterr().err
    assert "director:scheduler" in printed
    assert "RuntimeError" in printed
    assert "重启" in printed


async def test_a_cancelled_task_says_nothing(
    log_stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shutdown cancels all eleven on purpose; eleven death notices on every
    Ctrl-C would bury the ones that mean something."""
    await _run_watched("cancel")
    assert capsys.readouterr().err == ""
    assert _deaths(log_stream) == []


async def test_a_task_that_simply_finishes_says_nothing(
    log_stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdin_pump returns when stdin closes, and that is not a fault."""
    await _run_watched("return")
    assert capsys.readouterr().err == ""
    assert _deaths(log_stream) == []


# ------------------------------------------------------------ run_director wiring


def _function(source: str, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"没有找到函数 {name}")


def _run_director() -> ast.AST:
    return _function(_DEV_TALK_PY.read_text(encoding="utf-8"), "run_director")


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _handoff_problems(tree: ast.AST) -> list[str]:
    """Whether the broker built here is told to clear the playback tally.

    The page's playback receipts are the only thing that moves the count
    (ui/audio.py:270-281). A page that leaves mid-sentence never sends the
    `ended` half: the receipt goes out on a socket that is already closed and
    is dropped without a word (ui/web/js/audio.js:24,248). So the count keeps
    an offset for the rest of the session — first the floor gate is stuck shut
    and she stops talking, and then, once something else clears
    `queued_audio`, the gate stops CLOSING too, because `outstanding` never
    comes back to 1 (ui/audio.py:270-273).
    """
    problems: list[str] = []
    built = _calls(tree, "AudioBroker")
    if not built:
        return ["run_director 里没有 AudioBroker(...)"]
    for call in built:
        handoff = next((kw for kw in call.keywords if kw.arg == "on_handoff"), None)
        if handoff is None:
            problems.append("AudioBroker 少了 on_handoff：页面走了没人清 PlaybackTally")
            continue
        referenced = {node.id for node in ast.walk(handoff.value) if isinstance(node, ast.Name)}
        if "tally" not in referenced:
            problems.append("on_handoff 没接到 tally 上")
            continue
        tallies = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PlaybackTally"
        ]
        if not tallies:
            problems.append("on_handoff 引用了 tally，但这里没有 PlaybackTally(...)")
        elif min(tallies) > call.lineno:
            problems.append("tally 建在 AudioBroker 之后，on_handoff=tally.cancelled 会 NameError")
    return problems


def test_the_broker_clears_the_tally_when_the_devices_change_hands() -> None:
    assert _handoff_problems(_run_director()) == []


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param(
            "async def run_director(args):\n"
            "    broker = AudioBroker(local=local_pair)\n"
            "    tally = PlaybackTally(on_playback=floor.on_playback, notify=notify)\n",
            id="没接 on_handoff",
        ),
        pytest.param(
            "async def run_director(args):\n"
            "    broker = AudioBroker(local=local_pair, on_handoff=tally.cancelled)\n"
            "    tally = PlaybackTally(on_playback=floor.on_playback, notify=notify)\n",
            id="tally 建得太晚",
        ),
    ],
)
def test_the_handoff_check_catches_a_broker_that_forgets_the_tally(planted: str) -> None:
    """The check runs over a violation too — otherwise the green above proves
    only that the checker never fires."""
    assert _handoff_problems(_function(planted, "run_director")) != []


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"没有找到类 {name}")


def _method(klass: ast.ClassDef, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in klass.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{klass.name} 没有 {name}")


def _latch_problems(tree: ast.AST) -> list[str]:
    """Whether _LocalPair.resume() honours the shutdown latch, and honours it
    before it touches anything.

    resume() takes the speaker back and starts a new microphone task, in that
    order. A latch checked halfway down would still leave the output device
    reopened after 下播.
    """
    klass = _class(tree, "_LocalPair")
    flags = {
        target.attr
        for node in ast.walk(_method(klass, "stop_reviving"))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    if not flags:
        return ["stop_reviving() 什么都没记下来"]
    resume = _method(klass, "resume")
    first = resume.body[0] if resume.body else None
    if not isinstance(first, ast.If):
        return ["resume() 开头没有检查停机闩，设备会在收尾期间被起回来"]
    read = {
        node.attr
        for node in ast.walk(first.test)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    if not (read & flags):
        return ["resume() 开头那个判断读的不是 stop_reviving() 设的那个标志"]
    if not any(isinstance(node, ast.Return) for node in first.body):
        return ["resume() 看到停机闩之后没有 return"]
    return []


def test_the_local_pair_refuses_to_come_back_after_shutdown_starts() -> None:
    assert _latch_problems(_run_director()) == []


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param(
            "class _LocalPair:\n"
            "    def stop_reviving(self):\n"
            "        pass\n"
            "    async def resume(self):\n"
            "        await asyncio.to_thread(speaker.resume)\n",
            id="闩没记下来",
        ),
        pytest.param(
            "class _LocalPair:\n"
            "    def stop_reviving(self):\n"
            "        self._done = True\n"
            "    async def resume(self):\n"
            "        await asyncio.to_thread(speaker.resume)\n"
            "        if self._done:\n"
            "            return\n",
            id="扬声器已经先被起回来了",
        ),
    ],
)
def test_the_latch_check_catches_a_resume_that_ignores_it(planted: str) -> None:
    assert _latch_problems(ast.parse(planted)) != []


def _teardown_problems(tree: ast.AST) -> list[str]:
    """Whether teardown stops the local pair from coming back before it starts
    cancelling.

    A page holding the devices at Ctrl-C releases them from inside uvicorn's
    own task, which keeps running while the gather at the top of the finally
    waits (ui/server.py:394 -> _LocalPair.resume). The microphone and the
    speaker are then reopened AFTER 下播 and stay open through distillation —
    a live capture on a session that has already said goodbye.
    """
    finallys = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and node.finalbody]
    if not finallys:
        return ["run_director 没有收尾的 finally"]
    for block in finallys:
        cancels = [
            index
            for index, stmt in enumerate(block.finalbody)
            for node in ast.walk(stmt)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cancel"
        ]
        if not cancels:
            continue
        latches = [
            index
            for index, stmt in enumerate(block.finalbody)
            for node in ast.walk(stmt)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stop_reviving"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "local_pair"
        ]
        if not latches:
            return ["收尾没有 local_pair.stop_reviving()：麦克风会在下播之后被起回来"]
        if min(latches) > min(cancels):
            return ["local_pair.stop_reviving() 排在取消任务之后，那时麦克风已经回来了"]
        return []
    return ["run_director 的 finally 里没有取消任务这一步"]


def test_teardown_latches_the_local_pair_before_it_cancels_anything() -> None:
    assert _teardown_problems(_run_director()) == []


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param(
            "async def run_director(args):\n"
            "    try:\n"
            "        await stop.wait()\n"
            "    finally:\n"
            "        for task in tasks:\n"
            "            task.cancel()\n"
            "        await ui_server.stop()\n",
            id="根本没有闩",
        ),
        pytest.param(
            "async def run_director(args):\n"
            "    try:\n"
            "        await stop.wait()\n"
            "    finally:\n"
            "        for task in tasks:\n"
            "            task.cancel()\n"
            "        local_pair.stop_reviving()\n",
            id="闩上得太晚",
        ),
    ],
)
def test_the_teardown_check_catches_a_microphone_that_comes_back(planted: str) -> None:
    assert _teardown_problems(_function(planted, "run_director")) != []


# ------------------------------------------------------------ panic mute from the terminal


class _FakePanics:
    """Stands in for the Scheduler's two panic controls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def panic_mute(self) -> None:
        self.calls.append("mute")

    def release_panic(self) -> None:
        self.calls.append("release")


@pytest.mark.parametrize("typed", ["/mute", "/闭麦", "  /MUTE  "])
def test_the_terminal_can_panic_mute(typed: str) -> None:
    """Backlog #47: the red button lived only on the web panel, so a session
    started with --no-ui — or one whose port was taken — had no way to shut her
    up at all. panic-mute is the plan's single 「出事了一键闭嘴」 mechanism.
    """
    assert dev_talk._panic_command(typed) is True


@pytest.mark.parametrize("typed", ["/unmute", "/开麦"])
def test_the_terminal_can_let_her_speak_again(typed: str) -> None:
    assert dev_talk._panic_command(typed) is False


@pytest.mark.parametrize("typed", ["", "你好", "/sc 阿强 30 在玩什么", "/mut", "mute", "/mute now"])
def test_an_ordinary_line_is_not_a_panic_command(typed: str) -> None:
    """The console is a danmaku box first. Swallowing 「mute」 as a command would
    make an ordinary word unsendable, and a partial match would fire on a typo.
    """
    assert dev_talk._panic_command(typed) is None


def test_the_panic_command_reaches_the_scheduler_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    panics = _FakePanics()
    said: list[str] = []
    dev_talk._apply_panic(True, panics, said.append)
    dev_talk._apply_panic(False, panics, said.append)

    assert panics.calls == ["mute", "release"]
    # 「叫停」不是「闭麦」：这个开关停的是她，麦克风一根汗毛都不碰，而
    # --mute-while-speaking 停的才是麦克风。两个词曾经在同一个程序里指相反的事。
    assert any("叫停" in line for line in said)
    assert not any("闭麦" in line for line in said)
    assert any("恢复" in line for line in said)


# ------------------------------------------------------------ correlation ids


def _handle() -> link.ReplyHandle:
    return link.ReplyHandle()


def _done(status: link.ReplyStatus) -> link.ReplyDone:
    return link.ReplyDone(handle=_handle(), status=status)


async def _feed(events: list[Any]) -> AsyncIterator[Any]:
    for event in events:
        yield event


async def test_every_turn_gets_a_correlation_id(log_stream: io.StringIO) -> None:
    """Backlog #62: `bind()` had no caller anywhere in src, so `turn_id` never
    reached a single log line and §4.12's "correlation ids on every line" was
    zero lines. Without it the log groups by event name and by nothing else —
    "why didn't she answer that one" needs the lines of ONE turn together.
    """
    await dev_talk._consume_events(
        _feed(
            [
                link.SpeechStopped(),
                link.UserTranscriptDone(text="在玩什么"),
                link.ReplyTextDelta(handle=_handle(), text="在玩老头环。"),
                _done(link.ReplyStatus.COMPLETED),
            ]
        ),
        None,
        None,
    )
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line]
    turns = [line for line in lines if line["event"] == "dev_talk.turn_done"]
    assert len(turns) == 1
    assert turns[0]["turn_id"]
    assert turns[0]["status"] == "completed"


async def test_two_turns_do_not_share_an_id(log_stream: io.StringIO) -> None:
    """Two ids or the grouping is decoration."""
    await dev_talk._consume_events(
        _feed(
            [
                link.ReplyTextDelta(handle=_handle(), text="一"),
                _done(link.ReplyStatus.COMPLETED),
                link.ReplyTextDelta(handle=_handle(), text="二"),
                _done(link.ReplyStatus.CANCELLED),
            ]
        ),
        None,
        None,
    )
    turns = [
        json.loads(line)
        for line in log_stream.getvalue().splitlines()
        if line and json.loads(line)["event"] == "dev_talk.turn_done"
    ]
    assert len(turns) == 2
    assert turns[0]["turn_id"] != turns[1]["turn_id"]


async def test_the_correlation_id_is_gone_once_the_turn_is(log_stream: io.StringIO) -> None:
    """A contextvar left set would tag every later line with a turn that ended —
    worse than no id at all, because it reads as evidence."""
    await dev_talk._consume_events(_feed([_done(link.ReplyStatus.COMPLETED)]), None, None)
    dev_talk.log.info("dev_talk.after_the_turn")
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line]
    after = next(line for line in lines if line["event"] == "dev_talk.after_the_turn")
    assert "turn_id" not in after


async def test_the_link_probe_is_fed_from_the_event_stream(log_stream: io.StringIO) -> None:
    """§4.12 wants provider connection state in the health snapshot. The link's
    own events are the only place it exists, and this consumer already reads
    every one of them."""
    clock = FakeClock()
    health = LinkHealth(clock, provider="s2s")
    await dev_talk._consume_events(
        _feed(
            [
                link.LinkDown(reason="1006", retrying=True),
                link.LinkUp(attempts=2),
                link.LinkError(code="invalid_request_error", detail="voice 名字不对"),
            ]
        ),
        None,
        None,
        link_health=health,
    )
    status = health.status()
    assert status["connected"] is True
    assert status["reconnect_attempts"] == 2
    assert status["drops"] == 1
    assert "voice 名字不对" in status["last_error"]


# ------------------------------------------------------------ run_director, actually run
#
# Backlog #53: the checks above read this function's source. That catches wiring
# that was written wrong, and nothing at all about wiring that was never reached
# — measured, only 26 of dev_talk's 80 definitions ever executed, and neither
# run_director nor main was among them. What follows starts the real thing with
# every device and socket replaced, and then asserts on what it printed and left
# behind.
#
# The two replacements are not optional: _Speaker opens a PortAudio output
# stream in its constructor (dev_talk.py:433) and _pump_mic opens the microphone
# (dev_talk.py:384). A test run must touch neither.


class _FakeLink:
    """A SpeechLink that connects instantly and says nothing.

    Collects its instances so the test can reach the one run_director built
    inside itself — readiness is "the persona has been pushed", and teardown is
    "this got closed".
    """

    instances: ClassVar[list[_FakeLink]] = []

    def __init__(self, url: str, **kwargs: Any) -> None:
        _FakeLink.instances.append(self)
        self.url = url
        # SpeechLink promises it, and _Fanout reads it while wrapping.
        self.quiet_window_s = float(kwargs.get("quiet_window_s", 0.6))
        self.contexts: list[str] = []
        self.closed = False
        self._events: asyncio.Queue[Any] = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def set_context(self, instructions: str) -> None:
        self.contexts.append(instructions)

    async def push_audio(self, pcm: bytes) -> None:
        return None

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        return None

    async def request_reply(self, spec: Any) -> Any:
        return link.ReplyHandle()

    async def cancel(self, handle: Any) -> None:
        return None

    async def end_protection(self) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        while True:
            yield await self._events.get()


class _SilentSpeaker:
    """dev_talk._Speaker without PortAudio behind it."""

    def __init__(self, device: int | None = None) -> None:
        self.closed = False
        self.played = bytearray()

    @property
    def busy(self) -> bool:
        return False

    backlog_s = 0.0
    dropped_s = 0.0

    def play(self, pcm: bytes) -> None:
        self.played.extend(pcm)

    def flush(self) -> None:
        self.played.clear()

    def suspend(self) -> None:
        return None

    def resume(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _director_args(config: Path) -> argparse.Namespace:
    """The real flag parser, driven the way the runbook drives it.

    Through `build_parser` rather than a hand-built Namespace: a Namespace
    written out here goes stale the day a flag is added, and it would go stale
    silently in the one test that runs this function for real.
    """
    return dev_talk.build_parser().parse_args(
        [
            "--director",
            "--no-ui",  # no uvicorn, no browser, no port to bind
            "--no-pet",
            "--plain-console",
            "--provider",
            "s2s",
            "--config",
            str(config),
        ]
    )


def _director_config(root: Path) -> Path:
    """A config directory complete enough for the real startup path."""
    repo = Path(__file__).resolve().parents[2]
    (root / "safety").mkdir(parents=True, exist_ok=True)
    (root / "safety" / "wordlist.txt").write_text("测试敏感词\n", encoding="utf-8")
    (root / "prompts").mkdir(exist_ok=True)
    (root / "prompts" / "proactive.md").write_text("随便聊点什么。\n", encoding="utf-8")
    shutil.copytree(repo / "config" / "personas" / "tofu", root / "personas" / "tofu")
    shutil.copytree(repo / "config" / "personas" / "live", root / "personas" / "live")
    shutil.copytree(repo / "config" / "testsets", root / "testsets")
    path = root / "bilisama.toml"
    path.write_text(
        "config_version = 1\n"
        '[speech.s2s]\nllm_model = "our-s2t-v1"\n'
        '[interaction]\nchattiness = "low"\n'
        '[persona]\nid = "tofu"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def director_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Everything that would otherwise reach a device, a socket or $HOME."""
    _FakeLink.instances.clear()
    monkeypatch.setattr(dev_talk, "_Speaker", _SilentSpeaker)

    async def _no_mic(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(dev_talk, "_pump_mic", _no_mic)
    monkeypatch.setattr(s2s_module, "S2SLink", _FakeLink)
    # The side model is built straight from the environment (dev_talk.py:1182-1205),
    # so on a machine that has sourced path.sh this fixture's promise was false:
    # teardown distillation made a REAL network call to a REAL model, this test
    # asserted 「ran=False」 and failed, and the run took 36 seconds longer.
    # Which meant the gate was green or red depending on whether the developer
    # happened to have credentials exported — and unit tests were spending money.
    for name in (
        "openai_compatible_url",
        "side_model_name",
        "ali_api_key",
        "base_url",
        "model_name",
        "api_key",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # persona.loader.default_data_dir reads this, so the memory db and the live
    # persona copies land in tmp instead of the developer's real data home.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # closes at once, pump returns
    # run_director installs logging for real (relog → obs.logging.setup), which
    # clears the root handlers and leaves one pointed at capsys' stderr. That
    # stream is closed when the test ends, so the NEXT test in the session to
    # log anything at all got "--- Logging error --- ValueError: I/O operation
    # on closed file" on its stderr — a failure in a file that never touched
    # logging. Same restore the log_stream fixture above does.
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    quieted = {name: logging.getLogger(name).level for name in (*_QUIETED, "blivedm")}
    try:
        yield _director_config(tmp_path / "conf")
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, saved in quieted.items():
            logging.getLogger(name).setLevel(saved)


async def _run_until_ready(args: Any, *, ready: float = 5.0) -> _FakeLink:
    """Start run_director, wait until it has pushed the persona, then Ctrl-C it.

    Cancelling is what Ctrl-C does to this coroutine — it is parked on
    `stop.wait()` — so the whole finally chain runs exactly as it does at 下播.
    """
    task = asyncio.create_task(dev_talk.run_director(args), name="test:director")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ready
    while loop.time() < deadline:
        if _FakeLink.instances and _FakeLink.instances[-1].contexts:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - only on a hang, and then the assert says so
        task.cancel()
        raise AssertionError("run_director 没在预期时间内把人设推上去")
    await asyncio.sleep(0.05)  # let the task list settle
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return _FakeLink.instances[-1]


async def test_the_director_stands_the_whole_stack_up_and_takes_it_down(
    director_box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One end-to-end pass: config in, banners out, teardown chain complete.

    Each assertion below is a step that had no behavioural test at all — the
    AST checks further up cannot tell whether any of this RUNS.
    """
    speech = await _run_until_ready(_director_args(director_box))
    printed = capsys.readouterr().out

    # Config actually drove the run: chattiness low comes from the file, and
    # 180s is what derive() makes of it (config/derive.py).
    assert "话痨度 low" in printed
    assert "冷场 180s" in printed
    # The wordlist gate ran on the real path, not just in `validate`.
    assert "[安全] 词表已装载，命中策略 drop_sentence" in printed
    # The persona reached the link before any task started.
    assert speech.contexts
    # Teardown, in order: the snapshot, distillation, then the farewell.
    assert "[状态]" in printed
    assert "[蒸馏] ran=False" in printed
    assert printed.rstrip().endswith("再见。")
    assert speech.closed is True


def _logged(err: str) -> list[dict[str, Any]]:
    """The JSON log lines out of a director run's stderr.

    run_director configures logging for real, and with --no-ui the console
    keeps every level (obs/logging.py `console_level`), so its own handler
    writes the JSON beside the Chinese narration. Non-JSON lines are the
    narration — the prints this change is forbidden to remove.
    """
    lines: list[dict[str, Any]] = []
    for line in err.splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines


async def test_the_start_banner_is_a_structured_line_too(
    director_box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Which provider, whose address, which persona, which room.

    All four were on the terminal only, in a banner that scrolls away, and
    every one of them has been guessed wrong reading a bug report afterwards.
    The Chinese banner stays exactly where it was — this is the copy that
    survives the session.
    """
    await _run_until_ready(_director_args(director_box))
    captured = capsys.readouterr()

    assert "已连接 s2s" in captured.out  # the human line, untouched
    logged = _logged(captured.err)
    started = [line for line in logged if line["event"] == "dev_talk.session_started"]
    assert len(started) == 1, f"启动横幅没有对应的日志：{captured.err}"
    assert started[0]["provider"] == "s2s"
    assert started[0]["persona"] == "tofu"
    assert started[0]["chattiness"] == "low"
    # No room in this config, and no credential to go with it.
    assert started[0]["room_id"] == 0
    assert started[0]["room_credentialed"] is False
    # The address is logged with its query stripped, the same rule the banner
    # follows: a hosted endpoint carries its model — and sometimes a token —
    # in exactly that query.
    assert "?" not in started[0]["endpoint"]


async def test_a_swallowed_teardown_failure_keeps_its_traceback(
    director_box: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The [收尾] chain's eight `print(..., file=sys.stderr)` arms.

    Each of them turned an exception into one sentence and dropped the
    traceback on the floor — and a shutdown that leaves the database or the
    socket open is exactly the failure nobody can reproduce the next morning.
    The sentence still goes to the terminal; the traceback now goes to the log.
    """

    class _StuckSpeaker(_SilentSpeaker):
        def close(self) -> None:
            raise RuntimeError("PortAudio 不肯撒手")

    monkeypatch.setattr(dev_talk, "_Speaker", _StuckSpeaker)

    await _run_until_ready(_director_args(director_box))
    captured = capsys.readouterr()

    assert "[收尾] 扬声器没关上" in captured.err  # the human line, still there
    failures = [
        line for line in _logged(captured.err) if line["event"] == "dev_talk.speaker_close_failed"
    ]
    assert len(failures) == 1, f"收尾异常又被吞了：{captured.err}"
    assert failures[0]["error_text"] == "PortAudio 不肯撒手"
    # The half the print threw away.
    assert "RuntimeError" in failures[0]["exc"]
    assert "NoneType: None" not in failures[0]["exc"]


async def test_the_director_registers_every_health_probe(
    director_box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Plan §4.12 wants one snapshot that answers "what state is everything in".

    The exit snapshot is its only reader until the panel is up, and it is also
    the only place a probe that was never registered shows up as missing.
    """
    await _run_until_ready(_director_args(director_box))
    snapshot = _exit_snapshot(capsys.readouterr().out)

    assert set(snapshot) >= {
        "assembly",
        "proactive",
        "scheduler",
        "loop",
        "selector",
        "link",
        "outcomes",
    }
    # The two this round added, with the shape health promises rather than an
    # empty dict: connected because _connect_or_exit got past.
    assert snapshot["link"]["connected"] is True
    assert snapshot["link"]["provider"] == "s2s"
    assert snapshot["outcomes"]["seen"] == 0
    assert snapshot["outcomes"]["by_outcome"] == {}


async def test_a_missing_wordlist_stops_the_director_with_a_sentence(
    director_box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error path of §7.6's one hard gate, on the real startup path.

    `validate` reports it in Chinese first (that is test_config_validate.py);
    this pins that the run then actually refuses, and refuses with a sentence
    rather than the FileNotFoundError underneath it.
    """
    (director_box.parent / "safety" / "wordlist.txt").unlink()

    with pytest.raises(SystemExit) as exc_info:
        await dev_talk.run_director(_director_args(director_box))

    printed = capsys.readouterr().out
    assert "敏感词表文件不存在" in printed  # the validate line, before the refusal
    assert "词表是上线硬门槛" in str(exc_info.value)
    assert "Traceback" not in str(exc_info.value)


async def test_a_broken_config_file_is_a_sentence_too(
    director_box: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dev-talk loads with strict=False, which covers a config that is merely
    wrong. A file that cannot be PARSED goes down a different road, and that one
    used to arrive as a traceback in front of a streamer."""
    director_box.write_text("this is not = = toml", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        await dev_talk.run_director(_director_args(director_box))

    err = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "TOML 语法错误" in err
    assert "怎么办" in err


def _exit_snapshot(printed: str) -> dict[str, Any]:
    """The health snapshot dev-talk prints on its way out (dev_talk.py)."""
    line = next(one for one in printed.splitlines() if one.startswith("[状态] "))
    parsed: dict[str, Any] = json.loads(line[len("[状态] ") :])
    return parsed


# ------------------------------------------------------------ signals other groups left


class _Busy:
    def __init__(self, busy: bool) -> None:
        self.busy = busy


@pytest.mark.parametrize(
    ("local", "page", "expected"),
    [
        (False, False, False),
        (True, False, True),  # no page: the sounddevice speaker is the witness
        (False, True, True),  # a page holds the devices: the local speaker is mute
        (True, True, True),
    ],
)
def test_the_pet_hears_whichever_end_is_playing(local: bool, page: bool, expected: bool) -> None:
    """Backlog #26. `speaker.busy` alone reads False for the whole reply once a
    page holds the devices — play() returns immediately with no stream
    (dev_talk.py `_Speaker.play`) — so the pet dropped to 「空闲」 exactly when
    she started talking. PlaybackTally.busy is the other end's witness
    (ui/audio.py, VoiceSignals.audio_busy).
    """
    assert dev_talk._audio_busy(_Busy(local), _Busy(page)) is expected


class _Credential:
    """A danmaku source that answers the two questions the banner cannot."""

    def __init__(self, *, logged_in: bool = False, stale: bool = False) -> None:
        self.logged_in = logged_in
        self.credential_stale = stale


async def test_a_stale_sessdata_corrects_the_startup_banner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backlog #12. The banner is printed before the connection exists, so all
    it can see is that a credential STRING was resolved — an expired SESSDATA
    still prints 「登录态」 while the platform masks every uid to 0
    (ingest/bilibili/source.py `credential_stale`). The correction has to come
    after the connection, and it has to reach the console: the source's own
    warning goes to the log, which is not where the banner was read.
    """
    await dev_talk._watch_credential(_Credential(stale=True), poll_s=0.0)
    printed = capsys.readouterr().out
    assert "匿名" in printed
    assert "SESSDATA" in printed


async def test_a_working_login_says_nothing_more(capsys: pytest.CaptureFixture[str]) -> None:
    """The banner was already right; a second line about it is noise."""
    await dev_talk._watch_credential(_Credential(logged_in=True), poll_s=0.0)
    assert capsys.readouterr().out == ""


async def test_the_credential_watch_waits_rather_than_accusing_early() -> None:
    """Both answers are False until init_room replies, and 「匿名」 printed
    during connect would be a guess dressed as a fact."""
    source = _Credential()
    task = asyncio.create_task(dev_talk._watch_credential(source, poll_s=0.0))
    await asyncio.sleep(0)
    assert not task.done()
    source.logged_in = True
    await asyncio.wait_for(task, timeout=1.0)


# ------------------------------------------------------- bare-link mode refuses


_WIRE_CONFIG = """
config_version = 1
[speech]
provider = "s2s"
[speech.s2s]
llm_model = "our-s2t-v1"
[speech.volcano]
api_key_ref = "env:volcano_api_key"
[speech.openai_ga]
endpoint = "wss://api.openai.com/v1/realtime"
[avatar]
expression_source = "lexicon"
"""


@pytest.mark.parametrize(
    ("provider", "reason"),
    [("volcano", "不说 OpenAI Realtime 方言"), ("openai_ga", "上行要 24000 Hz")],
)
def test_bare_link_mode_refuses_a_backend_it_cannot_drive(
    provider: str, reason: str, tmp_path: Path
) -> None:
    """`--provider` used to list only the two that work, so argparse was the
    guard. Reading the list off the enum instead — right, because it was a
    fourth place that had to learn a new provider's name — removed it, and the
    refusal became a raw ValueError out of codec_for printed after the banner,
    past a main() that catches only KeyboardInterrupt. openai_ga got worse than
    that: it dialled with no Authorization header and no resampling at all.
    """
    config = tmp_path / "bilisama.toml"
    config.write_text(_WIRE_CONFIG, encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        dev_talk.main(["--provider", provider, "--config", str(config)])

    message = str(excinfo.value)
    assert reason in message
    assert "--director" in message, "得告诉主播往哪走"
    for jargon in ("Traceback", "ValueError", "codec_for"):
        assert jargon not in message
