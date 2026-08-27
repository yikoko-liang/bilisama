"""One real speech waveform, shared by the contract suites that need one.

Both the hosted and the volcano contract tests have to hand an endpoint
something a server-side VAD will accept as a person talking, and both had their
own copy of this — same voice, same sentence, same skip. A synthesised file
rather than a microphone: nothing here opens a device, so the tests stay
runnable on a machine with no audio at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

__all__ = ["speech_wav"]


def speech_wav(tmp_path: Path, said: str = "你好，我说句话打断一下，今天天气怎么样啊") -> Path:
    """A real speech waveform, synthesised to a file. No device is opened.

    Silence cannot stand in for it: a server-side VAD will not fire on silence,
    and a test that feeds silence and then reports "inconclusive" never had a
    chance.

    Args:
        tmp_path: Where to write it.
        said: What to say. The words do not matter to any assertion; they exist
            so a human listening to the file can tell which test made it.

    Returns:
        The path to a 16 kHz mono LEI16 WAV.
    """
    out = tmp_path / "speech.wav"
    try:
        subprocess.run(
            [
                "say",
                "-v",
                "Tingting",
                "-o",
                str(out),
                "--data-format=LEI16@16000",
                said,
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"造不出语音素材（macOS say 不可用）：{exc}")
    return out
