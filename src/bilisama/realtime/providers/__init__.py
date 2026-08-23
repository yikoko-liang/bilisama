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

from collections.abc import Mapping
from dataclasses import dataclass

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.config.validate import ConfigProblem
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.capabilities import Capabilities

__all__ = [
    "PROFILES",
    "Endpoint",
    "ProviderProfile",
    "compose_instructions",
    "profile_for",
    "resolve_endpoint",
    "turn_type_problems",
]

# Fallback name for the environment variable path.sh has always used. Lower
# case and unprefixed because that is what path.sh writes, not because it is
# a good name for a global.
_DASHSCOPE_URL_ENV = "dashscope_url"

# What a hosted address looks like once it reaches the socket. path.sh holds
# an https base for the REST API, so the host is the only reusable part.
_HOSTED_PATH = "/api-ws/v1/realtime"

# The model to ask for when no layer names one. Kept here rather than in
# argparse because position matters: as a flag default it outranked the
# config, which is the bug this module exists to fix. As the last fallback it
# only decides when nobody else did, so `dev-talk --provider dashscope` keeps
# working on a box whose config leaves the field blank.
_DEFAULT_MODELS: dict[ProviderName, str] = {
    ProviderName.DASHSCOPE: "qwen-audio-3.0-realtime-flash",
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where to dial, and which layer said so.

    `source` exists to be printed. Three layers can supply an address, and a
    streamer looking at a wrong one needs to know which of the three to go
    edit — the side-model line already does this ("来自 [speech.side]").
    """

    provider: ProviderName
    url: str
    model: str
    source: str


def _hosted_url(raw: str) -> str:
    """Normalise whatever a human or path.sh wrote into a socket address.

    Three shapes turn up in practice: a bare host, the https REST base that
    path.sh carries, and an address someone already finished by hand. Only
    the last one is left alone; the others get the scheme and path they need.
    """
    if raw.startswith(("ws://", "wss://")):
        return raw
    host = raw.replace("https://", "").replace("http://", "").split("/")[0]
    return f"wss://{host}{_HOSTED_PATH}"


def resolve_endpoint(
    settings: Settings,
    *,
    provider: str | None,
    url: str | None,
    model: str | None,
    env: Mapping[str, str],
) -> Endpoint:
    """Decide where to connect: command line, then config, then environment.

    That order is not new — the API key and the voice already follow it. The
    provider, the address and the model did not, because argparse handed them
    defaults, and a default that always holds a value always wins. So
    `[speech]` decided nothing, and an installed app — no terminal, no
    path.sh — had no way at all to say where to connect.

    Pure on purpose: no file reads, no network, no os.environ. The caller
    passes the environment in, so this can be tested without one.

    Args:
        provider: The --provider flag, or None when it was not given.
        url: The --url flag, or None.
        model: The --model flag, or None.
        env: Environment to consult as the last resort.

    Raises:
        SystemExit: No layer supplied an address, with the two places to fix.
    """
    chosen = ProviderName(provider) if provider else settings.speech.provider
    hosted = getattr(settings.speech, chosen.value)
    # An empty string is what the factory file ships, so it means "unset"
    # rather than "deliberately blank" — otherwise it would shadow the
    # environment on every clean checkout.
    configured = (hosted.endpoint or "").strip()
    picked_model = (
        model or getattr(hosted, "model", "") or _DEFAULT_MODELS.get(chosen, "")
    ).strip()

    if url:
        return Endpoint(chosen, url, picked_model, "命令行")
    if configured:
        address = configured if chosen is ProviderName.S2S else _hosted_url(configured)
        return Endpoint(chosen, address, picked_model, "配置")
    if chosen is not ProviderName.S2S:
        from_env = (env.get(_DASHSCOPE_URL_ENV) or "").strip()
        if from_env:
            return Endpoint(chosen, _hosted_url(from_env), picked_model, "环境变量")

    raise SystemExit(
        f"不知道该连哪儿：[speech.{chosen.value}] 的 endpoint 没填，命令行也没给 --url。\n"
        f"填上 bilisama.toml 里那一行，或者开发机上 source path.sh 用 {_DASHSCOPE_URL_ENV} 兜底。"
    )


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
