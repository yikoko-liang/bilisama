"""Dialect codec tables and shapes.

The two protocol versions differ only in names, which is the kind of difference
nothing catches at runtime: a missing table entry looks like an unknown event on the
way in and raises KeyError on the way out. So the first two tests here walk every
ServerEvent member on every codec instead of spot-checking names. That is what found
the missing beta `conversation.item.truncated` entry, and it is what will find the
next one when a third dialect or a new event arrives.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from bilisama.realtime.dialect import BETA, GA, ClientEvent, Codec, Dialect, ServerEvent

CODECS: list[Codec] = [GA, BETA]
CODEC_IDS: list[str] = [c.dialect.value for c in CODECS]


def test_one_codec_per_dialect() -> None:
    """Every Dialect member needs a codec, or the sweeps below quietly skip it."""
    covered = {c.dialect for c in CODECS}
    assert covered == set(Dialect), f"no codec under test for: {set(Dialect) - covered}"


@pytest.mark.parametrize("codec", CODECS, ids=CODEC_IDS)
def test_every_codec_can_name_every_server_event(codec: Codec) -> None:
    """Every internal event must have a wire name in every dialect.

    An event a codec cannot name raises inside the send path, and the failure
    surfaces as a closed socket rather than anywhere that names the real cause.
    """
    unnameable: list[str] = []
    for event in ServerEvent:
        try:
            wire = codec.wire_name(event)
        except KeyError:
            unnameable.append(event.name)
            continue
        assert wire, f"{codec.dialect.value}.wire_name({event.name}) returned an empty string"
    assert not unnameable, f"{codec.dialect.value} has no wire name for: {unnameable}"


@pytest.mark.parametrize("codec", CODECS, ids=CODEC_IDS)
def test_wire_name_round_trips_through_normalize(codec: Codec) -> None:
    """Out and back in must land on the same internal name.

    Catches the same gap from the inbound side, plus any future table where one
    direction knows a name and the other does not.
    """
    broken: list[str] = []
    for event in ServerEvent:
        try:
            wire = codec.wire_name(event)
        except KeyError:
            broken.append(f"{event.name}: no wire name")
            continue
        back, _ = codec.normalize({"type": wire})
        if back is not event:
            broken.append(f"{event.name}: sent as {wire!r}, came back as {back}")
    assert not broken, f"{codec.dialect.value} does not round-trip: {broken}"


@pytest.mark.parametrize(
    ("codec", "wire", "expected"),
    [
        (GA, "session.created", ServerEvent.SESSION_CREATED),
        (GA, "response.output_text.delta", ServerEvent.TEXT_DELTA),
        (GA, "response.output_audio.delta", ServerEvent.AUDIO_DELTA),
        (GA, "response.output_audio_transcript.done", ServerEvent.TRANSCRIPT_DONE),
        (GA, "input_audio_buffer.speech_stopped", ServerEvent.SPEECH_STOPPED),
        (GA, "conversation.item.truncated", ServerEvent.ITEM_TRUNCATED),
        (BETA, "session.created", ServerEvent.SESSION_CREATED),
        (BETA, "response.text.delta", ServerEvent.TEXT_DELTA),
        (BETA, "response.audio.delta", ServerEvent.AUDIO_DELTA),
        (BETA, "response.audio_transcript.done", ServerEvent.TRANSCRIPT_DONE),
        (BETA, "input_audio_buffer.speech_stopped", ServerEvent.SPEECH_STOPPED),
        (BETA, "conversation.item.truncated", ServerEvent.ITEM_TRUNCATED),
    ],
)
def test_normalize_maps_each_dialects_wire_names(
    codec: Codec, wire: str, expected: ServerEvent
) -> None:
    raw: dict[str, Any] = {"type": wire, "delta": "在的在的，弹幕我看到啦"}
    event, payload = codec.normalize(raw)
    assert event is expected
    # Same object, not a copy: callers read the rest of the frame off this.
    assert payload is raw


_MALFORMED: list[Mapping[str, Any]] = [
    {},
    {"type": 7},
    {"type": None},
    {"type": ""},
    {"type": "totally.made.up"},
]


@pytest.mark.parametrize("codec", CODECS, ids=CODEC_IDS)
@pytest.mark.parametrize("raw", _MALFORMED, ids=["no-type", "int", "null", "empty", "unknown"])
def test_normalize_returns_none_for_unknown_and_malformed_frames(
    codec: Codec, raw: Mapping[str, Any]
) -> None:
    """A frame we cannot read is not an exception. It is a None and the frame back.

    Handing the payload back matters: whoever called us is the one who gets to log
    the thing they could not parse.
    """
    event, payload = codec.normalize(raw)
    assert event is None
    assert payload is raw


def test_normalize_does_not_cross_dialects() -> None:
    """Neither codec answers to the other's names.

    Goes red the day someone merges the two tables into one to save a few lines.
    """
    assert GA.normalize({"type": "response.text.delta"})[0] is None
    assert GA.normalize({"type": "response.audio.delta"})[0] is None
    assert BETA.normalize({"type": "response.output_text.delta"})[0] is None
    assert BETA.normalize({"type": "response.output_audio.delta"})[0] is None
    # The names the two versions do share still work on both.
    for codec in CODECS:
        assert codec.normalize({"type": "error"})[0] is ServerEvent.ERROR
        assert codec.normalize({"type": "response.done"})[0] is ServerEvent.RESPONSE_DONE


def test_ga_item_added_is_an_inbound_alias_only() -> None:
    """GA sends two names for one thing, and we send back only the first.

    _reverse() keeps whichever wire name is listed first, so a table can accept an
    alias without that alias becoming what we emit.
    """
    assert GA.normalize({"type": "conversation.item.created"})[0] is ServerEvent.ITEM_CREATED
    assert GA.normalize({"type": "conversation.item.added"})[0] is ServerEvent.ITEM_CREATED
    assert GA.wire_name(ServerEvent.ITEM_CREATED) == "conversation.item.created"
    # Beta never had the alias, so accepting it there would be inventing protocol.
    assert BETA.normalize({"type": "conversation.item.added"})[0] is None


def test_session_patch_uses_the_dialect_modalities_key() -> None:
    """GA renamed `modalities` to `output_modalities` and added a session type."""
    instructions = "你是直播间的助播，说话短一点。"

    ga = GA.session_patch(instructions=instructions, text_only=True)
    assert ga["type"] == ClientEvent.SESSION_UPDATE.value == "session.update"
    assert ga["session"]["instructions"] == instructions
    assert ga["session"]["output_modalities"] == ["text"]
    assert ga["session"]["type"] == "realtime"
    assert "modalities" not in ga["session"]

    beta = BETA.session_patch(instructions=instructions, text_only=True)
    assert beta["session"]["modalities"] == ["text"]
    assert "output_modalities" not in beta["session"]
    assert "type" not in beta["session"]

    # Boundary: asking for audio too means not sending the key at all, rather than
    # sending a list of both names and hoping the endpoint agrees on the spelling.
    for codec in CODECS:
        session = codec.session_patch(instructions=instructions, text_only=False)["session"]
        assert codec.modalities_key not in session
        assert session["instructions"] == instructions


def test_tool_spec_shape_follows_nested_tools() -> None:
    """Beta nests the declaration under `function`; GA keeps it flat."""
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    ga = GA.tool_spec("search_wiki", "查一下资料再回答", parameters)
    assert ga["type"] == "function"
    assert ga["name"] == "search_wiki"
    assert ga["description"] == "查一下资料再回答"
    assert ga["parameters"] == parameters
    assert "function" not in ga

    beta = BETA.tool_spec("search_wiki", "查一下资料再回答", parameters)
    assert beta["type"] == "function"
    assert beta["function"] == {
        "name": "search_wiki",
        "description": "查一下资料再回答",
        "parameters": parameters,
    }
    # The flat keys must not also be present, or a strict endpoint rejects the frame.
    assert "name" not in beta
    assert "parameters" not in beta
