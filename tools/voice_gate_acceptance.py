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
        "为什么",
        True,
        "接着她上一句追问，全程没点名",
        setup=("豆腐 你喜欢什么样的故事",),
    ),
    Line(
        "那你觉得哪个更好",
        True,
        "延续正在进行的对话，没点名",
        setup=("豆腐 帮我在这两个方案里挑一个",),
    ),
    Line("帮我盯着点弹幕 有人问问题就喊我", True, "直接交代事情，没点名"),
    Line("给大家讲个笑话吧", True, "没点名，但只有她能执行；出现「大家」不等于略过"),
    # --- 多轮转向
    Line(
        "你明天有空吗",
        False,
        "上一句已转向连麦的阿远，这句还是问他",
        setup=("阿远 我们对一下明天的时间",),
    ),
    Line(
        "豆腐 那你说说看",
        True,
        "跟别人说完之后点名转回她",
        setup=("阿远 你那边什么时候方便",),
    ),
    # --- 引用后转交
    Line("这条弹幕问你喜欢夏天还是冬天", False, "念出一个问题，不是转交"),
    Line("这条弹幕问你喜欢夏天还是冬天 这题你自己答", True, "念完之后明确转交"),
    Line("有人问主播用的什么键盘 我用的机械键盘", False, "念问题并自己答了"),
    # --- 出现她的名字但不是在叫她
    Line("大家觉得豆腐刚才说得怎么样", False, "提到她的名字，但在问观众"),
    # --- 明确要求先听
    Line("豆腐 先别接 我把话说完", False, "明确说给她听，但本轮要求安静"),
    # --- 弱线索碎句：实盘失灵段的原话
    Line("在这里", False, "真实录音里的碎句，几乎没有线索"),
    Line("上去", False, "同上，两个字的自语"),
    Line("他肯定上大嘴鸥", False, "游戏解说，说给观众或自己"),
    Line("等没有了雨天 你看我怎么干他", False, "带光秃秃的「你」，但说的是对手"),
    Line("这个大流还挺快呀", False, "游戏解说里的感叹"),
    Line("锤大队友", False, "两三个字的操作嘀咕"),
    # --- 判断不出交流对象
    Line("你觉得呢", False, "没有上下文，判断不出在跟谁说，应当先听"),
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


async def _push(speech: link.SpeechLink, pcm: bytes) -> None:
    for i in range(0, len(pcm), _FRAME):
        await speech.push_audio(pcm[i : i + _FRAME])
        await asyncio.sleep(0.02)


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


def _instructions(persona: PersonaStore, variables: dict[str, str]) -> str:
    rules = live_voice_rules(_CONFIG, variables, addressing=True)
    return assemble_scoped(static_prefix(persona.anchors(variables)), rules)


async def _setup_turn(speech: link.SpeechLink, books: _Books, pcm: bytes, silence: bytes) -> None:
    """Speak a line and wait it out without scoring it.

    The scored line needs the conversation in a particular state — she has
    just spoken, or the streamer has just turned to someone else — and the
    only faithful way to get there is to let the turn happen.
    """
    dones_before = len(books.dones)
    deadline = time.monotonic() + _REPLY_TIMEOUT_S
    await _push(speech, pcm)
    await _push(speech, silence)
    while time.monotonic() < deadline:
        for done in books.dones[dones_before:]:
            hid = done.handle.handle_id
            if hid in books.skips or done.status is link.ReplyStatus.COMPLETED:
                await asyncio.sleep(1.0)  # let the turn settle before the next
                return
        await asyncio.sleep(0.05)


async def _one_line(
    speech: link.SpeechLink, books: _Books, line: Line, pcm: bytes, silence: bytes
) -> Result:
    result = Result(line=line)
    dones_before = len(books.dones)
    started_at = time.monotonic()
    await _push(speech, pcm)
    await _push(speech, silence)
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


async def _run(provider: str, model: str, voice: str, replay: Path | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
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
    print(f"后端：{endpoint.provider.value}{detail}  地址：{built.url}")
    audio: dict[str, bytes] = {}
    silence = b""
    recorded = b""
    if replay is None:
        print("合成台词…", end="", flush=True)
        wanted = {line.text for line in LINES} | {t for line in LINES for t in line.setup}
        audio = {text: _say(text) for text in sorted(wanted)}
        silence = b"\x00" * int(16000 * _TAIL_S) * 2
        setups = sum(len(line.setup) for line in LINES)
        print(f" {len(LINES)} 句评分 + {setups} 句铺垫")
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
    tasks = [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(_watch_raw(raw_view, books)),
        asyncio.create_task(_watch_speakers(gated_view, books)),
    ]
    results: list[Result] = []
    try:
        await speech.set_context(_instructions(persona, variables))
        await asyncio.sleep(1.0)
        if replay is not None:
            await _push(speech, recorded)
            print(f"音频推完，再等 {_REPLAY_DRAIN_S:.0f} 秒收最后一轮…")
            await asyncio.sleep(_REPLAY_DRAIN_S)
        else:
            for line in LINES:
                for cue in line.setup:
                    await _setup_turn(speech, books, audio[cue], silence)
                result = await _one_line(speech, books, line, audio[line.text], silence)
                results.append(result)
                want = "要接" if line.should_answer else "别接"
                got = "拦下" if result.skipped else ("出声" if result.spoke else "无输出")
                print(f"{len(results):>2}/{len(LINES)} {want} → {got:<4} {result.head}")
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
    hard = [r for r in scored if not r.line.should_answer and len(r.line.text) <= 8]
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
    print(f"无输出 {dead} 句（被取消或空回复，不计入上面两个错误率）")
    print(f"门：{gate.status()}")
    mean_s = sum(r.seconds for r in results) / len(results)
    print(f"调度器判决 {len(scheduler.verdicts)} 条，平均每句 {mean_s:.1f} 秒")
    passed = decided / total >= 0.8 and over <= 1
    print("放行" if passed else "不放行")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语音门验收：真实后端、合成台词、逐句对照")
    parser.add_argument("--provider", required=True, choices=["s2s", "dashscope", "volcano"])
    parser.add_argument("--model", default="", help="覆盖模型；火山上它决定人设走哪个字段")
    parser.add_argument("--voice", default="", help="覆盖音色；火山上必须和模型代际配对")
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="不合成台词，改回放一段 dev-talk --record-uplink 录下的上行 WAV："
        "同一份指令、同一个门，换成真实直播的音频",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.provider, args.model, args.voice, args.replay))


if __name__ == "__main__":
    sys.exit(main())
