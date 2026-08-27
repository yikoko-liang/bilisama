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
from urllib.parse import parse_qsl, urlencode

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
    "codec_for",
    "compose_instructions",
    "profile_for",
    "resolve_endpoint",
    "turn_type_problems",
    "with_model",
]

# Fallback name for the environment variable path.sh has always used. Lower
# case and unprefixed because that is what path.sh writes, not because it is
# a good name for a global.
_DASHSCOPE_URL_ENV = "dashscope_url"


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


def _hosted_url(raw: str, provider: ProviderName) -> str:
    """Normalise whatever a human or path.sh wrote into a socket address.

    Three shapes turn up in practice: a bare host, the https REST base that
    path.sh carries, and an address someone already finished by hand. Only
    the last one is left alone; the others get the scheme and path they need.
    """
    if raw.startswith(("ws://", "wss://")):
        return raw
    host = raw.replace("https://", "").replace("http://", "").split("/")[0]
    return f"wss://{host}{PROFILES[provider].socket_path}"


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
    picked_model = (model or getattr(hosted, "model", "") or PROFILES[chosen].default_model).strip()

    if url:
        return Endpoint(chosen, url, picked_model, "命令行")
    if configured:
        address = configured if chosen is ProviderName.S2S else _hosted_url(configured, chosen)
        return Endpoint(chosen, address, picked_model, "配置")
    # DashScope only: the variable is one vendor's address under one vendor's
    # name, and letting it answer for openai_ga would point OpenAI credentials
    # at DashScope's socket.
    if chosen is ProviderName.DASHSCOPE:
        from_env = (env.get(_DASHSCOPE_URL_ENV) or "").strip()
        if from_env:
            return Endpoint(chosen, _hosted_url(from_env, chosen), picked_model, "环境变量")

    # Last, and only where the address is the same for everyone. It ranks
    # BELOW the environment on purpose: a variable someone deliberately
    # exported should still win over a constant compiled in here.
    if PROFILES[chosen].default_url:
        return Endpoint(chosen, PROFILES[chosen].default_url, picked_model, "内置默认")

    # The env fallback is only offered where it exists, so the fix line never
    # sends an openai_ga user to source a file that cannot help them.
    fallback = (
        f"，或者开发机上 source path.sh 用 {_DASHSCOPE_URL_ENV} 兜底"
        if chosen is ProviderName.DASHSCOPE
        else ""
    )
    raise SystemExit(
        f"不知道该连哪儿：[speech.{chosen.value}] 的 endpoint 没填，命令行也没给 --url。\n"
        f"填上 bilisama.toml 里那一行{fallback}。"
    )


def with_model(url: str, model: str, *, explicit: bool = False) -> str:
    """Name the model in the query string, the way hosted endpoints expect.

    Deciding WHICH model, and which address, is resolve_endpoint's job just
    above; this only joins the two. Idempotent because an address may arrive already
    finished: someone can paste a complete URL into config or --url, and
    stapling a second ?model= on would make it unroutable.

    That idempotence used to be absolute, and it silently ate `--model`. The
    hosted address shape carries the model in its query, so the endpoint line
    a streamer pastes into bilisama.toml usually already has one; the flag
    ranks FIRST in resolve_endpoint (realtime/providers/__init__.py:117-119)
    and then lost right here. `explicit` is that ranking, carried down one
    level: a model the command line asked for replaces the address's, and a
    model that merely fell out of the config or the registry default does not
    — between two config-level answers the more specific one should win.

    Args:
        url: The resolved address, with or without a query.
        model: The resolved model name; "" for a provider that has none.
        explicit: Whether `--model` supplied it.
    """
    if not model:
        return url
    base, question, query = url.partition("?")
    named = parse_qsl(query, keep_blank_values=True)
    # Whole parameter names, not a substring: "model=" in url also matched
    # llm_model= and submodel=, and then refused to add the model actually
    # asked for.
    if not any(key == "model" for key, _ in named):
        return f"{url}{'&' if question else '?'}model={model}"
    if not explicit:
        return url
    kept = [(key, value) for key, value in named if key != "model"]
    kept.append(("model", model))
    return f"{base}?{urlencode(kept)}"


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
    """Everything the code knows about one provider, in one place.

    It used to hold two fields while three sibling tables — hosted paths,
    default models, session caps — sat beside it keyed by the same enum, one
    of them in another module entirely. Four tables keyed alike are four
    chances to add a provider to three of them, and the fourth failure is a
    KeyError at connect time rather than at import.
    """

    caps: Capabilities

    codec: dia.Codec | None
    """The OpenAI-Realtime dialect, or None for a provider that speaks its own
    protocol. Reach it through `codec_for` when a codec is required."""

    socket_path: str = ""
    """Path to staple onto a bare host. DashScope serves Realtime on its own
    path while OpenAI GA uses /v1/realtime — the same path our s2s server
    answers on (config/schema.py:51), because both speak GA. Stapling
    DashScope's path onto an OpenAI host dials a 404 and reports it as a
    refused handshake, which sends people checking a key that was never the
    problem. Empty means addresses arrive complete (s2s)."""

    default_model: str = ""
    """The model to ask for when no layer names one. Here rather than in
    argparse because position matters: as a flag default it outranked the
    config, which is the bug resolve_endpoint exists to fix. As the last
    fallback it only decides when nobody else did."""

    session_cap_min: int = 0
    """How long this endpoint lets one connection live, in minutes; 0 means no
    published cap. Not a capability — capabilities.py's own docstring puts
    session limits with the adapters, because a field only the adapter reads
    is an adapter constant. It sits here because it is per-provider data that
    the adapter should not have to be told."""

    default_url: str = ""
    """A complete address to fall back on when no layer named one.

    Only for providers whose address is genuinely universal. DashScope's is
    not — path.sh holds a tenant's own MaaS instance — so it stays empty
    there and the refusal keeps pointing at the config. Where it IS universal,
    shipping it is what lets an installed app connect at all: no terminal, no
    path.sh, and nowhere in the panel to type an endpoint (backlog item 55)."""


PROFILES: dict[ProviderName, ProviderProfile] = {
    ProviderName.S2S: ProviderProfile(caps_mod.S2S, dia.GA),
    ProviderName.DASHSCOPE: ProviderProfile(
        caps_mod.DASHSCOPE,
        dia.BETA,
        socket_path="/api-ws/v1/realtime",
        default_model="qwen-audio-3.0-realtime-flash",
        session_cap_min=120,
    ),
    ProviderName.OPENAI_GA: ProviderProfile(
        caps_mod.OPENAI_GA,
        dia.GA,
        socket_path="/v1/realtime",
        session_cap_min=60,
    ),
    ProviderName.VOLCANO: ProviderProfile(
        caps_mod.VOLCANO,
        # Binary frames and numbered events — neither dialect fits, so there is
        # no codec. volcano_wire.py does this one's encoding.
        None,
        socket_path="/api/v3/realtime/dialogue",
        # No per-connection cap published; a 10-minute IDLE timeout instead,
        # which our never-silent uplink (plan section 3.3 rule 7) cannot reach.
        session_cap_min=0,
        default_url="wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
    ),
}


def profile_for(provider: ProviderName) -> ProviderProfile:
    return PROFILES[provider]


def codec_for(provider: ProviderName) -> dia.Codec:
    """The dialect codec, for the code paths that cannot work without one.

    Raises:
        ValueError: This provider speaks its own protocol. Loud on purpose —
            the alternative is handing RealtimeClient a None it would only
            trip over several frames later, somewhere that reads like a
            server fault.
    """
    codec = PROFILES[provider].codec
    if codec is None:
        raise ValueError(
            f"{provider.value} 不说 OpenAI Realtime 方言，没有 codec 可用——"
            "这条路径要的是能说方言的 provider。"
        )
    return codec


def turn_type_problems(
    provider: ProviderName, turn_type: str, *, model: str = ""
) -> list[ConfigProblem]:
    """Refuse a turn-detection type the provider or the model never declared.

    Plan section 3.3 is explicit: an unsupported type must be an error, never a
    silent downgrade — speech-to-speech accepts semantic_vad on the wire and
    then ignores it (vad_handler.py:173-202 reads only threshold and
    silence_duration_ms), which is exactly the failure mode this check exists
    to catch before a stream starts.

    Args:
        provider: Which backend the config selected.
        turn_type: The configured `turn.type`.
        model: The model that will actually be dialed, when the caller knows
            it. DashScope answers differently per model (capabilities.py's
            DASHSCOPE note), so a provider-level pass on qwen-audio-3.0 plus
            semantic_vad is a pass the endpoint will not honour. Empty means
            "unknown", and then only the provider declaration applies.
    """
    declared = caps_mod.for_model(PROFILES[provider].caps, model).turn_detection_types
    if turn_type in declared:
        return []
    return [
        ConfigProblem(
            field=f"speech.{provider.value}.turn.type",
            message=f"这个语音后端不支持「{turn_type}」判停，写了也会被静默忽略。",
            fix=f"改成它声明过的类型之一：{'、'.join(sorted(declared))}。",
        )
    ]
