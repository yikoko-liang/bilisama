"""Volcengine's binary framing, pinned to a frame the vendor published.

The decoder's whole job is to eat bytes off a socket, so the test that matters
is not a round-trip against our own encoder — that only proves we agree with
ourselves. It is the byte array in the upstream docs, which is a real
TTSResponse captured from the real endpoint. Everything else here is edges.
"""

from __future__ import annotations

import gzip
import json
import struct

import pytest

from bilisama.realtime.providers.volcano_wire import (
    ClientEvent,
    MessageKind,
    ServerEvent,
    VolcanoProtocolError,
    audio_request,
    client_request,
    decode,
)

# Straight from the vendor's own worked example, byte for byte:
#   17  -> version 1, header 1 word (4 bytes)
#   180 -> type 0b1011 (audio-only server response), flags 0b0100 (event present)
#   0   -> Raw serialization, no compression
#   0   -> reserved
#   event 352 (TTSResponse), then a 36-byte session id, then 2044 bytes of Ogg.
_SESSION_ID = "3c791a7d-227a-4446-993b-24f9e302cc98"
_DOC_AUDIO_BODY = b"OggS" + b"\x00" * 2040

# The low nibble of byte 1, spelled out here rather than imported: these tests
# build frames by hand on purpose, so they must not inherit the decoder's own
# idea of what the bits mean.
_SEQUENCE = 0b0001
_LAST_NO_SEQ = 0b0010
_EVENT = 0b0100


def _doc_frame() -> bytes:
    return (
        bytes((17, 180, 0, 0))
        + struct.pack(">i", 352)
        + struct.pack(">I", len(_SESSION_ID))
        + _SESSION_ID.encode()
        + struct.pack(">I", len(_DOC_AUDIO_BODY))
        + _DOC_AUDIO_BODY
    )


def test_the_frame_from_the_vendor_docs_decodes_field_for_field() -> None:
    """If this fails, nothing below it is worth reading."""
    frame = decode(_doc_frame())

    assert frame.kind is MessageKind.AUDIO_SERVER
    assert frame.event == ServerEvent.TTS_RESPONSE
    assert frame.session_id == _SESSION_ID
    assert frame.payload == _DOC_AUDIO_BODY
    assert frame.error_code is None


def test_the_first_four_bytes_say_what_the_docs_say_they_say() -> None:
    """Read the header the long way round, so a decoder that happened to land
    on the right answer by shifting the wrong nibble still fails."""
    raw = _doc_frame()
    assert raw[0] >> 4 == 1, "protocol version"
    assert raw[0] & 0x0F == 1, "header size in 4-byte words"
    assert raw[1] >> 4 == 0b1011, "audio-only server response"
    assert raw[1] & 0x0F == 0b0100, "event number present"


# ------------------------------------------------------------------ encoding


def test_a_connection_level_event_carries_no_session_id() -> None:
    """StartConnection happens before a session exists. Writing an empty
    length field there would shift every byte after it."""
    raw = client_request(ClientEvent.START_CONNECTION)

    assert raw[1] >> 4 == MessageKind.FULL_CLIENT
    assert struct.unpack(">i", raw[4:8])[0] == 1
    # Straight to the payload length: 4 header + 4 event.
    assert struct.unpack(">I", raw[8:12])[0] == len(raw) - 12
    assert json.loads(raw[12:]) == {}


def test_a_session_event_puts_the_id_between_event_and_payload() -> None:
    body = {"tts": {"audio_config": {"format": "pcm_s16le"}}}
    raw = client_request(ClientEvent.START_SESSION, session_id=_SESSION_ID, body=body)

    assert struct.unpack(">i", raw[4:8])[0] == 100
    assert struct.unpack(">I", raw[8:12])[0] == 36
    assert raw[12:48].decode() == _SESSION_ID
    assert json.loads(raw[52:]) == body


def test_uplink_audio_is_raw_not_json() -> None:
    """The payload is samples. A JSON serialization bit here would have the
    server trying to parse PCM as text."""
    pcm = b"\x01\x02" * 320  # one 20 ms frame at 16 kHz mono s16le
    raw = audio_request(pcm, session_id=_SESSION_ID)

    assert len(pcm) == 640
    assert raw[1] >> 4 == MessageKind.AUDIO_CLIENT
    assert raw[2] >> 4 == 0b0000, "raw serialization"
    assert struct.unpack(">i", raw[4:8])[0] == ClientEvent.TASK_REQUEST
    assert raw[-640:] == pcm


def test_chinese_in_a_payload_is_measured_in_bytes_not_characters() -> None:
    """The length prefix counts encoded bytes. Counting characters would
    truncate every payload the persona appears in — which is all of them."""
    body = {"content": "主播今天玩什么"}
    raw = client_request(ClientEvent.CHAT_TEXT_QUERY, session_id=_SESSION_ID, body=body)

    declared = struct.unpack(">I", raw[48:52])[0]
    assert declared == len(raw) - 52
    assert json.loads(raw[52:]) == body


def test_what_we_encode_is_what_we_decode() -> None:
    frame = decode(client_request(ClientEvent.SAY_HELLO, session_id=_SESSION_ID, body={"a": 1}))

    assert frame.event == ClientEvent.SAY_HELLO
    assert frame.session_id == _SESSION_ID
    assert frame.json() == {"a": 1}


# -------------------------------------------------------------------- errors


def test_a_gzipped_payload_comes_back_uncompressed() -> None:
    """Nothing we send is compressed, but the server may compress what it
    sends; a decoder that ignored the bit would hand JSON parsers gzip."""
    body = json.dumps({"content": "你好"}).encode()
    raw = (
        bytes((17, (MessageKind.FULL_SERVER << 4) | 0b0100, 0b0001_0001, 0))
        + struct.pack(">i", ServerEvent.CHAT_RESPONSE)
        + struct.pack(">I", len(_SESSION_ID))
        + _SESSION_ID.encode()
    )
    squeezed = gzip.compress(body)
    raw += struct.pack(">I", len(squeezed)) + squeezed

    assert decode(raw).json() == {"content": "你好"}


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("空帧", b""),
        ("只有半个头", b"\x11\xb4"),
        ("头声明为 0 字", bytes((0x10, 0xB4, 0, 0)) + struct.pack(">i", 352)),
        ("event 读一半", bytes((17, 180, 0, 0)) + b"\x00\x00"),
        ("session id 越界", bytes((17, 180, 0, 0)) + struct.pack(">i", 352) + b"\x00\x00\x0f\xff"),
        ("payload 长度超过实际", _doc_frame()[:-100]),
        ("不认识的消息类型", bytes((17, 0b0111_0100, 0, 0)) + struct.pack(">i", 352)),
        ("gzip 位开着但不是 gzip", bytes((17, 148, 0b0000_0001, 0)) + struct.pack(">I", 0)),
    ],
)
def test_a_broken_frame_is_a_protocol_error_not_a_crash(name: str, raw: bytes) -> None:
    """A socket hands over whatever it hands over. Each of these must name the
    field that broke — an IndexError from inside a slice tells the person
    reading the log nothing about the wire."""
    with pytest.raises(VolcanoProtocolError):
        decode(raw)


def test_the_last_message_flag_does_not_carry_a_sequence_number() -> None:
    """0b0010 is 「最后一帧，不带序列号」. Reading four bytes for it shifts the
    event, the session id and the payload length all four bytes late, and the
    payload length then comes out of the payload — 帧被截断 on a frame that was
    never broken. The vendor's sample frame has flags 0b0100, so it could
    never have caught this; only a frame that actually sets the bit can.
    """
    raw = (
        bytes((17, (MessageKind.FULL_SERVER << 4) | _EVENT | _LAST_NO_SEQ, 0b0001_0000, 0))
        + struct.pack(">i", ServerEvent.TTS_ENDED)
        + struct.pack(">I", len(_SESSION_ID))
        + _SESSION_ID.encode()
        + struct.pack(">I", 0)
    )

    frame = decode(raw)

    assert frame.event == ServerEvent.TTS_ENDED
    assert frame.session_id == _SESSION_ID


def test_the_sequence_flag_still_carries_one() -> None:
    """The other half of the pair: 0b0001 (and 0b0011, which is 0b0001 plus the
    last-message bit) really do put four bytes there, and skipping them would
    shift everything the same way in the other direction."""
    for flags in (_SEQUENCE, _SEQUENCE | _LAST_NO_SEQ):
        raw = (
            bytes((17, (MessageKind.FULL_SERVER << 4) | _EVENT | flags, 0b0001_0000, 0))
            + struct.pack(">i", 7)  # the sequence number itself
            + struct.pack(">i", ServerEvent.TTS_ENDED)
            + struct.pack(">I", len(_SESSION_ID))
            + _SESSION_ID.encode()
            + struct.pack(">I", 0)
        )

        frame = decode(raw)

        assert frame.event == ServerEvent.TTS_ENDED, f"flags 0b{flags:04b}"
        assert frame.session_id == _SESSION_ID, f"flags 0b{flags:04b}"


def test_a_payload_that_is_not_json_says_so_rather_than_pretending() -> None:
    frame = decode(_doc_frame())  # its payload is Ogg audio

    with pytest.raises(VolcanoProtocolError):
        frame.json()


def test_an_absent_body_reads_as_empty_rather_than_raising() -> None:
    """Several server events carry no body. A caller doing
    `frame.json().get(...)` should not have to know which ones."""
    raw = (
        bytes((17, (MessageKind.FULL_SERVER << 4) | 0b0100, 0b0001_0000, 0))
        + struct.pack(">i", ServerEvent.TTS_ENDED)
        + struct.pack(">I", len(_SESSION_ID))
        + _SESSION_ID.encode()
        + struct.pack(">I", 0)
    )

    assert decode(raw).json() == {}
