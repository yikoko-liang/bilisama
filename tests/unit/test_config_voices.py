"""The voice lists and the prose that cites them stay one story.

config/voices.py is the single source; the runbook tables and validate.py's
example strings all read from or are checked against it here — the failure
mode is a list updated in one place and a streamer following the other.
"""

from __future__ import annotations

from pathlib import Path

from bilisama.config.validate import volcano_voice_problems
from bilisama.config.voices import DASHSCOPE_VOICES, VOLCANO_OFFICIAL_SPEAKERS

_RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "runbook.md"


def test_every_listed_voice_appears_in_the_runbook_tables() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    for voice_id, hz in DASHSCOPE_VOICES:
        assert voice_id in text, f"DashScope 音色 {voice_id} 不在 runbook 表里"
        assert f"{hz}Hz" in text, f"{voice_id} 的基频 {hz}Hz 不在 runbook 表里"
    for speaker in VOLCANO_OFFICIAL_SPEAKERS:
        assert speaker in text, f"火山官方音色 {speaker} 不在 runbook 里"


def test_the_official_volcano_speakers_pass_their_own_generation() -> None:
    """The whole point of the list: every id we offer must be safe on O2.0 —
    the wrong pairing swaps her persona or mutes her with no wire error."""
    for speaker in VOLCANO_OFFICIAL_SPEAKERS:
        assert volcano_voice_problems("1.2.1.1", speaker) == [], speaker


def test_the_shipped_dashscope_default_is_on_the_list() -> None:
    assert "longanlingxin" in {voice_id for voice_id, _ in DASHSCOPE_VOICES}


def test_the_lists_hold_the_probed_counts() -> None:
    """15 and 4 were what the real endpoints yielded (2026-08); a silent
    shrink here would quietly narrow the picker."""
    assert len(DASHSCOPE_VOICES) == 15
    assert len(VOLCANO_OFFICIAL_SPEAKERS) == 4
