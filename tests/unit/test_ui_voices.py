"""voice_options: the provider decides what the voice half of the card shows.

Shapes only — the page renders select/text/none and never re-derives provider
rules, so the payload is the whole contract.
"""

from __future__ import annotations

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.config.voices import DASHSCOPE_VOICES
from bilisama.ui.voices import voice_options


def _settings(provider: ProviderName) -> Settings:
    settings = Settings()
    settings.speech.provider = provider
    return settings


def test_dashscope_offers_the_full_vetted_list_with_pitch_hints() -> None:
    settings = _settings(ProviderName.DASHSCOPE)
    settings.speech.dashscope.voice = "longanlingxin"
    options = voice_options(settings)
    assert options["mode"] == "select"
    assert options["path"] == "speech.dashscope.voice"
    assert options["current"] == "longanlingxin"
    assert [o["id"] for o in options["options"]] == [voice_id for voice_id, _ in DASHSCOPE_VOICES]
    assert options["options"][0]["hint"] == "150Hz"


def test_volcano_o2_offers_the_four_officials() -> None:
    settings = _settings(ProviderName.VOLCANO)
    settings.speech.volcano.model = "1.2.1.1"
    settings.speech.volcano.speaker = "zh_female_vv_jupiter_bigtts"
    options = voice_options(settings)
    assert options["mode"] == "select"
    assert options["path"] == "speech.volcano.speaker"
    assert len(options["options"]) == 4
    assert all(o["id"].endswith("_bigtts") for o in options["options"])


def test_volcano_sc2_gets_a_text_field_with_the_prefix_rule() -> None:
    settings = _settings(ProviderName.VOLCANO)
    settings.speech.volcano.model = "2.2.0.0"
    settings.speech.volcano.speaker = "saturn_xyz"
    options = voice_options(settings)
    assert options["mode"] == "text"
    assert options["path"] == "speech.volcano.speaker"
    assert options["current"] == "saturn_xyz"
    assert "saturn_" in options["hint"]


def test_s2s_and_openai_offer_nothing_with_a_reason() -> None:
    for provider in (ProviderName.S2S, ProviderName.OPENAI_GA):
        options = voice_options(_settings(provider))
        assert options["mode"] == "none"
        assert options["hint"]
        assert "path" not in options, "none 模式不该给可写路径"


def test_duplex_offers_the_probed_list_and_marks_the_shipped_default() -> None:
    """The duplex voice picker (2026-10-07): only voices that made a sound on
    the real endpoint — a wrong id there is silent with no error frame — with
    the shipped default marked, the four the vendor features first."""
    from bilisama.config.schema import VolcanoDuplexConfig
    from bilisama.config.voices import DUPLEX_FEATURED, DUPLEX_VOICES

    settings = _settings(ProviderName.VOLCANO_DUPLEX)
    settings.speech.volcano_duplex.voice = "zh_male_m191_uranus_bigtts"
    options = voice_options(settings)
    assert options["mode"] == "select"
    assert options["path"] == "speech.volcano_duplex.voice"
    assert options["current"] == "zh_male_m191_uranus_bigtts"
    assert options["default"] == VolcanoDuplexConfig().voice == "zh_female_vv_uranus_bigtts"
    assert {o["id"] for o in options["options"]} == {voice_id for voice_id, _, _ in DUPLEX_VOICES}
    first = options["options"][0]
    assert first["id"] == DUPLEX_FEATURED[0]
    assert first["label"] == "Vivi 2.0"
    assert first["hint"].endswith("Hz")
    groups = [o["group"] for o in options["options"]]
    assert groups[: len(DUPLEX_FEATURED)] == ["官方主推"] * len(DUPLEX_FEATURED)
    assert set(groups) <= {"官方主推", "女声", "男声"}
