"""Acceptance run for the voice gate on a real backend (plan P6, scripted).

The runbook's live-mock acceptance needs a person in Chrome sharing a tab.
This measures the same thing without one: the real link (s2s / DashScope /
volcano), the real fan-out, gate and scheduler — wired the way dev-talk
wires them — with the streamer's lines synthesised by macOS `say` and pushed
through push_audio at real-time pace, and the gated view standing in for
the speakers. For every line it records what she wrote at the head, what
the gate did and whether any audio reached the speakers, then scores the
run against each line's label: classification accuracy, lines meant for
her that were skipped (over-skip), lines not for her that played
(under-skip).

Usage::

    source path.sh
    .venv/bin/python tools/voice_gate_acceptance.py --provider dashscope
    .venv/bin/python tools/voice_gate_acceptance.py --provider s2s      # server on :8765
    .venv/bin/python tools/voice_gate_acceptance.py --provider volcano

Two scores. 该不该说判对 is what the room hears: she spoke exactly when the
line was for her. 记号分类准确 is stricter: the category she reported matches
the label. Exit status 0 when the first is at least 0.8 and at most one line
meant for her was skipped — the bar in docs/mvp-validation-plan.md §11.
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
from bilisama.scene_markers import SceneCategory

_REPO = Path(__file__).resolve().parents[1]
_CONFIG = _REPO / "config"
_FRAME = 640  # 20 ms of 16 kHz mono s16le
_TAIL_S = 2.5  # silence after each line, so the server's VAD closes the turn
_REPLY_TIMEOUT_S = 30.0

# The streamer's lines, with who they were for. Punctuation is kept out of
# the spoken text: `say` pauses on it and the pause splits one line into two
# turns (tests/integration/test_real_server.py's note). `accept` widens the
# label where a human listener could reasonably hear it either way.
_TO_ME = SceneCategory.TO_ME


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    expected: SceneCategory
    accept: frozenset[SceneCategory] = frozenset()

    def ok(self, category: SceneCategory) -> bool:
        return category is self.expected or category in self.accept


LINES: tuple[Line, ...] = (
    Line("各位观众朋友们大家好 欢迎来到直播间", SceneCategory.AUDIENCE),
    Line("豆腐 你觉得今天的天气怎么样", _TO_ME),
    Line("谢谢小明送的火箭 谢谢谢谢", SceneCategory.READING, frozenset({SceneCategory.AUDIENCE})),
    Line("哎呀 这个怎么又卡住了", SceneCategory.SELF_TALK),
    Line("豆腐你在吗 帮我看看弹幕里有什么问题", _TO_ME),
    Line("今天人有点少啊 大家多多点赞关注", SceneCategory.AUDIENCE),
    Line("老王 你那边听得到我说话吗 麦克风开了没", SceneCategory.GUEST),
    Line("豆腐 你喜欢吃火锅还是烧烤", _TO_ME),
    Line(
        "有人问我用的什么键盘 是机械键盘",
        SceneCategory.READING,
        frozenset({SceneCategory.AUDIENCE}),
    ),
    Line("等一下 我找找刚才那个文件放哪了", SceneCategory.SELF_TALK),
    Line("豆腐 给大家讲个笑话吧", _TO_ME),
    Line("感谢大家的陪伴 我们今天播到十点就下播", SceneCategory.AUDIENCE),
    Line("感谢阿强的舰长 欢迎上舰", SceneCategory.READING, frozenset({SceneCategory.AUDIENCE})),
    Line(
        "嗯 这里应该往左走还是往右走呢", SceneCategory.SELF_TALK, frozenset({SceneCategory.UNSURE})
    ),
    Line("豆腐你说 我这个发型好不好看", _TO_ME),
    Line("家人们 今天这个游戏我一定要通关", SceneCategory.AUDIENCE),
    Line("小李你先别说 等我把这局打完", SceneCategory.GUEST),
    Line(
        "这位朋友说主播声音好听 谢谢夸奖",
        SceneCategory.READING,
        frozenset({SceneCategory.AUDIENCE}),
    ),
    Line("豆腐 我们等会儿玩什么游戏好", _TO_ME),
    Line("大家把弹幕刷起来 让我看看有多少人在", SceneCategory.AUDIENCE),
    Line("你觉得我刚才唱得怎么样 豆腐", _TO_ME),
    Line("好了 那今天就先到这里 大家晚安", SceneCategory.AUDIENCE),
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
    def category(self) -> SceneCategory:
        """What her head said, as the gate read it."""
        if self.ruling is not None:
            return self.ruling.category
        return _TO_ME

    @property
    def correct(self) -> bool:
        """The category she reported matches the label (the stricter score)."""
        return self.status != "no_reply" and self.line.ok(self.category)

    @property
    def decided_right(self) -> bool:
        """She spoke exactly when the line was for her — what the room hears."""
        if self.status == "no_reply":
            return False
        return (self.audio_frames > 0) == (self.line.expected is _TO_ME)

    @property
    def over_skip(self) -> bool:
        return self.line.expected is _TO_ME and self.skipped

    @property
    def under_skip(self) -> bool:
        return self.line.expected is not _TO_ME and self.audio_frames > 0


@dataclass(slots=True)
class _Books:
    """What the wiring saw, per reply handle."""

    skips: dict[int, Skip] = field(default_factory=dict)
    audio: dict[int, int] = field(default_factory=dict)
    dones: list[link.ReplyDone] = field(default_factory=list)
    started: list[int] = field(default_factory=list)


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


async def _watch_speakers(view: AsyncIterator[link.LinkEvent], books: _Books) -> None:
    async for event in view:
        if isinstance(event, link.ReplyAudioDelta):
            books.audio[event.handle.handle_id] = books.audio.get(event.handle.handle_id, 0) + 1


def _instructions(persona: PersonaStore, variables: dict[str, str]) -> str:
    rules = live_voice_rules(_CONFIG, variables, addressing=True)
    return assemble_scoped(static_prefix(persona.anchors(variables)), rules)


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


def _render(results: list[Result]) -> str:
    rows = ["#  | 期望       | 她写的开头                     | 门     | 音频帧 | 判定"]
    for i, r in enumerate(results, 1):
        gate = "拦下" if r.skipped else ("放行" if r.status != "no_reply" else "没回复")
        verdict = "✓" if r.correct else ("说对了 记号不准" if r.decided_right else "✗")
        if r.over_skip:
            verdict += " 过度不接"
        if r.under_skip:
            verdict += " 漏拦"
        cells = (f"{i:<2}", f"{r.line.expected.value:<10}", f"{r.head:<30}", f"{gate:<4}")
        rows.append(" | ".join(cells) + f" | {r.audio_frames:>5} | {verdict}")
    return "\n".join(rows)


async def _run(provider: str) -> int:
    logging.basicConfig(level=logging.WARNING)
    settings = load(_CONFIG / "bilisama.toml")
    endpoint = resolve_endpoint(settings, provider=provider, url=None, model=None, env=os.environ)
    persona = PersonaStore.from_config(settings.persona, config_dir=_CONFIG)
    variables = template_variables(settings.persona, reply_length=settings.interaction.reply_length)
    built = build_link(
        LinkRequest(
            endpoint=endpoint,
            settings=settings,
            bot_name=variables["agentName"],
            env=os.environ,
            text_replies=False,
        )
    )
    print(f"后端：{endpoint.provider.value}  地址：{built.url}")
    print("合成台词…", end="", flush=True)
    audio = [_say(line.text) for line in LINES]
    silence = b"\x00" * int(16000 * _TAIL_S) * 2
    print(f" {len(LINES)} 句")

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
        for line, pcm in zip(LINES, audio, strict=True):
            result = await _one_line(speech, books, line, pcm, silence)
            results.append(result)
            print(
                f"{len(results):>2}/{len(LINES)} {line.expected.value:<10} → "
                f"{'拦下' if result.skipped else result.status:<9} {result.head}"
            )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await speech.aclose()

    print()
    print(_render(results))
    total = len(results)
    decided = sum(r.decided_right for r in results)
    correct = sum(r.correct for r in results)
    over = sum(r.over_skip for r in results)
    under = sum(r.under_skip for r in results)
    no_reply = sum(r.status == "no_reply" for r in results)
    print()
    print(
        f"该不该说判对 {decided}/{total} = {decided / total:.0%}   过度不接 {over}   漏拦 {under}"
    )
    print(f"记号分类准确 {correct}/{total} = {correct / total:.0%}   没回复 {no_reply}")
    print(f"门：{gate.status()}")
    mean_s = sum(r.seconds for r in results) / total
    print(f"调度器判决 {len(scheduler.verdicts)} 条，平均每句 {mean_s:.1f} 秒")
    passed = decided / total >= 0.8 and over <= 1
    print("放行" if passed else "不放行")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语音门验收：真实后端、合成台词、逐句对照")
    parser.add_argument("--provider", required=True, choices=["s2s", "dashscope", "volcano"])
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.provider))


if __name__ == "__main__":
    sys.exit(main())
