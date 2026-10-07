"""Her voice's own volume (audio.voice_volume) on the local speaker path.

The page applies the same rule in ui/web/js/voice-worklet.js, and the browser
tier measures that one (tests/ui/test_audio_page.py). These pin the
sounddevice fallback's arithmetic, the field itself, and the constants the
two implementations must share.

The loud test signal is not arbitrary: 0.67 of full scale (-3.5 dBFS) is the
hottest peak among nine real duplex recordings at loudness 0 (2026-10-07
voice probe, peaks -9.9 to -3.5 dBFS, median -6.5). Doubled, it is the case
that would clip without a limiter.
"""

from __future__ import annotations

import math
import re
from array import array
from pathlib import Path

import pytest

from bilisama import dev_talk
from bilisama.config.schema import AudioConfig, Settings
from bilisama.dev_talk import _OUTPUT_RATE, _VOICE_CEILING, _VOICE_RELEASE_S, _VoiceVolume
from bilisama.ui.config_edit import field_control

_HOTTEST_MEASURED = 0.67
_BLOCK = 512  # frames; a typical PortAudio callback on this machine's CoreAudio


def _tone(amplitude: float, frames: int, *, hz: float = 220.0) -> bytes:
    """A 24 kHz int16 sine, the shape her PCM arrives in."""
    return array(
        "h",
        (
            round(amplitude * 32767 * math.sin(2 * math.pi * hz * i / _OUTPUT_RATE))
            for i in range(frames)
        ),
    ).tobytes()


def _peak(pcm: bytes) -> float:
    samples = array("h", pcm)
    return max(-min(samples), max(samples)) / 32768 if samples else 0.0


def _run(volume: _VoiceVolume, pcm: bytes, *, block: int = _BLOCK) -> list[bytes]:
    """Feed pcm through in callback-sized blocks, each with the next one as its
    lookahead — what _Speaker._feed hands it while the ring holds a reply."""
    size = block * 2
    chunks = [pcm[i : i + size] for i in range(0, len(pcm), size)]
    return [
        volume.apply(chunk, chunks[n + 1] if n + 1 < len(chunks) else b"")
        for n, chunk in enumerate(chunks)
    ]


# ------------------------------------------------------------ the field


def test_her_volume_ships_at_100_and_the_panel_range_is_0_to_200() -> None:
    assert Settings().audio.voice_volume == 100
    control = field_control(AudioConfig.model_fields["voice_volume"])
    assert (control["kind"], control["min"], control["max"]) == ("number", 0, 200)


def test_the_page_and_the_local_speaker_limit_at_the_same_line() -> None:
    """Two implementations of one rule: a ceiling moved in one and not the
    other makes her louder on whichever path the streamer happens to be on."""
    worklet = (Path(dev_talk.__file__).parent / "ui" / "web" / "js" / "voice-worklet.js").read_text(
        encoding="utf-8"
    )
    ceiling = re.search(r"const CEILING = ([0-9.]+);", worklet)
    release = re.search(r"const RELEASE_S = ([0-9.]+);", worklet)
    assert ceiling and release, "voice-worklet.js 里找不到 CEILING / RELEASE_S"
    assert float(ceiling.group(1)) == _VOICE_CEILING
    assert float(release.group(1)) == _VOICE_RELEASE_S


# ------------------------------------------------------------ normal path


def test_at_100_her_audio_leaves_byte_for_byte() -> None:
    pcm = _tone(_HOTTEST_MEASURED, _BLOCK * 8)
    out = b"".join(_run(_VoiceVolume(100), pcm))
    assert out == pcm


def test_turning_her_down_scales_every_sample_after_one_block() -> None:
    """The first block ramps (a step in gain is a click); every block after
    it is the plain half."""
    pcm = _tone(_HOTTEST_MEASURED, _BLOCK * 6)
    blocks = _run(_VoiceVolume(50), pcm)
    want = array("h", pcm)[_BLOCK:]
    got = array("h", b"".join(blocks[1:]))
    assert max(abs(g - round(w * 0.5)) for g, w in zip(got, want, strict=True)) <= 1


def test_a_volume_change_mid_reply_lands_within_a_fraction_of_a_second() -> None:
    """Down lands within one block; back up rides the release, which still
    has to arrive while she is mid-sentence, not on the next reply."""
    volume = _VoiceVolume(100)
    _run(volume, _tone(0.3, _BLOCK * 4))
    volume.set_percent(50)
    down = _run(volume, _tone(0.3, _BLOCK * 2))
    assert _peak(down[1]) == pytest.approx(0.15, rel=0.03)

    volume.set_percent(100)
    quarter_second = int(0.25 * _OUTPUT_RATE)
    up = _run(volume, _tone(0.3, quarter_second))
    assert _peak(up[-1]) >= 0.3 * 0.9, "调回 100% 之后四分之一秒还没回来"


# ------------------------------------------------------------ edges


def test_zero_is_silence_after_one_block() -> None:
    blocks = _run(_VoiceVolume(0), _tone(_HOTTEST_MEASURED, _BLOCK * 4))
    assert all(_peak(block) == 0.0 for block in blocks[1:])


def test_200_doubles_her_and_the_hottest_measured_voice_stays_under_the_ceiling() -> None:
    pcm = _tone(_HOTTEST_MEASURED, _BLOCK * 16)
    out = b"".join(_run(_VoiceVolume(200), pcm))
    assert _peak(out) <= _VOICE_CEILING
    assert _peak(out) >= 0.9, "调到 200% 反而没变响"
    quiet = b"".join(_run(_VoiceVolume(200), _tone(0.2, _BLOCK * 8))[1:])
    assert _peak(quiet) == pytest.approx(0.4, rel=0.02), "没碰到上限的段落也该是两倍"


def test_a_full_scale_square_at_200_stays_under_the_ceiling() -> None:
    """The worst input there is: every sample at full scale, doubled."""
    square = array("h", ((32767 if (i // 40) % 2 else -32768) for i in range(_BLOCK * 8)))
    out = b"".join(_run(_VoiceVolume(200), square.tobytes()))
    assert _peak(out) <= _VOICE_CEILING


def test_once_a_loud_word_has_passed_the_boost_comes_back() -> None:
    volume = _VoiceVolume(200)
    _run(volume, _tone(0.9, _BLOCK * 4))
    after = _run(volume, _tone(0.2, int(0.6 * _OUTPUT_RATE)))
    assert _peak(after[-1]) == pytest.approx(0.4, rel=0.03), "响的那个字过去了，增益没回来"


# ------------------------------------------------------------ error paths


def test_a_loud_block_nobody_saw_coming_steps_down_instead_of_wrapping() -> None:
    """The lookahead is whatever the ring held, and a jitter gap can leave it
    empty. Unguarded, a doubled full-scale sample does not even clip: it
    raises OverflowError inside the PortAudio callback, which stops the stream
    for the rest of the run. The block that arrives unannounced starts at the
    gain it can take instead, a step rather than a ramp."""
    volume = _VoiceVolume(200)
    volume.apply(_tone(0.1, _BLOCK), b"")  # quiet, nothing ahead: gain stays 2
    out = volume.apply(_tone(1.0, _BLOCK), b"")
    assert len(out) == _BLOCK * 2
    assert _peak(out) <= _VOICE_CEILING


def test_an_odd_byte_never_breaks_the_audio_callback() -> None:
    volume = _VoiceVolume(50)
    block = _tone(0.5, 100) + b"\x01"
    out = volume.apply(block, b"\x02")
    assert len(out) == len(block)


@pytest.mark.parametrize("percent", [-1, math.nan, math.inf])
def test_a_volume_the_schema_would_refuse_is_refused_here_too(percent: float) -> None:
    with pytest.raises(ValueError, match="voice volume"):
        _VoiceVolume(100).set_percent(percent)
