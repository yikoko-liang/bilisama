"""Provider registry: the code-level binding config names lacked.

Until now `ProviderName.S2S` and `capabilities.S2S` merely happened to share a
name — no module imported both, so a renamed constant or a new provider could
drift apart silently. The registry is the single place that says which
capability set and which dialect each provider speaks; adapters and validation
both read it here.

Import direction: this package may import config (foundation) and the sibling
realtime modules. Nothing under director/, persona/, memory/ or tools/ may
import this package — the dependency gate enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass

from bilisama.config.enums import ProviderName
from bilisama.config.validate import ConfigProblem
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.capabilities import Capabilities

__all__ = ["PROFILES", "ProviderProfile", "profile_for", "turn_type_problems"]


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """What one provider speaks: its capability bits and its wire dialect."""

    caps: Capabilities
    codec: dia.Codec


PROFILES: dict[ProviderName, ProviderProfile] = {
    ProviderName.S2S: ProviderProfile(caps_mod.S2S, dia.GA),
    ProviderName.DASHSCOPE: ProviderProfile(caps_mod.DASHSCOPE, dia.BETA),
    ProviderName.OPENAI_GA: ProviderProfile(caps_mod.OPENAI_GA, dia.GA),
}


def profile_for(provider: ProviderName) -> ProviderProfile:
    return PROFILES[provider]


def turn_type_problems(provider: ProviderName, turn_type: str) -> list[ConfigProblem]:
    """Refuse a turn-detection type the provider never declared.

    Plan section 3.3 is explicit: an unsupported type must be an error, never a
    silent downgrade — speech-to-speech accepts semantic_vad on the wire and
    then ignores it (vad_handler.py:173-202 reads only threshold and
    silence_duration_ms), which is exactly the failure mode this check exists
    to catch before a stream starts.
    """
    declared = PROFILES[provider].caps.turn_detection_types
    if turn_type in declared:
        return []
    return [
        ConfigProblem(
            field=f"speech.{provider.value}.turn.type",
            message=f"这个语音后端不支持「{turn_type}」判停，写了也会被静默忽略。",
            fix=f"改成它声明过的类型之一：{'、'.join(sorted(declared))}。",
        )
    ]
