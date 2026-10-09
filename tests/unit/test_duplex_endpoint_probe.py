"""Pure probe contracts: paired inputs, timing, provenance and truthful summaries."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from duplex_endpoint_probe import (
    ASR_PREFIX,
    CASES,
    FRAME_BYTES,
    PROMPT,
    SAMPLE_RATE,
    TEXTS,
    ZERO,
    Frames,
    Options,
    OverlapEvidence,
    build_plan,
    event_for_log,
    gate_frames,
    load_clips,
    read_pcm,
    summarize,
)

VOICE = struct.pack("<h", 1000) * 320


@pytest.fixture
def clips() -> dict[str, Frames]:
    return {label: (VOICE,) * (20 + index) for index, label in enumerate(TEXTS)}


@pytest.mark.parametrize("case", CASES)
def test_both_arms_have_identical_audio(case: str, clips: dict[str, Frames]) -> None:
    natural = build_plan(case, "natural", clips, 14)
    forced = build_plan(case, "commit", clips, 14)
    assert natural.segments == forced.segments
    assert natural.end_frame == forced.end_frame
    assert not natural.commits
    if case == "overlap_natural":
        assert natural.schedule_overlap(100, clips["one"]) == forced.schedule_overlap(
            100, clips["one"]
        )
        assert natural.segments == forced.segments
        assert not natural.commits
        assert len(forced.commits) == 1


def test_resume_starts_120ms_after_commit(clips: dict[str, Frames]) -> None:
    plan = build_plan("resume", "commit", clips, 14)
    first, second = plan.segments
    assert second.start - first.end == 11
    assert len(plan.commits) == 1
    assert plan.commits[0].at == first.end + 5
    assert second.start - plan.commits[0].at == 6
    assert plan.audio_at(first.end) == ("zero", ZERO)
    assert plan.audio_at(second.start) == ("two", VOICE)


def test_internal_pauses_do_not_schedule_commits(clips: dict[str, Frames]) -> None:
    plan = build_plan("long_pause", "commit", clips, 14)
    segment = plan.segments[0]
    assert (
        segment.frames
        == clips["long_a"] + (ZERO,) * 30 + clips["long_b"] + (ZERO,) * 30 + clips["long_c"]
    )
    assert len(plan.commits) == 1
    assert plan.commits[0].at == segment.end + 5


def test_multiturn_has_one_commit_per_utterance(clips: dict[str, Frames]) -> None:
    plan = build_plan("multiturn", "commit", clips, 14)
    assert len(plan.commits) == 3
    assert [job.at for job in plan.commits] == [segment.end + 5 for segment in plan.segments]
    assert plan.segments[1].start - plan.segments[0].end == 250


def test_completion_race_waits_for_server_event(clips: dict[str, Frames]) -> None:
    plan = build_plan("completion_race", "commit", clips, 14)
    assert not plan.commits
    assert len(plan.segments) == 2


def test_overlap_waits_for_output_and_does_not_commit_prelude(clips: dict[str, Frames]) -> None:
    plan = build_plan("overlap_natural", "commit", clips, 45)
    assert bool(plan.overlap_pending)
    assert not plan.commits
    assert len(plan.segments) == 1
    start = plan.schedule_overlap(100, clips["one"])
    assert start == 140
    assert not plan.overlap_pending
    assert plan.end_frame == start + len(clips["one"]) + 2250
    assert plan.commits[0].label == "one_during_reply"


def test_audio_logging_keeps_ids_and_text_without_audio_payload() -> None:
    source = {
        "type": "response.output_audio.delta",
        "delta": "AAAA",
        "response_id": "r1",
        "item_id": "i1",
    }
    logged = event_for_log(source)
    assert "delta" not in logged
    assert logged["audio_base64_length"] == 4
    assert logged["response_id"] == "r1" and logged["item_id"] == "i1"
    assert source["delta"] == "AAAA"
    text_event = {"type": ASR_PREFIX + "delta", "item_id": "i2", "delta": "普通文本"}
    assert event_for_log(text_event) == text_event


def test_ack_without_asr_stays_visible_as_empty_transcription() -> None:
    trace: dict[str, Any] = {
        "case": "overlap_natural",
        "policy": "commit",
        "segments": [],
        "observation_completed": True,
        "condition_exercised": True,
        "events": [
            {
                "kind": "recv",
                "t": 2.0,
                "frame": {"type": "input_audio_buffer.committed", "event_id": "c1"},
            }
        ],
    }
    summary = summarize(trace)
    assert summary["observation_completed"] is True
    assert summary["items"] == []
    assert len(summary["committed"]) == 1
    assert "ok" not in summary


def test_summary_keeps_item_identity_and_measures_actual_sends() -> None:
    trace: dict[str, Any] = {
        "case": "resume",
        "policy": "commit",
        "segments": [{"label": "one"}],
        "events": [
            {
                "kind": "send_audio",
                "t": 9.0,
                "send_started_t": 1.0,
                "send_ms": 2,
                "lag_ms": 3,
                "label": "one",
                "nonzero": True,
            },
            {
                "kind": "send_audio",
                "t": 12.0,
                "send_started_t": 1.02,
                "send_ms": 2,
                "lag_ms": 3,
                "label": "zero",
                "nonzero": False,
            },
            {
                "kind": "recv",
                "t": 1.5,
                "frame": {"type": ASR_PREFIX + "completed", "item_id": "first", "text": "一加一"},
            },
            {
                "kind": "recv",
                "t": 3.0,
                "frame": {"type": ASR_PREFIX + "delta", "item_id": "second", "delta": "二加二"},
            },
        ],
    }
    summary = summarize(trace)
    assert [item["item_id"] for item in summary["items"]] == ["first", "second"]
    assert summary["items"][0]["text"] == "一加一"
    assert "completed_t" not in summary["items"][1]
    assert summary["segments"][0]["last_nonzero_end_t"] == 1.02
    assert summary["max_frame_interval_ms"] == 20
    assert summary["observation_completed"] is False


def test_gating_trims_edges_and_keeps_internal_silence() -> None:
    assert gate_frames(ZERO + VOICE + ZERO + VOICE + ZERO) == (VOICE, ZERO, VOICE)
    with pytest.raises(ValueError, match="有声帧"):
        gate_frames(ZERO)


def write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)


def test_wav_validation_rejects_wrong_format_and_empty(tmp_path: Path) -> None:
    path = tmp_path / "input.wav"
    write_wav(path, VOICE, rate=24000)
    with pytest.raises(ValueError, match="格式"):
        read_pcm(path)
    write_wav(path, b"")
    with pytest.raises(ValueError, match="为空"):
        read_pcm(path)


def test_manifest_rejects_changed_audio_and_text(tmp_path: Path) -> None:
    records = {}
    for label, text in TEXTS.items():
        write_wav(tmp_path / f"{label}.wav", VOICE)
        records[label] = {
            "text": text,
            "frames": 1,
            "pcm_sha256": hashlib.sha256(VOICE).hexdigest(),
        }
    manifest = {"prompt": PROMPT, "clips": records}
    manifest_path = tmp_path / "public-inputs.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert all(frames == (VOICE,) for frames in load_clips(tmp_path).values())
    write_wav(tmp_path / "one.wav", VOICE + ZERO)
    with pytest.raises(ValueError, match="不匹配"):
        load_clips(tmp_path)
    write_wav(tmp_path / "one.wav", VOICE)
    records["one"]["text"] = "不同的输入"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="不匹配"):
        load_clips(tmp_path)


def test_result_paths_use_explicit_directory_and_tag(tmp_path: Path) -> None:
    assert Options(tmp_path).result_path("summary") == tmp_path / "summary.json"
    assert (
        Options(tmp_path, tag="long-tail").result_path("normal-commit")
        == tmp_path / "normal-commit-long-tail.json"
    )
    assert len(VOICE) == FRAME_BYTES


def test_overlap_requires_audio_still_active_when_input_starts() -> None:
    ended = OverlapEvidence()
    started = {"type": "response.output_audio.started", "tts_type": "default", "response_id": "r1"}
    ended.receive(started)
    ended.receive({"type": "response.output_audio.done", "response_id": "r1"})
    ended.observe_input("one_during_reply", VOICE, 1.0)
    assert ended.input_started_t == 1.0
    assert ended.responses_at_input == ()
    active = OverlapEvidence()
    active.receive(started)
    active.observe_input("one_during_reply", ZERO, 0.5)
    assert active.input_started_t is None
    active.observe_input("one_during_reply", VOICE, 1.0)
    active.receive({"type": "response.output_audio.done", "response_id": "r1"})
    active.observe_input("one_during_reply", VOICE, 1.02)
    assert active.responses_at_input == ("r1",)
    assert active.input_started_t == 1.0
