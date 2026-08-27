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

__all__ = [
    "PROFILES",
    "ProviderProfile",
    "compose_instructions",
    "profile_for",
    "turn_type_problems",
]


def compose_instructions(context: str, turn: str | None) -> str | None:
    """Per-turn instructions on top of the persona, never instead of it.

    The Realtime protocol makes response.instructions REPLACE the session's for
    that response — upstream picks either/or
    (base_openai_compatible_language_model.py:709-711). The scheduler sends
    only the per-turn ask and assumes the persona stays; probed live
    2026-08-14, a session-level persona vanished from every
    instruction-carrying reply until this recomposition.

    Shared by every adapter rather than copied into each: the semantics are the
    protocol's, not one provider's, and a fix to the joint (or to the empty-turn
    edge) must not land in one copy only.

    Args:
        context: The session-level persona, "" when none was pushed.
        turn: This turn's ask, or None.

    Returns:
        The composed instructions, or None to stay bare — the server then falls
        back to the session instructions, which are exactly the persona.
    """
    if not turn:
        # None and "" both mean "no per-turn ask": staying bare beats sending a
        # dangling "本轮要求：" tail.
        return None
    if not context:
        return turn
    return f"{context}\n\n本轮要求：{turn}"


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
