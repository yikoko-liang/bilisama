"""Volcengine's binary dialogue framing, as pure functions.

This provider speaks neither of the two OpenAI dialects dialect.py covers. Its
frames are binary rather than JSON text, its events are numbered rather than
named, and a session lives two levels deep (connection, then session). None of
that fits a Codec, so it gets its own encoder and decoder.

Split from volcano.py the way dialect.py is split from hosted.py: everything
here is pure, so the whole layout can be pinned by a byte array without opening
a socket. The upstream docs publish one complete server frame, and that array
is a test fixture in tests/unit/test_volcano_wire.py — a decoder that cannot
eat a real frame has no business dialing a real endpoint.

Frame layout::

    byte 0   version (high 4) | header size in 4-byte units (low 4)
    byte 1   message type (high 4) | type-specific flags (low 4)
    byte 2   serialization (high 4) | compression (low 4)
    byte 3   reserved
    ...      optional fields, in order: sequence, event, session id, error code
    4 bytes  payload size, big-endian
    N bytes  payload

Every multi-byte integer is big-endian.
"""

from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

__all__ = [
    "ClientEvent",
    "Frame",
    "MessageKind",
    "ServerEvent",
    "VolcanoProtocolError",
    "audio_request",
    "client_request",
    "decode",
]

# Below this number an event is connection-level and carries no session id.
# See the note in decode() — it is the same line in both directions.
_SESSION_SCOPED_FROM = 100

_VERSION = 0b0001
_HEADER_WORDS = 0b0001  # 1 * 4 = 4 bytes
_HEADER_SIZE = _HEADER_WORDS * 4


class MessageKind(IntEnum):
    """The high nibble of byte 1."""

    FULL_CLIENT = 0b0001
    AUDIO_CLIENT = 0b0010
    FULL_SERVER = 0b1001
    AUDIO_SERVER = 0b1011
    ERROR = 0b1111


class _Flag(IntEnum):
    """Low nibble of byte 1. Only EVENT is used by the dialogue API; the
    sequence bits are decoded anyway so a frame carrying them still parses."""

    SEQUENCE = 0b0001
    LAST_NO_SEQ = 0b0010
    EVENT = 0b0100


class _Serialization(IntEnum):
    RAW = 0b0000
    JSON = 0b0001


class _Compression(IntEnum):
    NONE = 0b0000
    GZIP = 0b0001


class ClientEvent(IntEnum):
    """What we may send. Numbers are the wire's, not ours."""

    START_CONNECTION = 1
    FINISH_CONNECTION = 2
    START_SESSION = 100
    FINISH_SESSION = 102
    TASK_REQUEST = 200  # uplink audio
    UPDATE_CONFIG = 201
    SAY_HELLO = 300
    END_ASR = 400
    CHAT_TTS_TEXT = 500
    CHAT_TEXT_QUERY = 501
    CHAT_RAG_TEXT = 502
    CONVERSATION_CREATE = 510
    CONVERSATION_TRUNCATE = 513
    CONVERSATION_DELETE = 514
    # Stops the server mid-reply. The docs qualify it with 「在麦克风按键输入
    # 模式下即 push_to_talk 模式」, which reads like a restriction and is not
    # one: probed live 2026-08-27 in plain server_vad mode, 94 audio frames
    # before it and 1 in-flight frame after, the last arriving 0.05 s later.
    CLIENT_INTERRUPT = 515


class ServerEvent(IntEnum):
    """What it may send us."""

    CONNECTION_STARTED = 50
    CONNECTION_FAILED = 51
    CONNECTION_FINISHED = 52
    SESSION_STARTED = 150
    SESSION_FINISHED = 152
    SESSION_FAILED = 153
    USAGE_RESPONSE = 154
    CONFIG_UPDATED = 251
    TTS_SENTENCE_START = 350
    TTS_SENTENCE_END = 351
    TTS_RESPONSE = 352  # binary audio
    TTS_ENDED = 359
    ASR_INFO = 450  # "用于打断客户端的播报" — the barge-in signal
    ASR_RESPONSE = 451
    ASR_ENDED = 459
    CHAT_RESPONSE = 550
    CHAT_ENDED = 559
    CHAT_TEXT_QUERY_CONFIRMED = 553
    DIALOG_COMMON_ERROR = 599


class VolcanoProtocolError(ValueError):
    """A frame that cannot be parsed.

    Its own type so the adapter can tell "the wire said something we do not
    understand" from "the socket died", and report each honestly instead of
    blaming whichever one it noticed first.
    """


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame. `payload` is already decompressed."""

    kind: MessageKind
    event: int | None
    session_id: str
    payload: bytes
    error_code: int | None = None

    def json(self) -> dict[str, Any]:
        """The payload as a JSON object, or {} when it is not one.

        Empty rather than raising: several server events carry no body at all,
        and a caller reading `frame.json().get("dialog_id")` should not have to
        know which. A payload that is genuinely malformed JSON does raise —
        that is a protocol fault, not an absent body.

        Raises:
            VolcanoProtocolError: The payload is neither empty nor valid JSON.
        """
        if not self.payload:
            return {}
        try:
            loaded = json.loads(self.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VolcanoProtocolError(f"payload 不是 JSON：{exc}") from exc
        return loaded if isinstance(loaded, dict) else {}


def _header(kind: MessageKind, *, flags: int, serialization: int) -> bytes:
    return bytes(
        (
            (_VERSION << 4) | _HEADER_WORDS,
            (kind << 4) | flags,
            (serialization << 4) | _Compression.NONE,
            0,
        )
    )


def _encode(
    kind: MessageKind,
    event: ClientEvent,
    *,
    session_id: str,
    payload: bytes,
    serialization: int,
) -> bytes:
    """Assemble a client frame. Never compresses — see the module note."""
    parts = [_header(kind, flags=_Flag.EVENT, serialization=serialization)]
    parts.append(struct.pack(">i", int(event)))
    # Connection-level events (START_CONNECTION, FINISH_CONNECTION) come before
    # any session exists, so they carry no id. Everything else does, and an
    # empty one there is a caller bug rather than a valid frame — sending it
    # draws a server-side error whose text says nothing about the real cause.
    if session_id:
        raw_id = session_id.encode()
        parts.append(struct.pack(">I", len(raw_id)))
        parts.append(raw_id)
    parts.append(struct.pack(">I", len(payload)))
    parts.append(payload)
    return b"".join(parts)


def client_request(
    event: ClientEvent, *, session_id: str = "", body: dict[str, Any] | None = None
) -> bytes:
    """A JSON-carrying client frame.

    Args:
        event: Which numbered event this is.
        session_id: The session it belongs to; empty for connection-level
            events, which happen before a session exists.
        body: The JSON payload. None sends `{}` rather than nothing — several
            events are documented as taking an empty object, and a zero-length
            payload under a JSON serialization bit is a different frame.
    """
    return _encode(
        MessageKind.FULL_CLIENT,
        event,
        session_id=session_id,
        payload=json.dumps(body if body is not None else {}, ensure_ascii=False).encode(),
        serialization=_Serialization.JSON,
    )


def audio_request(pcm: bytes, *, session_id: str) -> bytes:
    """One uplink audio frame: PCM 16 kHz mono s16le, 20 ms = 640 bytes.

    Raw serialization, not JSON — the payload is the samples themselves.
    """
    return _encode(
        MessageKind.AUDIO_CLIENT,
        ClientEvent.TASK_REQUEST,
        session_id=session_id,
        payload=pcm,
        serialization=_Serialization.RAW,
    )


def _take(raw: bytes, offset: int, count: int, what: str) -> tuple[bytes, int]:
    """Slice `count` bytes, or say which field ran off the end.

    Python slicing past the end returns short rather than raising, so without
    this every truncated frame would surface as a confusing struct.error or,
    worse, as a silently wrong value.

    Raises:
        VolcanoProtocolError: The frame is shorter than the field it declares.
    """
    end = offset + count
    if end > len(raw):
        raise VolcanoProtocolError(f"帧被截断：读{what}要 {count} 字节，只剩 {len(raw) - offset}")
    return raw[offset:end], end


def decode(raw: bytes) -> Frame:
    """Parse one server frame.

    Written to survive anything the socket hands over: a short frame, a
    declared length that overruns the buffer, a message type we have no name
    for. Each of those is a VolcanoProtocolError naming the field that broke,
    because the alternative — an IndexError from deep inside a slice — tells
    whoever reads the log nothing about the wire.

    Raises:
        VolcanoProtocolError: The bytes are not a frame we can read.
    """
    if len(raw) < _HEADER_SIZE:
        raise VolcanoProtocolError(f"帧太短：{len(raw)} 字节，连 4 字节头都不够")

    header_words = raw[0] & 0x0F
    header_size = header_words * 4
    if header_size < _HEADER_SIZE:
        raise VolcanoProtocolError(f"头长声明成 {header_size} 字节，至少要 4")

    kind_bits = raw[1] >> 4
    try:
        kind = MessageKind(kind_bits)
    except ValueError as exc:
        raise VolcanoProtocolError(f"不认识的消息类型 0b{kind_bits:04b}") from exc

    flags = raw[1] & 0x0F
    serialization = raw[2] >> 4
    compression = raw[2] & 0x0F

    # Skip any header words beyond the four we understand rather than assuming
    # there are none: the size field exists so the format can grow, and a
    # decoder that ignores it breaks on the first frame that uses it.
    offset = header_size

    if flags & (_Flag.SEQUENCE | _Flag.LAST_NO_SEQ):
        # Unused by the dialogue API, read so a frame carrying it still parses.
        _, offset = _take(raw, offset, 4, "sequence")

    event: int | None = None
    if flags & _Flag.EVENT:
        chunk, offset = _take(raw, offset, 4, "event")
        event = struct.unpack(">i", chunk)[0]

    session_id = ""
    # Connection-level events carry no id; everything else does, length first.
    #
    # 100 is the line in BOTH directions, which is why the constant is a bare
    # number rather than one of the enums: client 1/2 (StartConnection,
    # FinishConnection) and server 50/51/52 sit below it, client StartSession
    # (100) and server SessionStarted (150) above. Keying off ServerEvent
    # .SESSION_STARTED instead read right for every server frame and skipped
    # the id on StartSession, shifting every byte after it — and the vendor's
    # published sample frame is event 352, so it could never have caught this.
    if event is not None and event >= _SESSION_SCOPED_FROM:
        chunk, offset = _take(raw, offset, 4, "session id 长度")
        id_len = struct.unpack(">I", chunk)[0]
        chunk, offset = _take(raw, offset, id_len, "session id")
        session_id = chunk.decode(errors="replace")

    error_code: int | None = None
    if kind is MessageKind.ERROR:
        chunk, offset = _take(raw, offset, 4, "error code")
        error_code = struct.unpack(">I", chunk)[0]

    chunk, offset = _take(raw, offset, 4, "payload 长度")
    size = struct.unpack(">I", chunk)[0]
    payload, offset = _take(raw, offset, size, "payload")

    if compression == _Compression.GZIP:
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:  # gzip raises BadGzipFile, an OSError subclass
            raise VolcanoProtocolError(f"payload 说自己是 gzip，解不开：{exc}") from exc
    elif compression != _Compression.NONE:
        raise VolcanoProtocolError(f"不认识的压缩方式 0b{compression:04b}")

    if serialization not in (_Serialization.RAW, _Serialization.JSON):
        raise VolcanoProtocolError(f"不认识的序列化方式 0b{serialization:04b}")

    return Frame(
        kind=kind,
        event=event,
        session_id=session_id,
        payload=payload,
        error_code=error_code,
    )
