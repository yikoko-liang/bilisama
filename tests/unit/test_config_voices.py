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


def test_every_duplex_voice_appears_in_the_runbook_with_its_pitch() -> None:
    from bilisama.config.voices import DUPLEX_VOICES

    text = _RUNBOOK.read_text(encoding="utf-8")
    for voice_id, name, hz in DUPLEX_VOICES:
        assert (
            f"`{voice_id}` | {hz}Hz" in text
        ), f"duplex 音色 {name}（{voice_id}）不在 runbook 表里"


def test_the_shipped_duplex_voice_leads_the_featured_four_and_passes_its_rule() -> None:
    """The picker marks the shipped default and validate.py warns about any id
    off the list; the default being on it is what keeps a fresh install quiet."""
    from bilisama.config.schema import VolcanoDuplexConfig
    from bilisama.config.validate import duplex_voice_problems
    from bilisama.config.voices import DUPLEX_FEATURED, DUPLEX_VOICES

    shipped = VolcanoDuplexConfig().voice
    assert shipped == DUPLEX_FEATURED[0]
    assert set(DUPLEX_FEATURED) <= {voice_id for voice_id, _, _ in DUPLEX_VOICES}
    assert [voice_id for voice_id, _, _ in DUPLEX_VOICES[: len(DUPLEX_FEATURED)]] == list(
        DUPLEX_FEATURED
    )
    for voice_id, _, _ in DUPLEX_VOICES:
        assert duplex_voice_problems(voice_id) == [], voice_id


def test_the_duplex_list_holds_the_swept_count() -> None:
    """All 90 Chinese voices of the vendor's full-duplex table made a sound on
    the real endpoint (2026-10-07); a silent shrink would narrow the picker."""
    from bilisama.config.voices import DUPLEX_VOICES

    assert len(DUPLEX_VOICES) == 90
    assert len({voice_id for voice_id, _, _ in DUPLEX_VOICES}) == 90
