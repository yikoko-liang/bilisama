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
