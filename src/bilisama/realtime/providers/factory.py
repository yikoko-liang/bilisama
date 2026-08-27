"""The one place that turns a resolved endpoint into a SpeechLink.

Before this module, dev-talk carried an if/elif over ProviderName that knew
each backend's credential variable, its turn-type check and its URL shape —
product code holding provider knowledge, which is the leak plan section 3.7
exists to prevent. Worse, its `else` was a refusal, so a provider added to the
registry, the config schema and the capability table still fell through to
"not supported" with no hint about which of the four tables it was missing
from.

This is ONE branch, not zero. A registry where adapters sign themselves up
would need something to import the adapter modules first, and that import is
both invisible and load-order dependent; a match statement in the module whose
whole job is choosing beats it.

What it does NOT buy is "one arm and one PROFILES entry", which is what this
docstring used to claim. Volcano took nine touchpoints: `config/enums.py`,
`PROFILES`, `capabilities.py`, the arm below, `quiet_window_s`, `schema.py`,
`validate.py`, `ui_meta.py` and `config/bilisama.toml`. The arm being in one
file is still right; the claim about the saving was not, and it would send the
next author looking in one place instead of nine.

Lazy imports on purpose: hosted.py and s2s.py import the package this module
lives in, so importing them at module scope would close a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

from bilisama import secrets
from bilisama.config.enums import ProviderName
from bilisama.config.validate import volcano_voice_problems
from bilisama.realtime import link
from bilisama.realtime.providers import (
    Endpoint,
    profile_for,
    quiet_window_s,
    turn_type_problems,
    with_model,
)

if TYPE_CHECKING:
    from bilisama.config.schema import Settings

__all__ = ["BuiltLink", "LinkRequest", "build_link", "hosted_key"]


@dataclass(frozen=True, slots=True)
class LinkRequest:
    """Everything any adapter might need, so the factory's arms stay uniform.

    A dataclass rather than a widening keyword list: the arms differ in which
    fields they read, and a new provider that needs one more input should not
    make every caller pass it.

    The cost, stated rather than hidden: some fields below belong to exactly
    one arm (`text_replies` to s2s, `bot_name` to volcano), so this is a union
    with no narrowing and nothing stops an arm reading a field that was not
    meant for it. At four providers that is cheaper than four request types;
    if it reaches the point where most fields are single-owner, per-provider
    payloads are the shape to move to.
    """

    endpoint: Endpoint
    settings: Settings

    voice: str = ""
    """Command-line override. Empty falls back to the provider's config."""

    bot_name: str = ""
    """What she calls herself, for the one provider that takes it as a field
    of its own rather than reading it out of the persona text.

    Supplied by the caller rather than derived here: the answer is
    persona.template_variables' {{agentName}}, and reaching for it from L2
    would make the adapter layer depend on L3 — the direction plan section 2.3
    only allows the other way round. The dependency gate does not watch this
    direction, so it is a rule kept by hand."""

    model_explicit: bool = False
    """Whether --model supplied the name. Decides whether it may replace a
    model already written into the address (see with_model)."""

    env: Mapping[str, str] = field(default_factory=dict)
    """Consulted only for credentials, and only after api_key_ref."""

    text_replies: bool = False
    """s2s only: ask for text instead of audio. False is the shipping default
    — it stands against the zero-patch official pipeline, whose own TTS does
    the speaking, and a text-pinned session would mute every reply until the
    TTS chain of stage 4 exists."""


@dataclass(frozen=True, slots=True)
class BuiltLink:
    """The adapter, plus the address it will actually dial.

    The URL comes back rather than staying inside because the start banner has
    to print the real one. It used to be printed from a variable the caller
    still had in hand, and the comment beside it earned its place: a banner
    that guesses which model won is how a swallowed --model stayed invisible
    for weeks. Now that the joining happens in here, handing it back is the
    only way that line can keep telling the truth.
    """

    link: link.SpeechLink
    url: str


def hosted_key(settings: Settings, provider: ProviderName, env: Mapping[str, str]) -> str:
    """Config reference first, path.sh second — the order the key already used.

    Public because dev-talk's bare-link mode needs the same answer and used to
    carry its own copy, message text and all. Two copies of a credential rule
    is two chances for them to disagree about which layer wins.

    Raises:
        SystemExit: Neither layer had one, naming both places to fix.
    """
    section = getattr(settings.speech, provider.value)
    # The env name lives on the profile. It used to be a module-level dict
    # keyed by the same enum — a fifth table beside the four ProviderProfile
    # exists to have absorbed, and one whose miss is a KeyError at a
    # streamer's connect that neither mypy nor assert_never can see.
    env_name = profile_for(provider).key_env
    key = secrets.resolve(section.api_key_ref) or (env.get(env_name, "") if env_name else "")
    if not key:
        raise SystemExit(
            f"缺 {provider.value} 凭据：配 [speech.{provider.value}] api_key_ref，"
            "或先 source path.sh。"
        )
    return key


def _build_hosted(request: LinkRequest, provider: ProviderName) -> BuiltLink:
    """DashScope and OpenAI GA: same adapter, same shape, different key name."""
    from bilisama.realtime.providers.hosted import HostedLink

    section = getattr(request.settings.speech, provider.value)
    # The registry knows which turn types this endpoint really honours, and it
    # narrows by MODEL, not just by provider: semantic_vad exists on
    # qwen3.5-omni and is refused by qwen-audio-3.0-realtime-flash, which is
    # the registry default. Refusing here beats a session.update that gets
    # silently ignored.
    for problem in turn_type_problems(provider, section.turn.type, model=request.endpoint.model):
        raise SystemExit(f"{problem.message} {problem.fix}")
    key = hosted_key(request.settings, provider, request.env)
    url = with_model(request.endpoint.url, request.endpoint.model, explicit=request.model_explicit)
    return BuiltLink(
        HostedLink(
            url,
            provider,
            headers={"Authorization": f"Bearer {key}"},
            turn=section.turn,
            voice=request.voice or section.voice,
            session_cap_min=section.session_cap_min,
            quiet_window_s=quiet_window_s(request.settings, provider),
        ),
        url,
    )


def build_link(request: LinkRequest) -> BuiltLink:
    """Build the adapter for whichever provider the endpoint resolved to.

    Raises:
        SystemExit: The provider needs something this machine has not got —
            a credential, a supported turn type, or, for openai_ga, code that
            does not exist yet. Every message names the fix.
    """
    provider = request.endpoint.provider

    match provider:
        case ProviderName.S2S:
            from bilisama.realtime.providers.s2s import S2SLink

            return BuiltLink(
                S2SLink(
                    request.endpoint.url,
                    text_replies=request.text_replies,
                    quiet_window_s=quiet_window_s(request.settings, provider),
                ),
                request.endpoint.url,
            )

        case ProviderName.DASHSCOPE | ProviderName.OPENAI_GA:
            # One body, because the difference between them is data — the
            # profile's codec, capabilities, key name and uplink rate — and
            # not behaviour. openai_ga was unblocked 2026-08-27: what had been
            # missing was the uplink rate conversion, which now rides on the
            # profile (realtime/resample.py).
            #
            # It is still the development reference rather than a shipping
            # path, and plan section 3.1 has the reasons — commercial and
            # regulatory, not technical. Nothing here enforces that; the config
            # simply defaults elsewhere.
            return _build_hosted(request, provider)

        case ProviderName.VOLCANO:
            from bilisama.realtime.providers.volcano import VolcanoLink

            cfg = request.settings.speech.volcano
            # One credential or two, and they are alternatives rather than
            # complements. Probed live 2026-08-27: an API Key placed in the
            # X-Api-Access-Key slot draws 401 「requested grant not found」,
            # so guessing which one a value is would fail in a way whose
            # error text names neither.
            #
            # Which is also why `volcano_access_key` is NOT read here as a
            # third source for the api_key slot. It is the older pair's second
            # half; feeding it to the x-api-key header is precisely the mixture
            # this file, the schema comment and the runbook each say is
            # impossible, and it had no documentation anywhere.
            api_key = secrets.resolve(cfg.api_key_ref) or request.env.get(
                profile_for(provider).key_env, ""
            )
            app_id = secrets.resolve(cfg.app_id_ref) or request.env.get("volcano_app_id", "")
            legacy_key = secrets.resolve(cfg.access_key_ref) or ""
            if not api_key and not (app_id and legacy_key):
                raise SystemExit(
                    "缺火山凭据。\n"
                    "推荐：控制台 > API Key 管理里拿一个 API Key，填进 "
                    "[speech.volcano] api_key_ref，或 export volcano_api_key。\n"
                    "老账号也可以用 App ID ＋ Access Token 那一对，填 app_id_ref "
                    "和 access_key_ref。"
                )
            # `--voice` overrides the config, so the model/generation pairing
            # has to be judged on what will actually be sent. Checking only the
            # config left the obvious way to try the other generation — pass
            # the other voice on the command line — silently wrong, and both
            # halves of wrong are silent on this endpoint: SC with an official
            # voice says nothing at all, O with a cloned one speaks as somebody
            # else. No turn-type check here: this protocol declares server_vad
            # and nothing else, so asking would be asking a constant.
            speaker = request.voice or cfg.speaker
            # The RESOLVED model, not the config's: `--model` outranks it in
            # resolve_endpoint and then used to be dropped here, so the banner
            # printed one generation while the wire carried the other.
            model = request.endpoint.model or cfg.model
            for problem in volcano_voice_problems(model, speaker):
                raise SystemExit(f"{problem.message} {problem.fix}")
            return BuiltLink(
                VolcanoLink(
                    request.endpoint.url,
                    bot_name=request.bot_name,
                    api_key=api_key,
                    app_id=app_id,
                    access_key=legacy_key,
                    config=cfg,
                    speaker=speaker,
                    model=model,
                    quiet_window_s=quiet_window_s(request.settings, provider),
                ),
                request.endpoint.url,
            )

        case _ as unhandled:
            # assert_never rather than a friendly fallback: it makes mypy check
            # the match covers ProviderName, so a provider added to the enum
            # without an arm here goes red in the type gate instead of at a
            # streamer's connect. Verified by adding volcano to the enum before
            # writing its arm — mypy failed, as intended.
            assert_never(unhandled)
