"""Contract tests against the real Seeduplex 3.0 endpoint (model 1.2.6.1).

These are the seventeen throwaway probes of 2026-09-16, kept. They are here
rather than in /tmp because of how that day went: the same conclusion was
reached three times on evidence that turned out invalid, and each time the
proof lived in a shell scrollback nobody could re-run. What the file pins is
not the adapter — unit tests do that against the fake — but the ENDPOINT's
behaviour, so the day the vendor changes its mind we find out from a red test
instead of from a stream where she has stopped answering danmaku.

The three facts everything else rests on, and how each one bit:

1. **She composes, but not on demand.** Text we supply reaches her by two
   routes (dialog_context at create, a function-call result mid-session) and
   she answers it in her own words. Nothing we can send makes her compose at
   a moment of our choosing. The first two rounds of probes concluded "she
   cannot compose" — both had fed her through channels she never reads.
   A third correction landed 2026-09-21: "nothing we can send starts a turn"
   was also wrong. `speech_text_buffer.commit` starts one, reading words we
   supply; the sweep had guessed `.append` and never sent `.commit`.
2. **dialog_context must be even in length.** The endpoint pairs it
   positionally and ignores `role`; an odd array is discarded whole while
   `session.created` comes back unchanged. The adapter's `add_context_item`
   used to stash danmaku as a run of lone user turns, i.e. the exact shape
   that wipes the history, with nothing on the wire to say so.
3. **The uplink vocabulary is closed.** An unknown event name draws
   `45000000 unknown event name:<name>` and THEN the hang-up, 0.0-0.1 s
   later. `response.create` is not specially refused; it simply does not
   exist here. (This said "a 1006, not an error frame" until 2026-09-21 —
   both happen, in that order.)

Nothing here opens an audio device. The credential comes from the environment
(path.sh supplies it locally) and every test skips with a plain reason when it
is absent. Tests that need her to take a turn feed a waveform synthesised by
`say`, the same way the volcano contract file does.

Each test opens its own socket: an unknown event name hangs the connection up,
so sharing one would let a single failure cascade into unrelated skips.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import wave
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets

from bilisama.config.enums import ProviderName
from bilisama.config.schema import VolcanoDuplexConfig
from bilisama.realtime.providers import PROFILES
from bilisama.realtime.providers.duplex import PULL_TOOL, session_body
from tests.integration.speech_fixture import speech_wav

pytestmark = pytest.mark.provider_a

_MODEL = "1.2.6.1"
_VOICE = "zh_female_vv_jupiter_bigtts"
_PERSONA = "你叫豆腐，是个直播陪播。回答只说一句话。"
_TAIL_SILENCE_S = 2.5


def _credentials() -> tuple[str, str]:
    key = os.environ.get("volcano_api_key", "")  # noqa: SIM112
    if not key:
        pytest.skip(
            "没有火山 API Key（volcano_api_key）。控制台 > API Key 管理拿一个，"
            "export 到 path.sh 里"
        )
    override = os.environ.get("volcano_duplex_url", "")  # noqa: SIM112
    return override or PROFILES[ProviderName.VOLCANO_DUPLEX].default_url, key


def _create(
    *,
    history: list[dict[str, str]] | None = None,
    instructions: str = _PERSONA,
    pull_tool: bool = False,
) -> dict[str, Any]:
    """A create frame built by the adapter's own builder.

    Deliberately not a hand-written literal: half the value of running these
    against the real endpoint is that the shape under test is the shape we
    ship, so a field that quietly moves shows up here.
    """
    return {
        "type": "session.create",
        "event_id": "contract",
        **session_body(
            VolcanoDuplexConfig(),
            instructions=instructions,
            model=_MODEL,
            speaker=_VOICE,
            history=history,
            pull_tool=pull_tool,
        ),
    }


class _Session:
    """One socket, with the frames it saw."""

    def __init__(self, ws: Any, tmp_path: Path) -> None:
        self.ws = ws
        self.tmp_path = tmp_path
        self.kinds: Counter[str] = Counter()
        self.said = ""
        self.tts_types: list[str] = []
        self.errors: list[dict[str, Any]] = []
        self.call_ids: list[str] = []
        self.audio = bytearray()
        # Frame kinds in arrival order, audio deltas folded out — the order is
        # the whole point of one of the tests below.
        self.order: list[str] = []
        self.asr: list[str] = []
        # The item id on every transcription frame, deltas and completed alike.
        self.asr_items: list[str] = []
        self.asr_final: str | None = None

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    async def collect(self, seconds: float) -> None:
        """Read for a fixed window. Never raises on a closed socket — a 1006 is
        a RESULT here, and several tests assert on it."""
        texts: list[str] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=end - time.monotonic())
            except TimeoutError:
                break
            except websockets.ConnectionClosed:
                self.kinds["<closed>"] += 1
                break
            frame = json.loads(raw)
            kind = str(frame.get("type") or "?")
            self.kinds[kind] += 1
            if kind != "response.output_audio.delta":
                self.order.append(kind)
            if kind == "response.output_text.delta":
                texts.append(str(frame.get("delta") or ""))
            elif kind == "response.output_audio.started":
                self.tts_types.append(str(frame.get("tts_type") or ""))
            elif kind == "conversation.item.input_audio_transcription.delta":
                self.asr.append(str(frame.get("delta") or ""))
                self.asr_items.append(str(frame.get("item_id") or ""))
            elif kind == "conversation.item.input_audio_transcription.completed":
                self.asr_final = str(frame.get("text") or "")
                self.asr_items.append(str(frame.get("item_id") or ""))
            elif kind == "response.output_audio.delta":
                self.audio.extend(base64.b64decode(str(frame.get("delta") or "")))
            elif kind == "response.function_call_arguments.done":
                items = frame.get("items") or [{}]
                self.call_ids.append(str(items[0].get("call_id") or ""))
            elif kind == "error":
                self.errors.append(dict(frame.get("error") or frame))
        self.said += "".join(texts)

    async def speak(self, line: str) -> None:
        """The streamer says something, then stops. The trailing silence is
        what the server's VAD endpoints on; there is no commit on this path."""
        with wave.open(str(speech_wav(self.tmp_path, line))) as w:
            pcm = w.readframes(w.getnframes())
        pcm += b"\x00" * int(16000 * _TAIL_SILENCE_S) * 2
        for i in range(0, len(pcm), 640):
            await self.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm[i : i + 640]).decode(),
                }
            )
            await asyncio.sleep(0.02)

    @property
    def closed(self) -> bool:
        return bool(self.kinds["<closed>"])


async def _open(tmp_path: Path, frame: dict[str, Any] | None = None) -> AsyncIterator[_Session]:
    url, key = _credentials()
    ws = await websockets.connect(
        url, additional_headers={"X-Api-Key": key}, open_timeout=15, max_size=16 << 20
    )
    session = _Session(ws, tmp_path)
    try:
        await session.send(frame if frame is not None else _create())
        await session.collect(5)
        if not session.kinds["session.created"]:
            pytest.skip(f"建会话失败，收到的是 {dict(session.kinds)}")
        yield session
    finally:
        with_close = not session.closed
        if with_close:
            try:
                await session.send({"type": "session.close", "event_id": "bye"})
                await asyncio.sleep(0.3)
            except websockets.ConnectionClosed:
                pass
        await ws.close()


# ------------------------------------------------------------ 1. she composes


async def test_history_at_create_reaches_her(tmp_path: Path) -> None:
    """The control every other context test is read against: if this goes red
    the harness is broken, not the finding."""
    frame = _create(
        history=[
            {"role": "user", "text": "我刚养了一只猫，叫土豆。"},
            {"role": "assistant", "text": "土豆这名字真可爱。"},
        ]
    )
    async for s in _open(tmp_path, frame):
        await s.speak("我的猫叫什么名字")
        await s.collect(13)
        assert "土豆" in s.said, f"成对历史她该记得，实际说的是 {s.said!r}"


async def test_a_danmaku_in_her_context_gets_answered_in_her_own_words(tmp_path: Path) -> None:
    """The finding that changed the product shape. She is never asked to
    answer this — the streamer says something unrelated and she raises it."""
    frame = _create(
        history=[
            {"role": "user", "text": "[弹幕] 观众小王：豆腐你今天播到几点呀？"},
            {"role": "assistant", "text": "这条我还没回。"},
        ]
    )
    async for s in _open(tmp_path, frame):
        await s.speak("我回来了")
        await s.collect(14)
        assert s.tts_types == ["default"], "她自己的轮次，不是我们递的稿子"
        assert any(
            word in s.said for word in ("几点", "播到", "点")
        ), f"上下文里的弹幕她该接得住，实际说的是 {s.said!r}"


async def test_a_function_call_result_is_composed_from(tmp_path: Path) -> None:
    """The one mid-session text channel, and the shape is the vendor's own.

    Two things this pins beyond "it works": the result only lands against a
    call_id she issued, and the reply needs a turn edge after it — the pushed
    audio below is what flushes it.
    """
    instructions = _PERSONA + (
        "\n\n观众的提问你看不到，必须调用 get_live_events 才能拿到。" "主播一问起弹幕就调用它。"
    )
    async for s in _open(tmp_path, _create(instructions=instructions, pull_tool=True)):
        await s.speak("看看现在弹幕里有人问什么")
        await s.collect(14)
        assert s.call_ids, f"她没调工具，收到的是 {dict(s.kinds)}"

        await s.send(
            {
                "type": "conversation.item.create",
                "event_id": "fc",
                "items": [
                    {
                        "call_id": s.call_ids[-1],
                        "role": "tool",
                        "content": [
                            {"type": "input_text", "text": "观众小王问：豆腐你今天播到几点呀？"}
                        ],
                    }
                ],
            }
        )
        await s.collect(6)
        await s.speak("嗯")  # the turn edge that flushes it
        await s.collect(14)
        assert any(
            word in s.said for word in ("几点", "播到", "点")
        ), f"工具结果她该据此组织，实际说的是 {s.said!r}"


async def test_the_pull_tool_we_ship_is_accepted_as_declared(tmp_path: Path) -> None:
    """PULL_TOOL goes out on every session in the 「tool」 route. A schema the
    server rejects would take the whole session with it."""
    async for s in _open(tmp_path, _create(pull_tool=True)):
        assert not s.closed
        assert s.kinds["session.created"] == 1
        assert PULL_TOOL["name"] == "get_live_events", "改名要同时改人设里那句话"


# ------------------------------------------------------------ 2. she cannot start


@pytest.mark.parametrize(
    ("name", "nudge"),
    [
        ("nothing at all", None),
        ("a buffer commit", {"type": "input_audio_buffer.commit", "event_id": "c"}),
        ("the mute keepalive", {"type": "input_audio_mute.commit", "event_id": "m"}),
    ],
    ids=["silence", "commit", "mute"],
)
async def test_nothing_but_the_microphone_starts_a_turn(
    tmp_path: Path, name: str, nudge: dict[str, Any] | None
) -> None:
    """Why ReplySpec.text is required on this provider rather than merely
    supported. The danmaku is in a context she demonstrably reads (the test
    above proves it), and still nothing comes back until a voice does.

    Narrower than it reads, and the name is a leftover. These three nudges do
    not start a turn; `speech_text_buffer.commit` does, and the test below
    pins it. What stays true is the thing this file needs: she will not
    COMPOSE on demand, so the words have to arrive already written."""
    frame = _create(
        history=[
            {"role": "user", "text": "[弹幕] 观众小王：豆腐你今天播到几点呀？"},
            {"role": "assistant", "text": "这条我还没回。"},
        ]
    )
    async for s in _open(tmp_path, frame):
        if nudge is not None:
            await s.send(nudge)
        await s.collect(20)
        assert not s.said, f"{name} 之后她不该开口，实际说了 {s.said!r}"
        assert not s.tts_types


async def test_a_fabricated_call_id_is_not_a_back_door(tmp_path: Path) -> None:
    """If this ever passes, the whole ghostwriter seam can go: it would mean
    text can be pushed into a turn at a moment of our choosing."""
    async for s in _open(tmp_path, _create(pull_tool=True)):
        await s.send(
            {
                "type": "conversation.item.create",
                "event_id": "fake",
                "items": [
                    {
                        "call_id": "fabricated-not-hers",
                        "role": "tool",
                        "content": [{"type": "input_text", "text": "观众问你今天播到几点"}],
                    }
                ],
            }
        )
        await s.collect(8)
        await s.speak("嗯")
        await s.collect(12)
        assert "几点" not in s.said, f"伪造的 call_id 不该被采纳，她说了 {s.said!r}"


# ------------------------------------------------------------ 3. the shapes


@pytest.mark.parametrize(
    ("shape", "history", "kept"),
    [
        ("one lone user", [{"role": "user", "text": "我刚养了一只猫，叫土豆。"}], False),
        (
            "two users then an assistant",
            [
                {"role": "user", "text": "我刚养了一只猫，叫土豆。"},
                {"role": "user", "text": "还有一只狗，叫饺子。"},
                {"role": "assistant", "text": "名字都挺下饭。"},
            ],
            False,
        ),
        (
            "a clean pair",
            [
                {"role": "user", "text": "我刚养了一只猫，叫土豆。"},
                {"role": "assistant", "text": "土豆这名字真可爱。"},
            ],
            True,
        ),
    ],
    ids=["odd-one", "odd-three", "even-two"],
)
async def test_dialog_context_keeps_even_arrays_and_discards_odd_ones(
    tmp_path: Path, shape: str, history: list[dict[str, str]], kept: bool
) -> None:
    """The rule `pair_up` exists to satisfy. Note what failure looks like from
    the wire: `session.created` either way, so only asking her tells you."""
    async for s in _open(tmp_path, _create(history=history)):
        assert s.kinds["session.created"] == 1, "奇数上下文也照样建得起会话"
        await s.speak("我的猫叫什么名字")
        await s.collect(13)
        assert ("土豆" in s.said) is kept, f"{shape}：她说的是 {s.said!r}"


@pytest.mark.parametrize(
    "name",
    ["response.create", "chat.text.query", "ChatTextQuery", "input_text_buffer.commit"],
)
async def test_an_event_outside_the_vocabulary_errors_then_hangs_up(
    tmp_path: Path, name: str
) -> None:
    """An error frame AND the hang-up, in that order, 0.0-0.1 s apart.

    This used to say "a 1006, not an error frame", and half of that was wrong:
    the endpoint does name the offending event —
    `45000000 unknown event name:<name>` — before it drops the socket
    (measured 2026-09-21, polled once a second for 25 s, with and without
    uplink keep-alive). Both halves matter. The frame is why a client can log
    which name it got wrong; the close is why sweeping for names still costs a
    session per guess, so a name should come from the vendor's event reference
    rather than from a hunch."""
    async for s in _open(tmp_path):
        await s.send({"type": name, "event_id": "unknown"})
        await s.collect(6)
        assert s.closed, f"{name} 居然没把连接掐了：{dict(s.kinds)}"
        named = [e for e in s.errors if name in str(e.get("message") or "")]
        assert named, f"掐断之前没说是哪个名字错了：{s.errors}"


async def test_instructions_apply_mid_session(tmp_path: Path) -> None:
    """The persona channel. SC2.0 cannot do this and has to swap sessions;
    this one can, which is why set_context does not churn the connection."""
    async for s in _open(tmp_path):
        await s.speak("你叫什么名字")
        await s.collect(13)
        assert "豆腐" in s.said, f"人设该生效，她说的是 {s.said!r}"

        s.said = ""
        await s.send(
            {
                "type": "session.update",
                "event_id": "rename",
                **session_body(
                    VolcanoDuplexConfig(),
                    instructions="你叫小明，是个直播陪播。回答只说一句话。",
                    model=_MODEL,
                    speaker=_VOICE,
                ),
            }
        )
        await s.collect(3)
        await s.speak("你叫什么名字")
        await s.collect(13)
        assert "小明" in s.said, f"改完人设该改口，她说的是 {s.said!r}"


async def test_an_update_before_her_first_turn_is_not_heard_by_it(tmp_path: Path) -> None:
    """Why DuplexLink.set_context recreates a session that was created empty.

    The rename above works because a turn has already happened when the
    update goes out. dev-talk's order is the other one — connect with no
    instructions, push the persona, THEN the streamer speaks — and that first
    turn answers as the vendor's default assistant. Measured 2026-09-24,
    three runs out of three: 「我叫豆包，是字节跳动开发的AI助手」. If this ever
    goes red, the recreate in set_context has become unnecessary churn.
    """
    async for s in _open(tmp_path, _create(instructions="")):
        await s.send(
            {
                "type": "session.update",
                "event_id": "persona",
                **session_body(
                    VolcanoDuplexConfig(), instructions=_PERSONA, model=_MODEL, speaker=_VOICE
                ),
            }
        )
        await s.collect(3)
        assert s.kinds["session.updated"], f"update 该有回执，收到的是 {dict(s.kinds)}"
        await s.speak("你叫什么名字")
        await s.collect(13)
        assert s.said, "第一轮她得开口，不然这条测不出任何东西"
        assert "豆腐" not in s.said, f"第一轮听见了建会话之后的人设，重建就多余了：{s.said!r}"


# ------------------------------------------------------------ 4. what it sounds like


def _continuity(pcm: bytes) -> tuple[int, float]:
    """Peak, and mean absolute step between neighbours, read as signed 16-bit.

    Speech is a continuous waveform: consecutive samples are close, so the
    mean step is a small fraction of the peak. Anything that is NOT 16-bit
    PCM — float32, Ogg Opus, MP3 — read this way is white noise, where the
    mean step approaches the peak. That ratio is the whole test, and it
    separates the two by an order of magnitude rather than a hair.
    """
    count = len(pcm) // 2
    values = struct.unpack(f"<{count}h", pcm[: count * 2])
    peak = max(abs(v) for v in values)
    step = sum(abs(values[i + 1] - values[i]) for i in range(count - 1)) / (count - 1)
    return peak, step


async def test_the_audio_she_sends_is_the_pcm_we_asked_for(tmp_path: Path) -> None:
    """The one that would have caught the noise, and the reason this file
    exists at all.

    Seventeen rounds of probes counted frames and never listened, so a session
    asking for `pcm` — which is float32 here — passed every check while the
    streamer got harsh static. Nothing else in the suite can catch it: the
    fake server sends two placeholder bytes, and the endpoint reports a format
    it does not honour with the same `session.created` as one it does.
    """
    async for s in _open(tmp_path):
        await s.speak("你好，今天天气怎么样")
        await s.collect(14)
        assert len(s.audio) > 20000, f"没收到够长的音频，只有 {len(s.audio)} 字节"

        for magic, name in ((b"OggS", "Ogg/Opus"), (b"ID3", "MP3"), (b"RIFF", "WAV")):
            assert not bytes(s.audio).startswith(
                magic
            ), f"收到的是 {name}，不是裸 PCM——这个后端的音频要直接喂播放器"

        peak, step = _continuity(bytes(s.audio))
        assert peak > 1000, f"音频几乎是静音（峰值 {peak}），这一轮不算数"
        assert step / peak < 0.15, (
            f"当 16 位 PCM 读是白噪声（峰值 {peak}，相邻样本平均差 {step:.0f}，"
            f"比值 {step / peak:.2f}）。真语音这个比值在 0.05 上下。"
            "多半是 session.create 里的 format.type 不是 pcm_s16le——"
            "写 pcm 会拿到 float32，不写会拿到 Ogg Opus，两种都照回 session.created"
        )


async def test_her_words_start_before_the_frame_that_announces_the_turn(
    tmp_path: Path,
) -> None:
    """Nothing announces a reply here, and her text does not wait for the
    audio frame that comes closest.

    An adapter that books the turn on `response.output_audio.started` drops
    everything streamed ahead of it — measured at eleven characters over five
    deltas, which reads on the panel as her starting mid-sentence rather than
    as anything broken.
    """
    async for s in _open(tmp_path):
        await s.speak("给我讲讲你平时都干什么")
        await s.collect(16)
        assert "response.output_text.delta" in s.order, f"她没说话：{dict(s.kinds)}"
        assert "response.output_audio.started" in s.order, "没有音频起始帧"

        first_text = s.order.index("response.output_text.delta")
        announced = s.order.index("response.output_audio.started")
        assert (
            first_text < announced
        ), "这一轮音频帧先到，那这条测试就白写了——重跑几次确认端点改了行为"
        ahead = sum(1 for kind in s.order[:announced] if kind == "response.output_text.delta")
        assert ahead >= 1, f"音频帧之前只有 {ahead} 个文本块"


async def test_the_streamers_line_is_numbered_and_finished_in_text(tmp_path: Path) -> None:
    """The endpoint facts the adapter builds the streamer's line from.

    Every transcription frame of one line carries the same `item_id`; the
    deltas are the line as it reads so far, not increments; `completed`
    carries the finished line in `text`. Until 2026-10-05 this test read a
    `transcript` field, found it empty and pinned 「completed has no text」 —
    so the adapter rebuilt the line from the last delta, and the backstop was
    tuned for a closing frame we were reading wrong. Measured that day on 15
    lines: `text` equals the last delta every time.

    It also pinned 「each delta is a prefix of the last」. That held for this
    short line but not for long ones: 14 of the 15 revised a word or a mark
    in the middle (「今天。」 then 「今天我们」), which is why the adapter
    measures what a reading adds from the common prefix (link.added_text).
    """
    said = "今天打算打几把游戏"
    async for s in _open(tmp_path):
        await s.speak(said)
        await s.collect(10)
        assert len(s.asr) >= 2, f"转写块太少，没法判断累积与否：{s.asr}"
        assert s.asr_final is not None, "没收到 completed：结束帧真的丢了，兜底计时是对的"
        assert s.asr_final == s.asr[-1], (
            f"completed 的 text={s.asr_final!r} 和最后一块 delta {s.asr[-1]!r} 不一致——"
            "adapter 以 completed 为准，这条变了要看是谁更全"
        )
        assert (
            len(set(s.asr_items)) == 1 and s.asr_items[0]
        ), f"一句话里的转写帧编号不一致：{s.asr_items}——adapter 靠编号认行"
        assert any(
            word in s.asr_final for word in ("游戏", "几把", "今天")
        ), f"completed 应该是整句，实际是 {s.asr_final!r}"


# ------------------------------------------------------------ 5. the poison


async def test_a_tts_replacement_is_never_spoken_while_the_uplink_runs(
    tmp_path: Path,
) -> None:
    """The reason `event_replies = "ghostwriter"` is refused on this provider.

    With audio flowing the way a live microphone flows, a
    `speech_text_buffer.replacement` is never spoken. Not sometimes: forty
    attempts across eight live sessions on 2026-09-16/17 produced zero audio
    frames, in healthy stretches of a session as much as sick ones. It only
    speaks in a session whose uplink never ran at all — 112 ms to first audio
    — which is not a livestream.

    This used to also assert that the frame KILLS the session. That was wrong,
    and the log says so: the session it was inferred from carried on for
    another sixty minutes on the same socket, with twenty streamer turns in
    one five-minute stretch. Two unrelated faults had been read as one story.

    Pinned so the day the vendor fixes it shows up as a red test rather than
    as a guess.
    """
    import base64 as _b64

    url, key = _credentials()
    ws = await websockets.connect(
        url, additional_headers={"X-Api-Key": key}, open_timeout=15, max_size=16 << 20
    )
    frames: list[tuple[str, str]] = []

    async def reader() -> None:
        try:
            async for raw in ws:
                got = json.loads(raw)
                kind = str(got.get("type") or "?")
                if kind != "response.output_audio.delta":
                    frames.append((kind, str(got.get("tts_type") or "")))
        except Exception:
            return

    pending: asyncio.Queue[bytes] = asyncio.Queue()

    async def uplink() -> None:
        """A microphone that never stops, which is the whole point."""
        try:
            while True:
                try:
                    chunk = pending.get_nowait()
                except asyncio.QueueEmpty:
                    chunk = b"\x00" * 640
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": _b64.b64encode(chunk).decode(),
                        }
                    )
                )
                await asyncio.sleep(0.02)
        except Exception:
            return

    def speak(line: str) -> None:
        with wave.open(str(speech_wav(tmp_path, line))) as handle:
            pcm = handle.readframes(handle.getnframes())
        for i in range(0, len(pcm), 640):
            pending.put_nowait(pcm[i : i + 640].ljust(640, b"\x00"))

    tasks = [asyncio.create_task(reader()), asyncio.create_task(uplink())]
    try:
        await ws.send(json.dumps(_create()))
        await asyncio.sleep(2)

        def closed_turns() -> int:
            return sum(
                1
                for kind, _ in frames
                if kind == "conversation.item.input_audio_transcription.completed"
            )

        speak("你好呀")
        await asyncio.sleep(13)
        assert closed_turns() >= 1, f"对照就不通，这一轮不算数：{frames[:8]}"
        before = closed_turns()

        await ws.send(
            json.dumps(
                {
                    "type": "speech_text_buffer.replacement.append",
                    "event_id": "poison",
                    "text": "我插一句主动话题",
                }
            )
        )
        await ws.send(
            json.dumps({"type": "speech_text_buffer.replacement.commit", "event_id": "poison2"})
        )
        await asyncio.sleep(8)
        spoke = any(tts == "chat_tts_text" for _, tts in frames)

        assert not spoke, (
            "端点居然念了我们的稿子——上行在流动时它一直是吞掉的（实盘 40/40）。"
            "如果这条稳定复现，ghostwriter 那条路可以放开了"
        )
        # Deliberately NOT asserting that the session dies here. It does not:
        # the live session this was first inferred from went on talking for
        # another hour. What the frame reliably does is nothing at all.
        assert before >= 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()


@pytest.mark.parametrize("push_audio", [False, True], ids=["no-uplink", "uplink-running"])
async def test_the_greeting_event_starts_a_turn(tmp_path: Path, push_audio: bool) -> None:
    """`speech_text_buffer.commit` — 打招呼, the vendor's mapping of the legacy
    SayHello(300) — starts a turn on demand. This is the event the 2026-09-16
    sweep missed.

    That sweep sent `speech_text_buffer.append`, a guessed name, and concluded
    "nothing we can send starts a turn". The `.commit` spelling was never sent
    to the endpoint at all; the only place the string appeared in this repo was
    our own fake, where it had been assumed into the vocabulary and modelled as
    a silent no-op.

    Measured 2026-09-21 in four conditions: no uplink ever pushed (332 ms to
    first audio), silent uplink (440 ms), and twice after the streamer really
    spoke and she really answered. Audio every time. Both parametrised arms are
    here because the uplink is exactly what kills the sibling channel — see
    test_a_tts_replacement_is_never_spoken_while_the_uplink_runs, which stays
    red-worthy for its own reasons.

    If this ever fails, `request_reply` has lost its only working channel and
    the duplex backend goes mute for every event reply.
    """
    async for s in _open(tmp_path):
        if push_audio:
            await _push_silence(s, 2.0)
        await s.send(
            {"event_id": "greet", "type": "speech_text_buffer.commit", "text": "大家好，我是豆腐。"}
        )
        if push_audio:
            # Keep the uplink flowing while she answers, then read. The push
            # loop does not read the socket, so the collect has to follow it —
            # the frames are buffered, but nothing moves them into `s` until
            # somebody asks.
            await _push_silence(s, 6.0)
        await s.collect(10)
        assert s.audio, "打招呼没出声——这是 request_reply 在这个后端上唯一能用的通道"
        assert s.tts_types == ["chat_tts_text"], (
            f"tts_type 变了：{s.tts_types}。_book_her_turn 按这个字段分辨我们的轮次和她的，"
            "变了就会把我们点的回复当成她自起的轮送进语音门"
        )


async def _push_silence(s: _Session, seconds: float) -> None:
    """The streamer present but quiet. The uplink contract is never to stop."""
    packet = base64.b64encode(b"\x00" * 640).decode()
    for _ in range(int(seconds * 50)):
        await s.send({"type": "input_audio_buffer.append", "audio": packet})
        await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    ("shape", "items"),
    [
        ("lone-user", [{"role": "user", "text": "暗号是青色菠萝八十一号"}]),
        (
            "documented-pair",
            [
                {"role": "user", "text": "暗号是青色菠萝八十一号"},
                {"role": "assistant", "text": "好的，记住了，青色菠萝八十一号"},
            ],
        ),
    ],
    ids=["lone-user", "documented-pair"],
)
async def test_a_mid_session_context_item_never_reaches_the_model(
    tmp_path: Path, shape: str, items: list[dict[str, str]]
) -> None:
    """Why `add_context_item` stashes instead of sending, pinned at last.

    The adapter has said since 2026-09-16 that a mid-session
    `conversation.item.create` is accepted and ignored, and that history only
    reaches the model at `session.create`. That claim had no test: the two
    contract tests that send this event both use the FUNCTION-CALL shape
    (`role: "tool"`), which is a different thing entirely.

    Leaving it untested was a risk worth closing, because the vendor's 接入必读
    says the opposite in as many words — 「初始化历史：通过
    conversation.item.create 注入历史问答对」 — and this protocol has already
    punished us twice for sending a legal-looking shape that gets dropped whole
    (odd-length dialog_context, and five near-miss function-call payloads).

    Measured 2026-09-21, both shapes, one socket each. The documented even
    user/assistant pair IS accepted — `conversation.item.added` comes back,
    where a lone user item draws no ack at all — and she still answers 「你没
    说过具体暗号内容」. So the shape decides acceptance and neither decides
    what she knows.

    If this ever goes red the stash in `add_context_item` can go, and with it
    debt #105: history would stop having to ride a reconnect.
    """
    async for s in _open(tmp_path):
        await s.send(
            {
                "type": "conversation.item.create",
                "event_id": "hist",
                "items": [
                    {
                        "type": "message",
                        "role": i["role"],
                        "content": [{"type": "input_text", "text": i["text"]}],
                    }
                    for i in items
                ],
            }
        )
        await s.collect(2)
        await s.speak("刚才提到的暗号是什么？只说暗号")
        await s.collect(12)
        assert "菠萝" not in s.said and "八十一" not in s.said, (
            f"她居然读到了中途注入的上下文（{shape}）：{s.said!r}。"
            "那 add_context_item 就不必再攒着了，欠账 #105 也跟着消失。"
        )
