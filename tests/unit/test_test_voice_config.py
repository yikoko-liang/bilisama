"""Cloud test speech has an independent, secret-free configuration."""

import pytest
from pydantic import ValidationError

from bilisama.config.schema import Settings


def test_test_voice_defaults_to_seed_tts_without_changing_assistant_voice() -> None:
    settings = Settings()
    voice = settings.test_voice
    assert voice.resource_id == "seed-tts-2.0"
    assert voice.speaker == "zh_female_vv_uranus_bigtts"
    assert voice.api_key_ref == "volcano_api_key"
    assert voice.endpoint == "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
    assert voice.request_timeout_s == 60
    assert voice.speech_rate == 0
    assert settings.speech.volcano.speaker == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"request_timeout_s": 0},
        {"request_timeout_s": float("inf")},
        {"speech_rate": -51},
        {"speech_rate": 101},
        {"speaker": ""},
        {"unexpected": True},
    ],
)
def test_test_voice_rejects_invalid_configuration(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"test_voice": changes})
