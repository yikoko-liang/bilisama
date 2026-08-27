"""Rate conversion, checked against what a stream actually does to it.

The interesting failures here are not "wrong number of samples" — they are the
ones that sound wrong: a discontinuity at every chunk boundary, or a phase
that drifts a fraction of a sample per chunk until the stream runs slow.
"""

from __future__ import annotations

import math
from array import array
from itertools import pairwise

import pytest

from bilisama.realtime.resample import Resampler


def _tone(samples: int, *, rate: int, hz: float = 440.0) -> bytes:
    """A clean sine, so the checks below can be about shape rather than luck."""
    out = array("h", (int(12000 * math.sin(2 * math.pi * hz * n / rate)) for n in range(samples)))
    return out.tobytes()


def _ints(pcm: bytes) -> list[int]:
    got = array("h")
    got.frombytes(pcm)
    return list(got)


def test_the_rate_our_chain_actually_needs() -> None:
    """16 kHz capture into a 24 kHz endpoint: three samples out for every two
    in. Getting the ratio backwards makes her sound slow rather than failing."""
    r = Resampler(source_rate=16000, target_rate=24000)
    one_frame = _tone(320, rate=16000)  # 20 ms

    out = r.feed(one_frame)

    assert len(out) // 2 == pytest.approx(480, abs=2), "20ms 的 16k 帧应当变成 480 个样本"


def test_a_matched_rate_is_left_alone_byte_for_byte() -> None:
    """Three of the four providers need nothing. Copying anyway would put an
    interpolation pass on the hot path of the paths that were already right."""
    r = Resampler(source_rate=16000, target_rate=16000)
    frame = _tone(320, rate=16000)

    assert r.passthrough
    assert r.feed(frame) is frame


def test_a_stream_keeps_its_length_over_many_chunks() -> None:
    """The phase carries across chunks as a fraction. Rounding it per chunk
    loses a sliver each time, and a minute of speech arrives measurably short —
    which sounds like the far end truncating, not like arithmetic."""
    r = Resampler(source_rate=16000, target_rate=24000)
    total = 0
    for _ in range(500):  # 10 seconds of 20 ms frames
        total += len(r.feed(_tone(320, rate=16000))) // 2

    expected = 500 * 320 * 24000 / 16000
    assert total == pytest.approx(expected, rel=0.001), f"10 秒后差了 {total - expected} 个样本"


def test_chunk_boundaries_do_not_leave_a_step() -> None:
    """The carry-over is the whole reason this class holds state. Without it
    every 20 ms boundary gets a discontinuity — 50 clicks a second, which reads
    as a broken microphone rather than as a bug in here.

    Measured as the largest jump between neighbouring output samples: a smooth
    440 Hz tone has small ones everywhere, and a boundary step is an outlier.
    """
    r = Resampler(source_rate=16000, target_rate=24000)
    rendered: list[int] = []
    for chunk in range(10):
        frame = array(
            "h",
            (
                int(12000 * math.sin(2 * math.pi * 440.0 * (chunk * 320 + n) / 16000))
                for n in range(320)
            ),
        ).tobytes()
        rendered += _ints(r.feed(frame))

    jumps = [abs(b - a) for a, b in pairwise(rendered)]
    # One period of 440 Hz at 24 kHz is ~55 samples, so neighbours differ by at
    # most a few percent of amplitude. A dropped carry-over shows up as a jump
    # several times that.
    assert max(jumps) < 3 * (
        sum(jumps) / len(jumps)
    ), f"最大跳变 {max(jumps)}，均值 {sum(jumps)/len(jumps):.0f}"


def test_it_reconstructs_a_tone_rather_than_a_staircase() -> None:
    """Interpolating between samples, not repeating them. Nearest-neighbour
    would pass every length check above and sound like a broken speaker."""
    r = Resampler(source_rate=16000, target_rate=24000)
    got = _ints(r.feed(_tone(320, rate=16000)))
    reference = _ints(_tone(len(got), rate=24000))

    # Compare against a real 24 kHz tone. Linear interpolation tracks it
    # closely; a staircase does not.
    error = max(abs(a - b) for a, b in zip(got[:400], reference[:400], strict=False))
    assert error < 1200, f"跟真正的 24k 正弦差了 {error}（幅度 12000）"


def test_a_new_connection_starts_without_the_old_one_s_last_sample() -> None:
    r = Resampler(source_rate=16000, target_rate=24000)
    r.feed(_tone(320, rate=16000))
    r.reset()

    first = _ints(r.feed(_tone(320, rate=16000)))
    fresh = _ints(Resampler(source_rate=16000, target_rate=24000).feed(_tone(320, rate=16000)))
    assert first == fresh


def test_half_a_sample_is_refused_rather_than_rounded() -> None:
    """A one-byte offset turns every sample after it into noise, and the
    stream keeps flowing — so this has to be loud at the point it happens."""
    r = Resampler(source_rate=16000, target_rate=24000)
    with pytest.raises(ValueError, match="奇数"):
        r.feed(b"\x01\x02\x03")


def test_an_empty_chunk_is_not_an_error() -> None:
    r = Resampler(source_rate=16000, target_rate=24000)
    assert r.feed(b"") == b""


@pytest.mark.parametrize(("source", "target"), [(0, 24000), (16000, 0), (-16000, 24000)])
def test_a_nonsense_rate_is_refused_at_construction(source: int, target: int) -> None:
    with pytest.raises(ValueError, match="采样率"):
        Resampler(source_rate=source, target_rate=target)


def test_chunking_does_not_change_the_result() -> None:
    """The real invariant, and why the phase is a carried fraction.

    Feeding a stream in ragged pieces must produce what feeding it whole
    produces. Length alone is too blunt to see the difference: 16 kHz to
    24 kHz with 320-sample frames divides exactly, so resetting the phase per
    chunk happens to be right on OUR numbers, and a mutation that did exactly
    that survived every other check in this file. An uneven ratio plus ragged
    chunks — which is what a browser worklet actually hands over — makes each
    chunk round up on its own, and the streams diverge.
    """
    sizes = [313, 97, 640, 41, 320, 1021, 7, 512]
    whole = _tone(sum(sizes), rate=16000)

    piecewise = Resampler(source_rate=16000, target_rate=22050)
    parts, offset = [], 0
    for n in sizes:
        parts.append(piecewise.feed(whole[offset * 2 : (offset + n) * 2]))
        offset += n
    chunked = _ints(b"".join(parts))

    at_once = _ints(Resampler(source_rate=16000, target_rate=22050).feed(whole))

    assert len(chunked) == len(at_once), f"分块 {len(chunked)} 个样本，整块 {len(at_once)} 个"
    worst = max(abs(a - b) for a, b in zip(chunked, at_once, strict=True))
    assert worst <= 1, f"同一段音频分块喂和整块喂差了 {worst}"


def test_ragged_chunks_at_an_uneven_ratio_keep_their_length() -> None:
    """Why the phase is carried as a fraction rather than reset per chunk.

    16 kHz to 24 kHz with 320-sample frames divides exactly, so on OUR numbers
    resetting the phase every chunk happens to be correct — a mutation that
    did exactly that survived the whole file. It stops being correct the moment
    either the ratio or the chunk size is uneven, and ragged chunks are not
    hypothetical: a browser hands over whatever the worklet accumulated.
    """
    r = Resampler(source_rate=16000, target_rate=22050)
    sizes = [313, 97, 640, 41, 320, 1021]
    total_in = sum(sizes)
    total_out = sum(len(r.feed(_tone(n, rate=16000))) // 2 for n in sizes)

    expected = total_in * 22050 / 16000
    assert total_out == pytest.approx(
        expected, rel=0.002
    ), f"{total_in} 个样本重采样后是 {total_out}，应当接近 {expected:.0f}"
