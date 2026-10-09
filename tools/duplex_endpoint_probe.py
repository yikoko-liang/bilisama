"""Compare natural and forced endpointing using fixed, locally synthesized public inputs.

Preparation requires macOS /usr/bin/say and its Tingting voice. Credentials are
read only from the environment. No application prompts, recordings, or playback
are used. A completed observation is not evidence that endpointing worked.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import wave
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import websockets

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
SAMPLE_RATE = 16000
FRAME_BYTES = 640
FRAME_SECONDS = 0.02
ZERO = bytes(FRAME_BYTES)
PROMPT = (
    "你是中文语音助手。普通问题只用一句简短的话回答；"
    "用户明确要求详细说明时可以多说几句。不要使用任何格式标记。"
)
TEXTS = {
    "one": "请问一加一等于几？",
    "two": "请问二加二等于几？",
    "three": "请问三加三等于几？",
    "long_a": "我现在描述一个简单的整理过程。先把桌上的书放到书架上，再把铅笔放进笔筒里。",
    "long_b": "接着把水杯移到桌角，然后用干净的布擦一遍桌面，等水迹变干，再把台灯摆好。",
    "long_c": "最后把椅子推回桌下。请用一句话说说我刚才主要做了什么。",
    "prelude": "请详细介绍怎样照顾阳台上的薄荷，依次说说光照、浇水和修剪，多说几句。",
}
CASES = ("normal", "long_pause", "resume", "multiturn", "completion_race", "overlap_natural")
ASR_PREFIX = "conversation.item.input_audio_transcription."
Policy = Literal["natural", "commit"]
Frames = tuple[bytes, ...]
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Options:
    output_dir: Path
    tail_seconds: float = 14.0
    tag: str = ""

    def result_path(self, stem: str) -> Path:
        suffix = f"-{self.tag}" if self.tag else ""
        return self.output_dir / f"{stem}{suffix}.json"


@dataclass(frozen=True)
class Segment:
    label: str
    start: int
    frames: Frames

    @property
    def end(self) -> int:
        return self.start + len(self.frames)

    def metadata(self) -> JsonObject:
        return {
            "label": self.label,
            "start_s": self.start * FRAME_SECONDS,
            "end_s": self.end * FRAME_SECONDS,
            "frames": len(self.frames),
            "pcm_sha256": hashlib.sha256(b"".join(self.frames)).hexdigest(),
        }


@dataclass
class CommitJob:
    at: int
    label: str
    sent: bool = False


@dataclass
class Plan:
    policy: Policy
    tail_frames: int
    segments: list[Segment] = field(default_factory=list)
    commits: list[CommitJob] = field(default_factory=list)
    end_frame: int = 0
    overlap_pending: bool = False

    def add(self, label: str, frames: Frames, start: int, *, commit: bool = True) -> int:
        segment = Segment(label, start, frames)
        self.segments.append(segment)
        self.end_frame = max(self.end_frame, segment.end + self.tail_frames)
        if commit and self.policy == "commit":
            self.commits.append(CommitJob(segment.end + 5, label))
        return segment.end

    def schedule_overlap(self, current_frame: int, frames: Frames) -> int:
        start = max(current_frame + 40, self.segments[0].end + 1)
        end = self.add("one_during_reply", frames, start)
        self.end_frame = end + self.tail_frames
        self.overlap_pending = False
        return start

    def audio_at(self, index: int) -> tuple[str, bytes]:
        for segment in self.segments:
            if segment.start <= index < segment.end:
                return segment.label, segment.frames[index - segment.start]
        return "zero", ZERO


@dataclass
class OverlapEvidence:
    active_responses: set[str] = field(default_factory=set)
    input_started_t: float | None = None
    responses_at_input: tuple[str, ...] = ()

    def receive(self, frame: JsonObject) -> None:
        response_id = frame.get("response_id")
        if not isinstance(response_id, str):
            return
        event_type = frame.get("type")
        if event_type == "response.output_audio.started" and frame.get("tts_type") == "default":
            self.active_responses.add(response_id)
        elif event_type in ("response.output_audio.done", "response.done", "response.canceled"):
            self.active_responses.discard(response_id)

    def observe_input(self, label: str, pcm: bytes, sent_t: float) -> None:
        if label == "one_during_reply" and pcm != ZERO and self.input_started_t is None:
            self.input_started_t = sent_t
            self.responses_at_input = tuple(sorted(self.active_responses))


def build_plan(case: str, policy: Policy, clips: dict[str, Frames], tail_seconds: float) -> Plan:
    """Use identical clips/timing in each arm; only commit delivery differs."""
    if case not in CASES:
        raise ValueError(f"未知测试场景：{case}")
    plan = Plan(policy, round(tail_seconds / FRAME_SECONDS))
    start = 30
    if case == "long_pause":
        frames = clips["long_a"] + (ZERO,) * 30 + clips["long_b"] + (ZERO,) * 30 + clips["long_c"]
        plan.add("long_pause", frames, start)
    elif case == "resume":
        end = plan.add("one", clips["one"], start)
        plan.add("two", clips["two"], end + 11, commit=False)
    elif case == "multiturn":
        for label in ("one", "two", "three"):
            start = plan.add(label, clips[label], start) + 250
    elif case == "completion_race":
        end = plan.add("one", clips["one"], start, commit=False)
        plan.add("two", clips["two"], end + 250, commit=False)
    elif case == "overlap_natural":
        plan.add("prelude", clips["prelude"], start, commit=False)
        plan.end_frame += 1500
        plan.overlap_pending = True
    else:
        plan.add("one", clips["one"], start)
    return plan


def dump_json(path: Path, value: object, secret: str = "") -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if secret:
        text = text.replace(secret, "[redacted]")
    path.write_text(text + "\n", encoding="utf-8")


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (
            1,
            2,
            SAMPLE_RATE,
        ):
            raise ValueError(f"音频格式须为单声道 16 kHz PCM16：{path.name}")
        pcm = audio.readframes(audio.getnframes())
    if not pcm:
        raise ValueError(f"音频为空：{path.name}")
    return pcm


def gate_frames(pcm: bytes) -> Frames:
    """Gate below -43 dBFS, retaining internal pauses and trimming edge silence."""
    frames = []
    for offset in range(0, len(pcm), FRAME_BYTES):
        frame = pcm[offset : offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
        values = array.array("h", frame)
        if sys.byteorder != "little":
            values.byteswap()
        rms = math.sqrt(sum(value * value for value in values) / 320) / 32768
        frames.append(frame if rms >= 10 ** (-43 / 20) else ZERO)
    audible = [i for i, frame in enumerate(frames) if frame != ZERO]
    if not audible:
        raise ValueError("本地合成音频没有可辨认的有声帧，未进行端点测试")
    return tuple(frames[audible[0] : audible[-1] + 1])


def prepare(output_dir: Path) -> None:
    if not Path("/usr/bin/say").is_file():
        raise RuntimeError("生成测试音频需要 macOS 的 /usr/bin/say 和 Tingting 声音")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("准备音频需要空的输出目录，以免覆盖已有诊断数据")
    manifest: JsonObject = {
        "prompt": PROMPT,
        "source": "newly authored text, local macOS say",
        "clips": {},
    }
    for label, text in TEXTS.items():
        raw = output_dir / f"{label}-source.wav"
        subprocess.run(
            [
                "/usr/bin/say",
                "-v",
                "Tingting",
                "-r",
                "210",
                "-o",
                str(raw),
                "--data-format=LEI16@16000",
                text,
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        frames = gate_frames(read_pcm(raw))
        payload = b"".join(frames)
        with wave.open(str(output_dir / f"{label}.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            audio.writeframes(payload)
        manifest["clips"][label] = {
            "text": text,
            "frames": len(frames),
            "duration_s": len(frames) * FRAME_SECONDS,
            "pcm_sha256": hashlib.sha256(payload).hexdigest(),
        }
    dump_json(output_dir / "public-inputs.json", manifest)
    print(f"已生成 {len(TEXTS)} 段公开合成音频：{output_dir}", flush=True)


def load_clips(output_dir: Path) -> dict[str, Frames]:
    """Refuse inputs that no longer match the prepared public-content manifest."""
    manifest = json.loads((output_dir / "public-inputs.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("prompt") != PROMPT:
        raise ValueError("测试输入清单与内置通用提示词不匹配，请在新目录重新 prepare")
    records = manifest.get("clips")
    if not isinstance(records, dict) or records.keys() != TEXTS.keys():
        raise ValueError("测试音频清单不完整，请在新目录重新 prepare")
    clips = {}
    for label, text in TEXTS.items():
        pcm = read_pcm(output_dir / f"{label}.wav")
        record = records[label]
        if (
            not isinstance(record, dict)
            or record.get("text") != text
            or record.get("pcm_sha256") != hashlib.sha256(pcm).hexdigest()
            or len(pcm) % FRAME_BYTES
            or record.get("frames") != len(pcm) // FRAME_BYTES
        ):
            raise ValueError(f"测试音频与公开输入清单不匹配：{label}")
        clips[label] = tuple(pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES))
    return clips


def event_for_log(frame: JsonObject) -> JsonObject:
    """Keep full text and IDs, replacing only output audio payloads with lengths."""
    result = frame.copy()
    if result.get("type") == "response.output_audio.delta":
        result["audio_base64_length"] = len(result.pop("delta", ""))
    return result


def summarize(trace: JsonObject) -> JsonObject:
    recv = [row for row in trace["events"] if row["kind"] == "recv"]
    sent = [row for row in trace["events"] if row["kind"] == "send_audio"]
    items: dict[str, JsonObject] = {}
    for row in recv:
        frame = row["frame"]
        event_type = frame.get("type", "")
        if event_type not in {ASR_PREFIX + state for state in ("started", "delta", "completed")}:
            continue
        item = items.setdefault(
            str(frame.get("item_id", "")),
            {
                "item_id": frame.get("item_id"),
                "first_t": row["t"],
            },
        )
        if event_type.endswith(".started"):
            item["started_t"] = row["t"]
        elif event_type.endswith(".completed"):
            item["completed_t"] = row["t"]
            item["text"] = frame.get("text", "")
        else:
            item["last_delta_t"] = row["t"]
            item["last_delta"] = frame.get("delta", "")
    segments = []
    for segment in trace["segments"]:
        voiced = [row for row in sent if row["label"] == segment["label"] and row["nonzero"]]
        segments.append(
            {
                "label": segment["label"],
                "first_nonzero_sent_t": voiced[0]["send_started_t"] if voiced else None,
                "last_nonzero_end_t": (
                    round(voiced[-1]["send_started_t"] + FRAME_SECONDS, 6) if voiced else None
                ),
                "sent_frames": sum(row["label"] == segment["label"] for row in sent),
            }
        )
    return {
        "case": trace["case"],
        "policy": trace["policy"],
        "observation_completed": trace.get("observation_completed", False),
        "condition_exercised": trace.get("condition_exercised", False),
        "error_type": trace.get("error_type"),
        "segments": segments,
        "items": list(items.values()),
        "commits": [row for row in trace["events"] if row["kind"] == "commit"],
        "committed": [
            row for row in recv if row["frame"].get("type") == "input_audio_buffer.committed"
        ],
        "response_events": [
            row
            for row in recv
            if row["frame"].get("type")
            in (
                "response.output_audio.started",
                "response.output_audio.done",
                "response.done",
                "response.canceled",
            )
        ],
        "errors": [row for row in recv if row["frame"].get("type") == "error"],
        "max_send_ms": max((row["send_ms"] for row in sent), default=0),
        "max_schedule_lag_ms": max((row["lag_ms"] for row in sent), default=0),
        "max_frame_interval_ms": round(
            max(
                (b["send_started_t"] - a["send_started_t"] for a, b in pairwise(sent)),
                default=0,
            )
            * 1000,
            3,
        ),
        "observation_end_t": trace.get("observation_end_t"),
    }


def session_create(event_id: str) -> JsonObject:
    return {
        "type": "session.create",
        "event_id": event_id,
        "session": {
            "model": "1.2.6.1",
            "instructions": PROMPT,
            "audio": {
                "input": {"format": {"type": "pcm_s16le", "rate": SAMPLE_RATE}},
                "output": {
                    "format": {"type": "pcm_s16le", "rate": 24000},
                    "voice": "zh_female_vv_uranus_bigtts",
                    "speed": 0,
                    "loudness": 0,
                },
            },
        },
        "extension": {"asr": {}, "tts": {}, "dialog": {}},
    }


async def run_one(
    case: str, policy: Policy, clips: dict[str, Frames], key: str, options: Options
) -> JsonObject:
    loop = asyncio.get_running_loop()
    origin = loop.time()
    plan = build_plan(case, policy, clips, options.tail_seconds)
    trace: JsonObject = {
        "case": case,
        "policy": policy,
        "endpoint": ENDPOINT,
        "prompt": PROMPT,
        "started_unix": time.time(),
        "events": [],
        "segments": [],
        "commit_delay_s": 0.1,
        "tail_s": options.tail_seconds,
        "no_playback": True,
        "condition_exercised": False,
    }
    send_lock = asyncio.Lock()
    ready = asyncio.Event()
    first_response = asyncio.Event()
    race_fired = False
    overlap = OverlapEvidence()
    receiver_task: asyncio.Task[None] | None = None
    error_seen = False

    def record(kind: str, **data: Any) -> None:
        trace["events"].append({"kind": kind, "t": round(loop.time() - origin, 6), **data})

    try:
        async with websockets.connect(
            ENDPOINT,
            additional_headers={"X-Api-Key": key},
            max_size=16 << 20,
            open_timeout=15,
            close_timeout=3,
        ) as ws:

            async def control(payload: JsonObject, *, cause: str = "") -> None:
                async with send_lock:
                    before = loop.time()
                    await asyncio.wait_for(
                        ws.send(json.dumps(payload, ensure_ascii=False)), timeout=5
                    )
                    after = loop.time()
                record(
                    "commit" if payload["type"] == "input_audio_buffer.commit" else "send_control",
                    frame=payload,
                    cause=cause,
                    send_started_t=round(before - origin, 6),
                    send_ms=round((after - before) * 1000, 3),
                )

            async def receive() -> None:
                nonlocal race_fired, error_seen
                try:
                    async for raw in ws:
                        frame = json.loads(raw)
                        if not isinstance(frame, dict):
                            raise ValueError("端点返回非对象事件")
                        event_type = frame.get("type", "")
                        record("recv", frame=event_for_log(frame))
                        overlap.receive(frame)
                        if event_type == "session.created":
                            ready.set()
                        if (
                            event_type == "response.output_audio.started"
                            and frame.get("tts_type") == "default"
                        ):
                            first_response.set()
                        if event_type == "error":
                            error_seen = True
                            ready.set()
                        if (
                            event_type == ASR_PREFIX + "completed"
                            and case == "completion_race"
                            and not race_fired
                        ):
                            race_fired = True
                            record("race_trigger", item_id=frame.get("item_id"), policy=policy)
                            if policy == "commit":
                                await control(
                                    {
                                        "type": "input_audio_buffer.commit",
                                        "event_id": f"{case}-{policy}-after-natural-completed",
                                    },
                                    cause="immediately after first natural completed was received",
                                )
                except websockets.ConnectionClosed as exc:
                    record(
                        "recv_closed",
                        close_type=type(exc).__name__,
                        code=exc.rcvd.code if exc.rcvd else None,
                    )

            receiver_task = asyncio.create_task(receive())
            await control(session_create(f"{case}-{policy}-create"))
            await asyncio.wait_for(ready.wait(), timeout=15)
            if error_seen:
                raise RuntimeError("端点拒绝会话；错误已记录")
            stream_origin = loop.time()
            trace["stream_start_t"] = round(stream_origin - origin, 6)
            record("stream_begin")
            index = 0
            while index <= plan.end_frame:
                due = stream_origin + index * FRAME_SECONDS
                await asyncio.sleep(max(0, due - loop.time()))
                if plan.overlap_pending and first_response.is_set():
                    overlap_start = plan.schedule_overlap(index, clips["one"])
                    record("overlap_scheduled", starts_at_frame=overlap_start)
                label, pcm = plan.audio_at(index)
                message = json.dumps(
                    {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
                )
                async with send_lock:
                    before = loop.time()
                    overlap.observe_input(label, pcm, round(before - origin, 6))
                    await asyncio.wait_for(ws.send(message), timeout=5)
                    after = loop.time()
                record(
                    "send_audio",
                    index=index,
                    label=label,
                    nonzero=pcm != ZERO,
                    send_started_t=round(before - origin, 6),
                    send_ms=round((after - before) * 1000, 3),
                    lag_ms=round(max(0, before - due) * 1000, 3),
                )
                for job in plan.commits:
                    if not job.sent and index >= job.at:
                        job.sent = True
                        await control(
                            {
                                "type": "input_audio_buffer.commit",
                                "event_id": f"{case}-{policy}-{job.label}-commit",
                            },
                            cause=f"100ms after synthetic segment {job.label} ended",
                        )
                if error_seen:
                    raise RuntimeError("端点返回错误；错误已记录")
                if receiver_task.done():
                    await receiver_task
                    raise RuntimeError("端点提前关闭接收流")
                index += 1
            trace["condition_exercised"] = (
                bool(overlap.responses_at_input)
                if case == "overlap_natural"
                else case != "completion_race" or race_fired
            )
            if not trace["condition_exercised"]:
                record("condition_not_reached", case=case)
            trace["observation_end_t"] = round(loop.time() - origin, 6)
            await control({"type": "session.close", "event_id": f"{case}-{policy}-close"})
            await asyncio.sleep(0.25)
            if error_seen:
                raise RuntimeError("端点返回错误；错误已记录")
            if receiver_task.done():
                await receiver_task
            trace["observation_completed"] = True
    except (OSError, TimeoutError, RuntimeError, ValueError, websockets.WebSocketException) as exc:
        trace["observation_completed"] = False
        trace["error_type"] = type(exc).__name__
    finally:
        if receiver_task is not None and not receiver_task.done():
            receiver_task.cancel()
            with suppress(asyncio.CancelledError):
                await receiver_task
        elif receiver_task is not None and not receiver_task.cancelled():
            receiver_error = receiver_task.exception()
            if receiver_error is not None:
                trace["observation_completed"] = False
                trace["error_type"] = type(receiver_error).__name__
        if case == "overlap_natural":
            trace["overlap_evidence"] = {
                "first_nonzero_sent_t": overlap.input_started_t,
                "active_response_ids_at_input": list(overlap.responses_at_input),
            }
        trace["segments"] = [segment.metadata() for segment in plan.segments]
        summary = summarize(trace)
        trace["summary"] = summary
        await asyncio.to_thread(dump_json, options.result_path(f"{case}-{policy}"), trace, key)
    return summary


async def run_cases(cases: list[str], options: Options) -> None:
    # The lowercase alias matches the existing vendor credential environment.
    key = os.environ.get("BILISAMA_KEY_VOLCANO_API_KEY") or os.environ.get(
        "volcano_api_key"  # noqa: SIM112
    )
    if not key:
        raise RuntimeError("缺少供应商环境凭据，未发起请求")
    clips = await asyncio.to_thread(load_clips, options.output_dir)
    stems = [f"{case}-{policy}" for case in cases for policy in ("natural", "commit")] + ["summary"]
    if any(options.result_path(stem).exists() for stem in stems):
        raise ValueError("已有同名结果，拒绝覆盖；请使用新的 --tag")
    summaries: list[JsonObject] = []
    for case in cases:
        print(f"开始双连接对照：{case}；结束后需人工判断事件与时序", flush=True)
        pair = await asyncio.gather(
            run_one(case, "natural", clips, key, options),
            run_one(case, "commit", clips, key, options),
        )
        summaries.extend(pair)
        await asyncio.to_thread(dump_json, options.result_path("summary"), summaries, key)
        for summary in pair:
            print(
                f"{case}/{summary['policy']}：观察完成={summary['observation_completed']}，"
                f"场景触发={summary['condition_exercised']}，"
                f"转写条目={len(summary['items'])}，commit 回执={len(summary['committed'])}",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="用固定公开合成音频对比自然结束与 commit；不播放声音"
    )
    parser.add_argument(
        "command", choices=("prepare", "run"), help="prepare 依赖 macOS say；run 连接真实供应商"
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="合成音频、原始事件与摘要的保存目录"
    )
    parser.add_argument(
        "--case", choices=CASES, action="append", help="可重复指定；默认运行全部六种场景"
    )
    parser.add_argument(
        "--tail", type=float, default=14.0, help="最后一段音频后持续送零的秒数，默认 14"
    )
    parser.add_argument("--tag", default="", help="本次结果文件标签；便于复用同一组合成音频")
    args = parser.parse_args()
    if not 5 <= args.tail <= 60:
        parser.error("尾部观察时间须在 5 到 60 秒之间")
    if args.tag and (not args.tag.isascii() or not args.tag.replace("-", "").isalnum()):
        parser.error("结果标签只能包含英文字母、数字和连字符")
    options = Options(args.output_dir, args.tail, args.tag)
    try:
        if args.command == "prepare":
            prepare(options.output_dir)
        else:
            asyncio.run(run_cases(list(dict.fromkeys(args.case or CASES)), options))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, wave.Error) as exc:
        parser.exit(1, f"未完成测试：{exc}\n")


if __name__ == "__main__":
    main()
