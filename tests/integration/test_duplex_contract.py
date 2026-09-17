"""Contract tests against the real Seeduplex 3.0 endpoint (model 1.2.6.1).

These are the seventeen throwaway probes of 2026-09-16, kept. They are here
rather than in /tmp because of how that day went: the same conclusion was
reached three times on evidence that turned out invalid, and each time the
proof lived in a shell scrollback nobody could re-run. What the file pins is
not the adapter — unit tests do that against the fake — but the ENDPOINT's
behaviour, so the day the vendor changes its mind we find out from a red test
instead of from a stream where she has stopped answering danmaku.

The three facts everything else rests on, and how each one bit:

1. **She composes, she cannot start.** Text we supply reaches her by two
   routes (dialog_context at create, a function-call result mid-session) and
   she answers it in her own words. Nothing we can send starts a turn. The
   first two rounds of probes concluded "she cannot compose" — both had fed
   her through channels she never reads.
2. **dialog_context must be even in length.** The endpoint pairs it
   positionally and ignores `role`; an odd array is discarded whole while
   `session.created` comes back unchanged. The adapter's `add_context_item`
   used to stash danmaku as a run of lone user turns, i.e. the exact shape
   that wipes the history, with nothing on the wire to say so.
3. **The uplink vocabulary is closed.** An unknown event name is a 1006, not
   an error frame. `response.create` is not specially refused; it simply does
   not exist here.

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
            elif kind == "conversation.item.input_audio_transcription.completed":
                self.asr_final = str(frame.get("transcript") or "")
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
    above proves it), and still nothing comes back until a voice does."""
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
async def test_an_event_outside_the_vocabulary_hangs_up(tmp_path: Path, name: str) -> None:
    """A 1006, not an error frame. Worth pinning because it decides how the
    adapter may probe: there is no cheap way to ask "do you support X"."""
    async for s in _open(tmp_path):
        await s.send({"type": name, "event_id": "unknown"})
        await s.collect(6)
        assert s.closed, f"{name} 居然没把连接掐了：{dict(s.kinds)}"


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


async def test_the_streamers_words_only_exist_in_the_last_delta(tmp_path: Path) -> None:
    """Two endpoint facts the adapter has to rebuild the transcript from.

    The deltas are CUMULATIVE — 「你」「你叫」「你叫什么」 — and `completed`
    carries an empty `transcript`. So the last delta is the only copy of what
    the streamer said, and it is what `UserTranscriptDone` is built from. The
    chat page, the console and `on_transcript` all key on that event, so if
    this rebuild is wrong the streamer's whole half of the conversation goes
    missing while hers keeps arriving.
    """
    said = "今天打算打几把游戏"
    async for s in _open(tmp_path):
        await s.speak(said)
        await s.collect(10)
        assert len(s.asr) >= 2, f"转写块太少，没法判断累积与否：{s.asr}"
        # Cumulative, but the tail flaps — 「…游戏」「…游戏？」「…游戏」 in one
        # run, the transcriber revising its punctuation. So the invariant is
        # not "each grows on the last" but "each is a prefix of the last",
        # which is what makes the final delta the whole sentence.
        final = s.asr[-1]
        for i, block in enumerate(s.asr):
            assert final.startswith(block), (
                f"第 {i} 块 {block!r} 不是最后一块 {final!r} 的前缀——"
                "adapter 是靠这个重建整句的，这条变了就得改"
            )
        assert s.asr_final == "", (
            f"completed 里居然带了 transcript={s.asr_final!r}——"
            "那就可以直接用它，不必再从最后一块 delta 重建"
        )
        assert any(
            word in s.asr[-1] for word in ("游戏", "几把", "今天")
        ), f"最后一块 delta 应该是整句，实际是 {s.asr[-1]!r}"


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
