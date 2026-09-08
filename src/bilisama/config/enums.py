"""Plain enums shared by schema and validate.

They live here so those two modules do not have to import each other.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    S2S = "s2s"
    DASHSCOPE = "dashscope"
    OPENAI_GA = "openai_ga"
    VOLCANO = "volcano"


class Chattiness(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GrowthMode(StrEnum):
    """Three-state switch for a machine-grown persona layer (plan section 4.6).

    COLLECT distills to disk but injects nothing — the trust ramp: the streamer
    reads what would be learned before letting it near the prompt.
    """

    OFF = "off"
    COLLECT = "collect"
    ON = "on"


class VoiceReplyMode(StrEnum):
    """Whether her own microphone turn plays unconditionally.

    WHEN_ADDRESSED: the voice gate (director/voice_turn.py) holds each turn
    for its head and drops the ones whose scene marker says the streamer was
    not talking to her. ALWAYS: today's behaviour — every VAD turn plays.
    """

    ALWAYS = "always"
    WHEN_ADDRESSED = "when_addressed"
