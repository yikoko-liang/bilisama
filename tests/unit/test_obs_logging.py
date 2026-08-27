"""Structured logging: scrubbing, correlation ids and the JSON line shape.

The scrubbing tests are the load-bearing ones. Plan section 4.12 promises danmaku
bodies stay out of the log unless an operator asks for them, and the only thing
standing behind that promise is `_scrub`. It has to fail closed on audience text
without redacting `token_count` as if it were a credential.

The rest of the file pins the line format itself, because these lines are the
audit trail for "why didn't the assistant say anything" — the number one support
question for a live product.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from bilisama.obs import logging as obs_logging
from bilisama.obs.logging import (
    EventLogger,
    bind,
    get_logger,
    rolling_file_handler,
    setup,
)

_QUIETED = ("websockets", "asyncio", "aiohttp", "httpx", "uvicorn")


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Undo what setup() does to global logging state.

    `setup` calls `root.handlers.clear()` (src/bilisama/obs/logging.py:204), which
    throws away the handler pytest installs for caplog. Without this fixture the
    damage outlives the test and shows up as an unrelated failure elsewhere in the
    session. The third-party levels come back too, since setup() lowers them
    (src/bilisama/obs/logging.py:208).
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    quieted = {name: logging.getLogger(name).level for name in _QUIETED}
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, saved in quieted.items():
            logging.getLogger(name).setLevel(saved)


def _capture(
    *,
    level: Literal["debug", "info", "warning", "error"] = "info",
    log_viewer_content: bool = False,
) -> tuple[EventLogger, io.StringIO]:
    """Wire the real setup()/formatter into an in-memory stream."""
    stream = io.StringIO()
    setup(level=level, log_viewer_content=log_viewer_content, stream=stream)
    return get_logger("test.obs"), stream


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def _one(stream: io.StringIO) -> dict[str, Any]:
    lines = _lines(stream)
    assert len(lines) == 1, f"expected one line, got {lines}"
    return lines[0]


def _field(name: str, value: Any, *, log_viewer_content: bool = False) -> Any:
    """Log a single field and return what came out the other end."""
    log, stream = _capture(log_viewer_content=log_viewer_content)
    log.info("test.event", **{name: value})
    return _one(stream)[name]


# ------------------------------------------------------------ secrets


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "dashscope_api_key",
        "token",
        "access_token",
        "cookie",
        "sessdata",
        "bili_jct",
    ],
)
def test_secret_fields_are_redacted(name: str) -> None:
    """Nothing that names a credential reaches the log."""
    assert _field(name, "sk-live-abcdef") == "***"


@pytest.mark.parametrize("name", ["token_count", "max_tokens", "output_tokens", "keyframe"])
def test_metric_fields_are_not_redacted(name: str) -> None:
    """A field that counts tokens is a metric, not a credential."""
    assert _field(name, 1234) == 1234


@pytest.mark.parametrize(
    ("name", "expected"),
    [("apiKey", "***"), ("accessToken", "***"), ("tokenCount", 42), ("maxTokens", 42)],
)
def test_camel_case_field_names_split_the_same_way(name: str, expected: object) -> None:
    """The camelCase seam is a word boundary, so `apiKey` reads like `api_key`."""
    value: Any = "sk-live-abcdef" if expected == "***" else 42
    assert _field(name, value) == expected


def test_descriptor_suffix_only_demotes_the_last_word() -> None:
    """A token prefix is still credential material, so `token_prefix` fails closed."""
    assert _field("token_prefix", "sk-live") == "***"


def test_field_name_with_no_word_characters_is_left_alone() -> None:
    """Splitting a punctuation-only name yields no words; that must not crash."""
    assert _field("--", 1) == 1


# ------------------------------------------------------------ audience content


@pytest.mark.parametrize(
    "name",
    ["text", "user_text", "danmaku_text", "message", "sc_message", "content", "danmaku"],
)
def test_viewer_content_is_folded_to_a_length(name: str) -> None:
    """Plan section 4.12: danmaku bodies are not logged by default."""
    assert _field(name, "主播好帅") == "<4 chars>"


def test_empty_viewer_text_folds_to_zero_chars() -> None:
    assert _field("danmaku_text", "") == "<0 chars>"


@pytest.mark.parametrize("name", ["msg_count", "text_len", "content_type"])
def test_viewer_metadata_passes_through(name: str) -> None:
    """Counting messages is not quoting them; metadata stays readable."""
    assert _field(name, 7) == 7


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("danmaku", ["主播好帅"], "<list>"),
        ("text", b"\xe5\xa5\xbd", "<bytes>"),
        ("content", {"t": "hi"}, "<dict>"),
    ],
)
def test_viewer_content_non_str_does_not_leak_the_body(
    name: str, value: Any, expected: str
) -> None:
    """A LiveEvent or a list of danmaku carries the body too.

    json.dumps(default=str) would stringify it verbatim, so name the type instead.
    """
    assert _field(name, value) == expected


def test_viewer_content_none_stays_none() -> None:
    """An absent body and a withheld body must stay distinguishable."""
    assert _field("user_text", None) is None


@pytest.mark.parametrize("name", ["error_text", "detail", "advice"])
def test_operator_diagnostics_are_never_folded_as_viewer_content(name: str) -> None:
    """`error_text` says WHY something failed; it is not audience text.

    Nineteen call sites log the reason under this name (client.py:346,
    sources.py:126, scheduler.py:337, distill.py:177, …), and folding it to
    `<N chars>` erased the reason from the log file AND from the panel, which
    formats through the same handler (ui/hub.py:70-83).
    """
    reason = "语音服务拒绝连接，重试也没用：HTTP 401"
    assert _field(name, reason) == reason


def test_the_diagnostic_allowlist_does_not_free_audience_text() -> None:
    """Exempting a field must not exempt whatever ends in the same word."""
    assert _field("danmaku_text", "主播好帅") == "<4 chars>"
    assert _field("text", "主播好帅") == "<4 chars>"


def test_a_diagnostic_field_carrying_a_credential_is_still_redacted() -> None:
    """Redaction outranks the allowlist: a token is a token wherever it rides."""
    assert _field("error_token", "sk-live-abcdef") == "***"


def test_opt_in_logs_viewer_content_but_never_secrets() -> None:
    """The debug switch opens the audience gate only, never the credential gate."""
    log, stream = _capture(log_viewer_content=True)
    log.info("ingest.danmaku", user_text="主播好帅", api_key="sk-live-abcdef")
    payload = _one(stream)
    assert payload["user_text"] == "主播好帅"
    assert payload["api_key"] == "***"


# ------------------------------------------------------------ line shape


def test_json_line_carries_the_fixed_keys() -> None:
    log, stream = _capture()
    log.info("vad.speech_stopped", audio_end_ms=12345)
    payload = _one(stream)
    assert payload["event"] == "vad.speech_stopped"
    assert payload["level"] == "info"
    assert payload["logger"] == "test.obs"
    assert payload["audio_end_ms"] == 12345
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{4})?", payload["ts"])


def test_unbound_correlation_ids_are_omitted_not_null() -> None:
    """A null turn_id would look like a bug in the binding, not an absent one."""
    log, stream = _capture()
    log.info("director.idle")
    payload = _one(stream)
    assert "turn_id" not in payload
    assert "intent_id" not in payload
    assert "job_id" not in payload


def test_chinese_values_stay_readable() -> None:
    """ensure_ascii=False — a log full of \\u5c0f escapes is useless here."""
    log, stream = _capture()
    log.info("persona.loaded", persona_name="小沙")
    assert "小沙" in stream.getvalue()
    assert "\\u" not in stream.getvalue()


def test_unserializable_values_do_not_break_the_line() -> None:
    """A value json cannot encode must not take the whole line down."""
    log, stream = _capture()
    log.info("config.loaded", path=Path("/tmp/bilisama.toml"))
    assert _one(stream)["path"] == "/tmp/bilisama.toml"


def test_records_without_fields_still_format() -> None:
    """Third-party libraries log through plain logging and carry no `fields`."""
    _, stream = _capture()
    logging.getLogger("third.party").warning("connection reset")
    payload = _one(stream)
    assert payload["event"] == "connection reset"
    assert payload["level"] == "warning"


# ------------------------------------------------------------ correlation ids


def test_bound_ids_ride_on_every_line_inside_the_block() -> None:
    log, stream = _capture()
    with bind(turn_id="t-1", intent_id="i-1", job_id="j-1"):
        log.info("director.selected")
        log.info("tts.dispatched")
    log.info("director.idle")
    first, second, after = _lines(stream)
    assert first["turn_id"] == second["turn_id"] == "t-1"
    assert first["intent_id"] == "i-1"
    assert first["job_id"] == "j-1"
    assert "turn_id" not in after


def test_bind_sets_only_the_ids_it_is_given() -> None:
    log, stream = _capture()
    with bind(turn_id="t-1"):
        log.info("director.selected")
    payload = _one(stream)
    assert payload["turn_id"] == "t-1"
    assert "intent_id" not in payload
    assert "job_id" not in payload


def test_nested_bind_restores_the_outer_id() -> None:
    log, stream = _capture()
    with bind(turn_id="t-1"):
        with bind(turn_id="t-2"):
            log.info("inner")
        log.info("outer")
    inner, outer = _lines(stream)
    assert inner["turn_id"] == "t-2"
    assert outer["turn_id"] == "t-1"


def test_bind_restores_ids_when_the_block_raises() -> None:
    """A crash mid-turn must not leave a stale turn_id on every later line."""
    log, stream = _capture()
    with pytest.raises(RuntimeError, match="dispatch failed"), bind(turn_id="t-1"):
        raise RuntimeError("dispatch failed")
    log.info("director.idle")
    assert "turn_id" not in _one(stream)


# ------------------------------------------------------------ setup


def test_exception_path_records_the_traceback() -> None:
    log, stream = _capture()
    try:
        raise ValueError("empty response from provider")
    except ValueError:
        log.exception("director.reply_failed", reason="empty_response")
    payload = _one(stream)
    assert payload["level"] == "error"
    assert payload["reason"] == "empty_response"
    assert "ValueError: empty response from provider" in payload["exc"]
    assert "Traceback" in payload["exc"]


def test_lines_without_an_exception_carry_no_exc_key() -> None:
    log, stream = _capture()
    log.error("director.reply_failed", reason="empty_response")
    assert "exc" not in _one(stream)


def test_level_filters_everything_below_the_threshold() -> None:
    log, stream = _capture(level="warning")
    log.debug("vad.frame")
    log.info("director.selected")
    log.warning("provider.retrying")
    log.error("provider.auth_failed")
    assert [line["event"] for line in _lines(stream)] == [
        "provider.retrying",
        "provider.auth_failed",
    ]


def test_debug_level_lets_everything_through() -> None:
    log, stream = _capture(level="debug")
    log.debug("vad.frame")
    log.info("director.selected")
    assert [line["level"] for line in _lines(stream)] == ["debug", "info"]


def test_setup_replaces_handlers_rather_than_stacking_them() -> None:
    """Calling setup() twice must not double every line."""
    first = io.StringIO()
    setup(stream=first)
    second = io.StringIO()
    setup(stream=second)
    get_logger("test.obs").info("director.spoke")
    assert len(logging.getLogger().handlers) == 1
    assert first.getvalue() == ""
    assert len(_lines(second)) == 1


def test_third_party_loggers_are_quieted_to_warning() -> None:
    for name in _QUIETED:
        logging.getLogger(name).setLevel(logging.NOTSET)
    _, stream = _capture(level="debug")
    for name in _QUIETED:
        logging.getLogger(name).info("handshake ok")
    assert _lines(stream) == []
    logging.getLogger("websockets").warning("connection closed")
    assert [line["event"] for line in _lines(stream)] == ["connection closed"]


# ------------------------------------------------------------ extra handlers


class _ListHandler(logging.Handler):
    """Collects formatted lines, standing in for the UI log ring."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_extra_handler_receives_the_same_json_lines() -> None:
    """An extra handler sees every line the stream handler sees, same format."""
    extra = _ListHandler()
    stream = io.StringIO()
    setup(stream=stream, extra_handlers=(extra,))
    get_logger("test.obs").info("director.spoke", turn="t-1")
    assert len(extra.lines) == 1
    payload = json.loads(extra.lines[0])
    assert payload["event"] == "director.spoke"
    assert payload["turn"] == "t-1"
    assert _one(stream)["event"] == "director.spoke"


def test_extra_handler_survives_repeated_setup_without_stacking() -> None:
    """dev-talk calls setup() three times; the ring must ride along exactly once."""
    extra = _ListHandler()
    setup(stream=io.StringIO(), extra_handlers=(extra,))
    setup(stream=io.StringIO(), extra_handlers=(extra,))
    assert logging.getLogger().handlers.count(extra) == 1
    get_logger("test.obs").info("director.spoke")
    assert len(extra.lines) == 1


def test_extra_handler_output_is_scrubbed_like_the_stream() -> None:
    """Scrubbing has one source of truth; the UI ring gets no secrets either."""
    extra = _ListHandler()
    setup(stream=io.StringIO(), extra_handlers=(extra,))
    get_logger("test.obs").info("ingest.danmaku", api_key="sk-live-abcdef", user_text="主播好帅")
    payload = json.loads(extra.lines[0])
    assert payload["api_key"] == "***"
    assert payload["user_text"] == "<4 chars>"


# ------------------------------------------------------------ reserved names


def test_a_field_called_event_does_not_blow_up_the_call() -> None:
    """The exact regression this section exists for.

    `event` was a normal parameter, so a caller wanting to record WHICH event
    failed wrote the natural thing and got `TypeError: got multiple values for
    argument 'event'` instead of a log line. It was found inside an except
    block in director/scheduler.py, where the TypeError killed the very task
    the handler had been added to keep alive — the failure mode is not a
    missing line, it is a dead event loop.
    """
    log, stream = _capture()
    log.warning("scheduler.dispatch_failed", event="link.reply_done", reason="没有槽位")
    payload = _one(stream)
    assert payload["event"] == "scheduler.dispatch_failed"
    assert payload["field_event"] == "link.reply_done"
    assert payload["reason"] == "没有槽位"


def test_a_field_called_level_does_not_blow_up_the_call() -> None:
    """`level` is reserved by _emit for the same reason `event` was."""
    log, stream = _capture()
    log.info("persona.growth_written", level="voice", entries=12)
    payload = _one(stream)
    assert payload["event"] == "persona.growth_written"
    assert payload["level"] == "info"
    assert payload["field_level"] == "voice"
    assert payload["entries"] == 12


@pytest.mark.parametrize(
    "name", ["ts", "level", "event", "logger", "turn_id", "intent_id", "job_id", "exc"]
)
def test_the_formatter_keeps_its_namespace_whether_or_not_the_key_is_present(
    name: str,
) -> None:
    """Renamed rather than dropped, and unconditionally.

    Dropping would lose whatever the caller thought was worth recording, and
    silently — the same shape as the scrub that ate `error_text`. Overwriting is
    worse: `event` and `logger` are how every consumer groups these lines.

    Unconditional because these names mean something here. `turn_id` is the
    correlation id bound around the call; a field of that name is a different
    thing, and letting it occupy the slot on the lines where nothing is bound
    would have anything grouping by turn_id group the wrong lines together.
    """
    log, stream = _capture()
    log.info("some.event", **{name: "调用方给的值"})
    payload = _one(stream)
    assert payload[f"field_{name}"] == "调用方给的值"
    assert payload.get(name) != "调用方给的值"


def test_scrubbing_judges_the_name_the_caller_wrote_not_the_renamed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order inside the loop, pinned — a rename that ran first would leak.

    The example has to be a MULTI-word name matched exactly, because that is
    where a prefix flips the verdict: `_matches` tries the whole key first, then
    splits on word boundaries. `bili_jct` is redacted by the exact match, while
    `field_bili_jct` splits into three words that are each harmless — the real
    credential this would walk out. A single-word name survives prefixing (its
    word is still in there), so testing with one proves nothing.

    No owned key is sensitive today, so the collision cannot be built out of
    real names; the marker set is widened to `turn_id` instead, which tests the
    ordering rather than today's coincidence.
    """
    log, stream = _capture()
    monkeypatch.setattr(obs_logging, "_REDACTED", frozenset(obs_logging._REDACTED | {"turn_id"}))
    log.info("some.event", turn_id="sk-REAL-CREDENTIAL")
    payload = _one(stream)
    assert payload["field_turn_id"] == "***", "改名走在了脱敏前面，凭证漏出去了"


def test_a_field_needing_two_prefixes_does_not_land_on_top_of_the_first() -> None:
    """The `while`, not an `if`.

    One prefix is enough for almost everything, which is exactly why an `if`
    looks sufficient and is not. Order matters: with `field_event` claimed
    first, the later `event` prefixes once onto a name already taken and has to
    go round again. Stopping after one round writes it over the other field.
    """
    log, stream = _capture()
    log.info("real.event", **{"field_event": "先到的这份", "event": "后到的这份"})
    payload = _one(stream)
    assert payload["field_event"] == "先到的这份"
    assert payload["field_field_event"] == "后到的这份"


def test_a_three_way_collision_keeps_all_three_values() -> None:
    """Values all survive a pile-up, however deep it goes."""
    log, stream = _capture()
    log.info("e", **{"event": "A", "field_event": "B", "field_field_event": "C"})
    payload = _one(stream)
    assert {
        payload["field_event"],
        payload["field_field_event"],
        payload["field_field_field_event"],
    } == {
        "A",
        "B",
        "C",
    }


def test_exception_outside_an_except_block_does_not_invent_a_traceback() -> None:
    """`record.exc_info` is not an "is there an exception" test.

    logging fills it from sys.exc_info(), which outside an except block is the
    tuple (None, None, None) — truthy. Formatting that produced a line reading
    `exc: NoneType: None`, which says nothing and looks like a swallowed error.
    """
    log, stream = _capture()
    log.exception("link.retry_scheduled", exc="ConnectionError")
    payload = _one(stream)
    assert "exc" not in payload, f"凭空造了一条 traceback：{payload.get('exc')!r}"
    assert payload["field_exc"] == "ConnectionError"


def test_an_explicit_correlation_id_does_not_shadow_the_bound_one() -> None:
    """Both are real. The bound id is what every other line in the file is
    grouped by, so it keeps the canonical name; the caller's — typically a
    stale frame's turn, named while running inside a newer one — arrives
    beside it rather than in place of it."""
    log, stream = _capture()
    with bind(turn_id="现在这一轮"):
        log.info("link.stale_frame_dropped", turn_id="迟到那一帧的")
    payload = _one(stream)
    assert payload["turn_id"] == "现在这一轮"
    assert payload["field_turn_id"] == "迟到那一帧的"


@pytest.mark.parametrize("method", ["debug", "info", "warning", "error", "exception"])
def test_every_level_takes_the_event_name_positionally_only(method: str) -> None:
    """One signature slipping back to a named parameter is enough to bring the
    TypeError back on that one level only, which is the hardest kind to spot."""
    import inspect

    sig = inspect.signature(getattr(EventLogger, method))
    kinds = {p.name: p.kind for p in sig.parameters.values()}
    assert kinds["event"] is inspect.Parameter.POSITIONAL_ONLY, f"{method} 的 event 不是仅位置"


# ------------------------------------------------------------ two thresholds


def test_the_console_can_be_quieter_than_the_record() -> None:
    """One level decides what exists; another decides what the terminal shows.

    dev-talk narrates in Chinese through print(), and a JSON copy of the same
    events beside it makes the input line unusable. With a panel attached the
    console can drop to warnings and lose nothing, because the panel and the log
    file are both still receiving everything.
    """
    console = io.StringIO()
    panel = logging.StreamHandler(io.StringIO())
    setup(level="info", console_level="warning", stream=console, extra_handlers=(panel,))
    log = get_logger("test.obs")
    log.info("probe.decision")
    log.warning("probe.trouble")

    assert console.getvalue().count("probe.") == 1, "终端漏出了 info 级的行"
    assert "probe.trouble" in console.getvalue()
    assert isinstance(panel.stream, io.StringIO)
    assert panel.stream.getvalue().count("probe.") == 2, "面板没拿到完整记录"


def test_without_a_console_level_nothing_changes() -> None:
    """`--no-ui` has no panel, so the terminal is the only reader there is.
    The old behaviour has to stay the default or those lines go nowhere."""
    console = io.StringIO()
    setup(level="info", stream=console)
    get_logger("test.obs").info("probe.decision")
    assert "probe.decision" in console.getvalue()


def test_the_log_file_survives_a_reconfigure(tmp_path: Path) -> None:
    """dev-talk calls setup() again whenever the level or console mode changes,
    and setup() clears the root handlers each time. The file handler is built
    once by the caller and handed back on every call — the same contract the
    panel's handler already relies on. Building it inside setup() would reopen
    the file per call and leak the old one."""
    target = tmp_path / "logs" / "bilisama.jsonl"
    handler = rolling_file_handler(target)
    for _ in range(3):
        setup(level="info", stream=io.StringIO(), extra_handlers=(handler,))
        get_logger("test.obs").info("probe.line")
    logging.getLogger().handlers.clear()
    handler.close()

    lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3, f"重配之后文件handler掉了：{lines}"
    assert json.loads(lines[0])["event"] == "probe.line"


def test_the_log_file_is_scrubbed_like_every_other_handler(tmp_path: Path) -> None:
    """It shares the formatter, so audience text and credentials are folded
    there too. A file is the one output that outlives the session, which makes
    it the worst place to leak either."""
    target = tmp_path / "bilisama.jsonl"
    handler = rolling_file_handler(target)
    setup(level="info", stream=io.StringIO(), extra_handlers=(handler,))
    get_logger("test.obs").info("probe.line", api_key="sk-REAL", text="观众说的话")
    logging.getLogger().handlers.clear()
    handler.close()

    payload = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert payload["api_key"] == "***"
    assert payload["text"] == "<5 chars>"
