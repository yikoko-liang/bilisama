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

import ast
import asyncio
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from bilisama import dev_talk
from bilisama.config.enums import ProviderName
from bilisama.obs.logging import setup

_DEV_TALK_PY = Path(dev_talk.__file__)

# ------------------------------------------------------------ _with_model


def test_a_model_is_appended_when_the_address_names_none() -> None:
    """The plain case, unchanged: config gives a bare address, the resolved
    model gets stapled on."""
    joined = dev_talk._with_model("wss://host/api-ws/v1/realtime", "qwen-flash")
    assert joined == "wss://host/api-ws/v1/realtime?model=qwen-flash"


def test_an_address_that_already_names_a_model_keeps_it() -> None:
    """Nobody on the command line said otherwise, so the address wins — it is
    the more specific of the two, and the resolved model here is only the
    registry default (realtime/providers/__init__.py:117-119)."""
    url = "wss://dashscope.example/api-ws/v1/realtime?model=qwen-omni-turbo-realtime"
    assert dev_talk._with_model(url, "qwen-audio-3.0-realtime-flash") == url


def test_the_command_line_model_beats_the_one_written_into_the_address() -> None:
    """The bug: `--model` ranks first in resolve_endpoint
    (realtime/providers/__init__.py:117-119) and then lost here, because an
    address carrying `?model=` was treated as finished. DashScope's own
    address shape carries one, so the flag was a no-op on the most common
    config there is.
    """
    url = "wss://dashscope.example/api-ws/v1/realtime?model=qwen-omni-turbo-realtime"
    joined = dev_talk._with_model(url, "qwen-audio-3.0-realtime-flash", explicit=True)
    assert "model=qwen-audio-3.0-realtime-flash" in joined
    assert "qwen-omni-turbo-realtime" not in joined
    assert joined.startswith("wss://dashscope.example/api-ws/v1/realtime?")


def test_overriding_the_model_leaves_the_rest_of_the_query_alone() -> None:
    """An address can carry more than the model; replacing one parameter must
    not drop the others."""
    url = "wss://host/api-ws/v1/realtime?region=cn&model=old&trace=1"
    joined = dev_talk._with_model(url, "new", explicit=True)
    assert "region=cn" in joined
    assert "trace=1" in joined
    assert "model=new" in joined
    assert "model=old" not in joined


def test_a_parameter_that_merely_ends_in_model_is_not_a_model() -> None:
    """`"model=" in url` matched `llm_model=`, `submodel=`, and any other
    parameter whose name happens to end that way — and then refused to add the
    model that was actually asked for."""
    joined = dev_talk._with_model("wss://host/v1/realtime?llm_model=x", "qwen-flash")
    assert "model=qwen-flash" in joined
    assert "llm_model=x" in joined


def test_no_model_at_all_leaves_the_address_untouched() -> None:
    """s2s resolves to an empty model; stapling `?model=` on would be a lie."""
    assert dev_talk._with_model("ws://127.0.0.1:8765/v1/realtime", "") == (
        "ws://127.0.0.1:8765/v1/realtime"
    )
    assert dev_talk._with_model("ws://127.0.0.1:8765/v1/realtime", "", explicit=True) == (
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
