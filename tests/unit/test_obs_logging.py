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

from bilisama.obs.logging import EventLogger, bind, get_logger, setup

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
