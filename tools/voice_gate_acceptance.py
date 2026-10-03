"""Acceptance run for the voice gate on a real backend (plan P6, scripted).

The runbook's live-mock acceptance needs a person in Chrome sharing a tab.
This measures the same thing without one: the real link (s2s / DashScope /
volcano), the real fan-out, gate and scheduler — wired the way dev-talk
wires them — with the streamer's lines synthesised by macOS `say` and pushed
through push_audio at real-time pace, and the gated view standing in for
the speakers. For every line it records what she wrote at the head, what
the gate did and whether any audio reached the speakers, then scores the
run against one binary label: did the room need to hear her.

Usage::

    source path.sh
    .venv/bin/python tools/voice_gate_acceptance.py --provider dashscope
    .venv/bin/python tools/voice_gate_acceptance.py --provider s2s      # server on :8765
    .venv/bin/python tools/voice_gate_acceptance.py --provider volcano
    .venv/bin/python tools/voice_gate_acceptance.py --provider volcano_duplex

    # One class only, a candidate prompt, results kept for comparison:
    .venv/bin/python tools/voice_gate_acceptance.py --provider volcano_duplex \
        --only-quiet --prompt-dir /tmp/candidate --json /tmp/run1.json

One score, 该不该说判对, plus two error columns kept apart because they cost
different things: 该接没接 (she stayed silent when spoken to) and
不该接却接了 (she talked over the streamer). A turn with no output at all —
cut, failed or empty — is a third column and enters neither rate.

Two things this run cannot do, learned the hard way on 2026-09-09:

It cannot compare two prompts across time. The endpoint's working point
drifts: the same script's run-level failure rate moved from 31% to 100%
inside an hour. A/B means interleaving the arms in one window, at least
eight independent runs each, compared run by run — never one arm now and
the other after lunch.

It cannot stand in for real audio. `say` gives clean speech and this pushes
2.5 s of DIGITAL ZERO after each line; a live stream is tab audio that never
reaches zero. Five controlled runs built on that difference failed to
reproduce a production symptom. Use --replay with a recorded uplink
(dev-talk --record-uplink) for the fidelity half.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bilisama.clock import SystemClock
from bilisama.config.enums import VoiceReplyMode
from bilisama.config.loader import load
from bilisama.dev_talk import _Fanout
from bilisama.director.floor import SpeakingFloor
from bilisama.director.scheduler import Scheduler
from bilisama.director.turn_protocol import Ruling, TurnPolicy
from bilisama.director.voice_turn import Skip, VoiceTurnGate
from bilisama.obs.outcome import SkipReason
from bilisama.persona.loader import PersonaStore, live_voice_rules, template_variables
from bilisama.persona.prompt import assemble_scoped, static_prefix
from bilisama.realtime import link
from bilisama.realtime.providers import resolve_endpoint
from bilisama.realtime.providers.factory import LinkRequest, build_link

_REPO = Path(__file__).resolve().parents[1]
_CONFIG = _REPO / "config"
_FRAME = 640  # 20 ms of 16 kHz mono s16le
_TAIL_S = 2.5  # silence after each line, so the server's VAD closes the turn
_REPLY_TIMEOUT_S = 30.0
# A replay ends when the recording does, mid-turn as often as not; this is
# how long to keep listening afterwards for the reply already in flight.
_REPLAY_DRAIN_S = 15.0
# A --model string only reaches the config field through this table, which is
# what lets the field keep its Literal type instead of an unchecked cast.
_VOLCANO_GENERATIONS: dict[str, Literal["1.2.1.1", "2.2.0.0"]] = {
    "1.2.1.1": "1.2.1.1",
    "2.2.0.0": "2.2.0.0",
}

# The streamer's lines. Punctuation is kept out of the spoken text: `say`
# pauses on it and the pause splits one line into two turns
# (tests/integration/test_real_server.py's note).
#
# The label is BINARY — did the room need to hear her — because that is the
# only thing the gate decides (TurnPolicy.speak is {TO_ME}) and the only
# thing a listener can check. The scene she reports is printed, never scored:
# telling AUDIENCE from READING costs the model attention and buys the
# product nothing.


@dataclass(frozen=True, slots=True)
class Line:
    """One scored line, and what boundary it is there to test.

    setup lines are spoken first and left unscored: they exist to put the
    conversation in the state the scored line needs — she has just said
    something, or the streamer has just turned to someone else. Injecting
    that state as history instead would test a session production never has.
    """

    text: str
    should_answer: bool
    why: str
    setup: tuple[str, ...] = ()
    # Quoted from a real recording of the class production was failing on.
    # This is the bucket the acceptance reports separately, and it is a TAG,
    # not a length: the bucket used to be `len(text) <= 8`, which swept in
    # two lines that were never recording fragments, missed one that was,
    # and silently changed size when the set was decontaminated (7 → 6).
    weak_cue: bool = False
    # The 「让她别出声」 boundary and its controls (2026-09-24): the streamer
    # tells her to be quiet, which is addressed to her AND wants silence.
    # duplex answered 「收到，立刻静音」 to one of these live. --only-quiet runs
    # this bucket alone, which is what an A/B on that boundary needs.
    quiet: bool = False


# No line here may be a sentence the prompt's own examples use. A model that
# only pattern-matches the examples scores on those and nowhere else, and two
# of the three tags volcano wrote on the first version of this set were
# example text — which made 18% unreadable as a capability. test_persona.py
# holds the two apart.
#
# Weak-cue lines are quoted from the 2026-09-09 recording. The scripted set
# that preceded this one scored 95%-100% while production sat at 26%-49%,
# and the reason was here: every line meant for her said 豆腐, and every line
# that was not carried 家人们 or 谢谢小明. Those are the easy half.
LINES: tuple[Line, ...] = (
    # --- 强线索：留几条当对照，好看出难易分差
    Line("豆腐 你觉得今天的天气怎么样", True, "强线索正例：点名 + 直接提问"),
    Line("各位观众朋友们大家好 欢迎来到直播间", False, "强线索反例：明确对观众"),
    Line("谢谢小明送的火箭 谢谢谢谢", False, "强线索反例：谢礼物"),
    Line("哎呀 这个怎么又卡住了", False, "强线索反例：典型自语"),
    # --- 省略名字的正例：现在的验收集一条都没有
    Line(
        "那你最受不了哪一种",
        True,
        "接着她上一句追问，全程没点名",
        setup=("豆腐 你平时喜欢什么样的观众",),
    ),
    Line(
        "那你觉得哪个更好",
        True,
        "延续正在进行的对话，没点名",
        setup=("豆腐 帮我在这两个方案里挑一个",),
    ),
    Line("等下我去倒杯水 你先陪他们聊两句", True, "直接交代事情，没点名"),
    Line("给大家讲个笑话吧", True, "没点名，但只有她能执行；出现「大家」不等于略过"),
    # --- 多轮转向
    Line(
        "那你那边几点能上线",
        False,
        "上一句已转向连麦的阿远，这句还是问他",
        setup=("阿远 我们对一下周末的安排",),
    ),
    Line(
        "豆腐 那你说说看",
        True,
        "跟别人说完之后点名转回她",
        setup=("阿远 你那边什么时候方便",),
    ),
    # --- 引用后转交
    Line("弹幕在问咱们今天几点下播", False, "念出一个问题，不是转交"),
    Line("弹幕在问咱们今天几点下播 这个你替我答", True, "念完之后明确转交"),
    Line("有人问主播用的什么键盘 我用的机械键盘", False, "念问题并自己答了"),
    # --- 出现她的名字但不是在叫她
    Line("你们说豆腐这个反应是不是有点慢", False, "提到她的名字，但在问观众"),
    # --- 明确要求先听
    Line("豆腐 这段你别插话 让我讲完这个事", False, "明确说给她听，但本轮要求安静", quiet=True),
    # --- 让她别出声，问句也算（2026-09-24 duplex 实盘：第二遍她回了「收到，立刻静音」）
    Line("你能给我闭嘴吗", False, "问句形式的「别出声」，照做就是不出声", quiet=True),
    Line(
        "你可以闭嘴吗",
        False,
        "刚让她别出声、她已经先听了，又说一遍",
        setup=("豆腐 你先别出声",),
        quiet=True,
    ),
    Line("豆腐 你先安静一会儿", False, "点名让她安静", quiet=True),
    Line("能不能别说了 吵死了", False, "嫌吵、让停下，没点名", quiet=True),
    # 对照：带「说」「别」但不是让她闭嘴，也不能被一并压掉
    Line("别光说了 给大家唱两句", True, "让她换一件事做，不是让她安静", quiet=True),
    Line(
        "好了 你可以说话了 刚才那局你怎么看",
        True,
        "安静之后明确让她开口，得恢复接话",
        setup=("豆腐 你先别说话",),
        quiet=True,
    ),
    Line("你怎么不说话了", True, "问她为什么不吭声，是在跟她说话", quiet=True),
    # 留出来的几句：用词在提示词里一个都没出现过，测的是判断，不是背例子
    Line("豆腐 你嘴歇会儿行不行", False, "没教过的说法：让她停嘴", quiet=True),
    Line("行了行了 憋着吧", False, "没教过的说法：不耐烦地让停", quiet=True),
    Line(
        "你能不能歇会儿嘴",
        False,
        "没教过的说法：刚让她停过，又说一遍",
        setup=("豆腐 先憋会儿",),
        quiet=True,
    ),
    Line("你可以说得详细一点吗", True, "问句加「说」，但是要她多说，不能被压掉", quiet=True),
    # --- 弱线索碎句：实盘失灵段的原话
    Line("在这里", False, "真实录音里的碎句，几乎没有线索", weak_cue=True),
    Line("上去", False, "同上，两个字的自语", weak_cue=True),
    Line("他肯定上大嘴鸥", False, "游戏解说，说给观众或自己", weak_cue=True),
    Line("等没有了雨天 你看我怎么干他", False, "带光秃秃的「你」，但说的是对手", weak_cue=True),
    Line("这个大流还挺快呀", False, "游戏解说里的感叹", weak_cue=True),
    Line("锤大队友", False, "两三个字的操作嘀咕", weak_cue=True),
    # --- 判断不出交流对象
    Line("这样行不行啊", False, "没有上下文，判断不出在跟谁说，应当先听"),
)


@dataclass(slots=True)
class Result:
    line: Line
    head: str = ""
    ruling: Ruling | None = None
    skipped: bool = False
    audio_frames: int = 0
    status: str = "no_reply"
    seconds: float = 0.0

    @property
    def spoke(self) -> bool:
        """Audio reached the speakers. The only thing the room can tell."""
        return self.audio_frames > 0

    @property
    def no_output(self) -> bool:
        """Nothing was heard and the gate is not why.

        A turn the provider cut, failed or ended empty. Counting these as
        leaks is what made the ad-hoc harnesses read 81% worse than they
        were, so they get their own column and stay out of both error rates.
        """
        return not self.spoke and not self.skipped

    @property
    def decided_right(self) -> bool:
        """She spoke exactly when the line needed her."""
        if self.no_output:
            return False
        return self.spoke == self.line.should_answer

    @property
    def over_skip(self) -> bool:
        """The line needed her and the gate held it back."""
        return self.line.should_answer and self.skipped

    @property
    def under_skip(self) -> bool:
        """The line was not for her and the room heard her anyway."""
        return not self.line.should_answer and self.spoke


@dataclass(slots=True)
class _Books:
    """What the wiring saw, per reply handle."""

    skips: dict[int, Skip] = field(default_factory=dict)
    audio: dict[int, int] = field(default_factory=dict)
    dones: list[link.ReplyDone] = field(default_factory=list)
    started: list[int] = field(default_factory=list)
    # Replay only: heard-and-said in arrival order, so a recorded session can
    # be read as a conversation instead of a pile of handles.
    timeline: list[tuple[str, int, str]] = field(default_factory=list)


def _say(text: str) -> bytes:
    """16 kHz mono s16le PCM of the line, via the macOS synthesiser."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "line.wav"
        subprocess.run(
            ["say", "-v", "Tingting", "-o", str(out), "--data-format=LEI16@16000", text],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with wave.open(str(out)) as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1:
                raise SystemExit("say 没按 16 kHz 单声道输出，换个 --data-format 试试")
            pcm: bytes = w.readframes(w.getnframes())
            return pcm


def _load_wav(path: Path) -> bytes:
    """A recorded uplink, in the one format the uplink itself uses."""
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(
                f"{path} 是 {w.getframerate()}Hz/{w.getnchannels()}声道/"
                f"{w.getsampwidth() * 8}bit，回放必须和上行同格式：16kHz 单声道 16bit"
            )
        pcm: bytes = w.readframes(w.getnframes())
        if not pcm:
            raise SystemExit(f"{path} 是空的，没有可回放的音频")
        return pcm


class _Uplink:
    """The line pusher, and — when steady — the silence between lines.

    Production's microphone never stops: dev-talk pushes a frame every 20 ms,
    silence included. This tool used to push a line and its 2.5 s tail and
    then nothing until the next line, which DashScope does not mind and
    volcano_duplex does: its end-of-speech edge went missing and turns hung
    to the watchdog (2026-09-24, three of eight lines with no reply at all).
    `steady` keeps a zero frame going whenever no line is being pushed.
    """

    def __init__(self, speech: link.SpeechLink, *, steady: bool) -> None:
        self._speech = speech
        self._steady = steady
        self._busy = asyncio.Lock()

    async def push(self, pcm: bytes) -> None:
        async with self._busy:
            for i in range(0, len(pcm), _FRAME):
                await self._speech.push_audio(pcm[i : i + _FRAME])
                await asyncio.sleep(0.02)

    async def idle_fill(self) -> None:
        if not self._steady:
            return
        zero = b"\x00" * _FRAME
        while True:
            if not self._busy.locked():
                await self._speech.push_audio(zero)
            await asyncio.sleep(0.02)


async def _push(uplink: _Uplink, pcm: bytes) -> None:
    await uplink.push(pcm)


async def _watch_raw(view: AsyncIterator[link.LinkEvent], books: _Books) -> None:
    async for event in view:
        if isinstance(event, link.ReplyStarted) and event.handle.implicit:
            books.started.append(event.handle.handle_id)
        elif isinstance(event, link.ReplyDone) and event.handle.implicit:
            books.dones.append(event)
            books.timeline.append(("said", event.handle.handle_id, event.text.strip()))
        elif isinstance(event, link.UserTranscriptDone) and event.text.strip():
            books.timeline.append(("heard", -1, event.text.strip()))


async def _watch_speakers(view: AsyncIterator[link.LinkEvent], books: _Books) -> None:
    async for event in view:
        if isinstance(event, link.ReplyAudioDelta):
            books.audio[event.handle.handle_id] = books.audio.get(event.handle.handle_id, 0) + 1


def _instructions(persona: PersonaStore, variables: dict[str, str], prompt_dir: Path) -> str:
    """The session instructions: anchors, then the voice contract.

    `prompt_dir` stands where config/ does for the two voice files only
    (personas/live/voice_responses.md and voice_addressing.md), so a candidate
    contract can be measured against the shipped one without editing it.
    """
    rules = live_voice_rules(prompt_dir, variables, addressing=True)
    return assemble_scoped(static_prefix(persona.anchors(variables)), rules)


def _write_json(path: Path, provider: str, prompt_dir: Path, results: list[Result]) -> None:
    """One run, machine-readable, so interleaved A/B arms can be tallied."""
    rows = [
        {
            "text": r.line.text,
            "should_answer": r.line.should_answer,
            "quiet": r.line.quiet,
            "head": r.head,
            "skipped": r.skipped,
            "spoke": r.spoke,
            "no_output": r.no_output,
            "decided_right": r.decided_right,
            "status": r.status,
            "seconds": round(r.seconds, 2),
        }
        for r in results
    ]
    payload = {"provider": provider, "prompt_dir": str(prompt_dir), "results": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _setup_turn(uplink: _Uplink, books: _Books, pcm: bytes, silence: bytes) -> None:
    """Speak a line and wait it out without scoring it.

    The scored line needs the conversation in a particular state — she has
    just spoken, or the streamer has just turned to someone else — and the
    only faithful way to get there is to let the turn happen.
    """
    dones_before = len(books.dones)
    deadline = time.monotonic() + _REPLY_TIMEOUT_S
    await _push(uplink, pcm)
    await _push(uplink, silence)
    while time.monotonic() < deadline:
        for done in books.dones[dones_before:]:
            hid = done.handle.handle_id
            if hid in books.skips or done.status is link.ReplyStatus.COMPLETED:
                await asyncio.sleep(1.0)  # let the turn settle before the next
                return
        await asyncio.sleep(0.05)


async def _one_line(
    uplink: _Uplink, books: _Books, line: Line, pcm: bytes, silence: bytes
) -> Result:
    result = Result(line=line)
    dones_before = len(books.dones)
    started_at = time.monotonic()
    await _push(uplink, pcm)
    await _push(uplink, silence)
    deadline = started_at + _REPLY_TIMEOUT_S
    while time.monotonic() < deadline:
        for done in books.dones[dones_before:]:
            hid = done.handle.handle_id
            if hid in books.skips or done.status is link.ReplyStatus.COMPLETED:
                await asyncio.sleep(1.0)  # late frames for this handle
                skip = books.skips.get(hid)
                result.head = done.text.strip()[:30]
                result.ruling = skip.ruling if skip is not None else None
                result.skipped = skip is not None
                result.audio_frames = books.audio.get(hid, 0)
                result.status = str(done.status)
                result.seconds = time.monotonic() - started_at
                return result
        await asyncio.sleep(0.05)
    result.seconds = time.monotonic() - started_at
    return result


def _render_replay(books: _Books) -> str:
    """A replay has no expected answers — only what was heard, said and gated.

    Ground truth would have to be hand-labelled per utterance, and the point
    of a replay is the audio, not the labels: if a recorded session writes
    markers here but not in production, the difference is no longer the audio.
    """
    rows: list[str] = []
    spoke = 0
    silenced = 0
    no_output = 0
    for kind, handle_id, text in books.timeline:
        if kind == "heard":
            rows.append(f"  主播说  {text}")
            continue
        frames = books.audio.get(handle_id, 0)
        skip = books.skips.get(handle_id)
        if skip is not None:
            silenced += 1
            ruling = skip.ruling
            verdict = f"拦下 {ruling.category.value if ruling is not None else '残记号'}"
        elif frames > 0:
            # Audio, not a bracket, is the leak: a reply the provider cut or
            # ended empty passes the gate and is still never heard.
            spoke += 1
            verdict = f"出声 {frames}帧"
        else:
            no_output += 1
            verdict = "无输出"
        rows.append(f"  她 [{verdict}] {text[:70]}")
    rows.append("")
    rows.append(
        f"她自起的回复 {spoke + silenced + no_output} 条："
        f"出声 {spoke} 条，门拦下 {silenced} 条，无输出 {no_output} 条"
    )
    return "\n".join(rows)


def _render(results: list[Result]) -> str:
    rows = ["#  | 该接吗 | 她写的开头                     | 结果   | 音频帧 | 判定 | 测的是什么"]
    for i, r in enumerate(results, 1):
        want = "要接" if r.line.should_answer else "别接"
        if r.skipped:
            got = "拦下"
        elif r.spoke:
            got = "出声"
        else:
            got = "无输出"
        if r.no_output:
            verdict = "—"
        elif r.over_skip:
            verdict = "✗ 该接没接"
        elif r.under_skip:
            verdict = "✗ 不该接却接了"
        else:
            verdict = "✓"
        cells = (f"{i:<2}", f"{want:<6}", f"{r.head:<30}", f"{got:<4}")
        rows.append(" | ".join(cells) + f" | {r.audio_frames:>5} | {verdict:<14} | {r.line.why}")
    return "\n".join(rows)


async def _run(
    provider: str,
    model: str,
    voice: str,
    replay: Path | None = None,
    *,
    prompt_dir: Path = _CONFIG,
    only_quiet: bool = False,
    json_out: Path | None = None,
    steady_uplink: bool = False,
) -> int:
    logging.basicConfig(level=logging.WARNING)
    lines = tuple(line for line in LINES if line.quiet) if only_quiet else LINES
    # duplex without a steady uplink is not a measurement: it waits for
    # trailing silence before finishing a reply, and a third of the lines came
    # back with no output at all (2026-09-24).
    steady_uplink = steady_uplink or provider == "volcano_duplex"
    settings = load(_CONFIG / "bilisama.toml")
    endpoint = resolve_endpoint(
        settings, provider=provider, url=None, model=model or None, env=os.environ
    )
    persona = PersonaStore.from_config(settings.persona, config_dir=_CONFIG)
    variables = template_variables(settings.persona, reply_length=settings.interaction.reply_length)
    if model and provider == "volcano":
        # The volcano generation is config, not part of the address: its model
        # picks which persona field the session uses (system_role on O2.0,
        # character_manifest on SC2.0), and that turns out to decide whether
        # the marker contract is obeyed at all. Voice must move with it.
        generation = _VOLCANO_GENERATIONS.get(model)
        if generation is None:
            raise SystemExit("火山的 --model 只有 1.2.1.1（O2.0）和 2.2.0.0（SC2.0）")
        settings.speech.volcano.model = generation
        if voice:
            settings.speech.volcano.speaker = voice
    built = build_link(
        LinkRequest(
            endpoint=endpoint,
            settings=settings,
            voice=voice if provider != "volcano" else "",
            bot_name=variables["agentName"],
            model_explicit=bool(model),
            env=os.environ,
            text_replies=False,
        )
    )
    detail = f"  模型：{model}" if model else ""
    if provider == "volcano":
        detail += f"  音色：{settings.speech.volcano.speaker}"
    print(f"后端：{endpoint.provider.value}{detail}  地址：{built.url}  提示词：{prompt_dir}")
    audio: dict[str, bytes] = {}
    silence = b""
    recorded = b""
    if replay is None:
        print("合成台词…", end="", flush=True)
        wanted = {line.text for line in lines} | {t for line in lines for t in line.setup}
        audio = {text: _say(text) for text in sorted(wanted)}
        silence = b"\x00" * int(16000 * _TAIL_S) * 2
        setups = sum(len(line.setup) for line in lines)
        print(f" {len(lines)} 句评分 + {setups} 句铺垫")
    else:
        recorded = _load_wav(replay)
        print(f"回放 {replay}：{len(recorded) / 32000:.0f} 秒，按实时速度推")

    clock = SystemClock()
    speech = _Fanout(built.link)
    await speech.connect()
    floor = SpeakingFloor(clock)
    scheduler = Scheduler(speech, floor, clock)
    books = _Books()

    def on_skip(skip: Skip) -> None:
        books.skips[skip.handle.handle_id] = skip
        ruling = skip.ruling
        scheduler.skip_implicit(
            skip.handle,
            reason=SkipReason.VOICE_NOT_ADDRESSED,
            detail=ruling.detail() if ruling is not None else "残记号",
            clear_playback=skip.clear_playback,
        )

    gate = VoiceTurnGate(
        clock, policy=TurnPolicy(), mode=VoiceReplyMode.WHEN_ADDRESSED, on_skip=on_skip
    )
    speech.set_gate(gate)
    raw_view, gated_view = speech.events(), speech.gated_events()
    speech.start()
    uplink = _Uplink(speech, steady=steady_uplink)
    tasks = [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(_watch_raw(raw_view, books)),
        asyncio.create_task(_watch_speakers(gated_view, books)),
        asyncio.create_task(uplink.idle_fill()),
    ]
    results: list[Result] = []
    try:
        await speech.set_context(_instructions(persona, variables, prompt_dir))
        await asyncio.sleep(1.0)
        if replay is not None:
            await _push(uplink, recorded)
            print(f"音频推完，再等 {_REPLAY_DRAIN_S:.0f} 秒收最后一轮…")
            await asyncio.sleep(_REPLAY_DRAIN_S)
        else:
            for line in lines:
                for cue in line.setup:
                    await _setup_turn(uplink, books, audio[cue], silence)
                result = await _one_line(uplink, books, line, audio[line.text], silence)
                results.append(result)
                want = "要接" if line.should_answer else "别接"
                got = "拦下" if result.skipped else ("出声" if result.spoke else "无输出")
                print(f"{len(results):>2}/{len(lines)} {want} → {got:<4} {result.head}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await speech.aclose()

    if replay is not None:
        print()
        print(_render_replay(books))
        print(f"门：{gate.status()}")
        return 0

    if json_out is not None:
        _write_json(json_out, endpoint.provider.value, prompt_dir, results)
    print()
    print(_render(results))
    scored = [r for r in results if not r.no_output]
    total = len(scored)
    if not total:
        print("\n每一句都没有输出，这一轮不成立——先查链路，别读下面的数")
        return 1
    decided = sum(r.decided_right for r in scored)
    over = sum(r.over_skip for r in scored)
    under = sum(r.under_skip for r in scored)
    dead = len(results) - total
    hard = [r for r in scored if r.line.weak_cue]
    quiet = [r for r in scored if r.line.quiet]
    print()
    print(
        f"该不该说判对 {decided}/{total} = {decided / total:.0%}   "
        f"该接没接 {over}   不该接却接了 {under}"
    )
    if hard:
        hard_ok = sum(r.decided_right for r in hard)
        print(
            f"其中弱线索碎句 {hard_ok}/{len(hard)} = {hard_ok / len(hard):.0%}（实盘就栽在这一类）"
        )
    if quiet and not only_quiet:
        quiet_ok = sum(r.decided_right for r in quiet)
        print(f"其中「让她别出声」一类 {quiet_ok}/{len(quiet)} = {quiet_ok / len(quiet):.0%}")
    print(f"无输出 {dead} 句（被取消或空回复，不计入上面两个错误率）")
    print(f"门：{gate.status()}")
    mean_s = sum(r.seconds for r in results) / len(results)
    print(f"调度器判决 {len(scheduler.verdicts)} 条，平均每句 {mean_s:.1f} 秒")
    passed = decided / total >= 0.8 and over <= 1
    print("放行" if passed else "不放行")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语音门验收：真实后端、合成台词、逐句对照")
    parser.add_argument(
        "--provider", required=True, choices=["s2s", "dashscope", "volcano", "volcano_duplex"]
    )
    parser.add_argument("--model", default="", help="覆盖模型；火山上它决定人设走哪个字段")
    parser.add_argument("--voice", default="", help="覆盖音色；火山上必须和模型代际配对")
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="不合成台词，改回放一段 dev-talk --record-uplink 录下的上行 WAV："
        "同一份指令、同一个门，换成真实直播的音频",
    )
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        default=_CONFIG,
        help="换一版语音合同来测：目录下要有 personas/live/voice_responses.md 和 "
        "voice_addressing.md，默认就是 config/ 里出货的那份",
    )
    parser.add_argument(
        "--only-quiet", action="store_true", help="只跑「让她别出声」这一类台词和它的对照"
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="逐句结果另存一份 JSON，交错对比两版时汇总用"
    )
    parser.add_argument(
        "--steady-uplink",
        action="store_true",
        help="句与句之间一直推静音帧，像生产的麦克风一样不断流；duplex 不加会丢判停、整句无输出",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            args.provider,
            args.model,
            args.voice,
            args.replay,
            prompt_dir=args.prompt_dir,
            only_quiet=args.only_quiet,
            json_out=args.json,
            steady_uplink=args.steady_uplink,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
