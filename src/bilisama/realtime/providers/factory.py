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
whole job is choosing beats it. Adding a provider means adding one arm here
and one entry in PROFILES.

Lazy imports on purpose: hosted.py and s2s.py import the package this module
lives in, so importing them at module scope would close a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

from bilisama import secrets
from bilisama.config.enums import ProviderName
from bilisama.realtime import link
from bilisama.realtime.providers import Endpoint, turn_type_problems, with_model

if TYPE_CHECKING:
    from bilisama.config.schema import Settings

__all__ = ["BuiltLink", "LinkRequest", "build_link"]

# The names path.sh has always written: lower case and unprefixed because that
# is what the file holds, not because they are good names for globals. Each is
# the LAST resort — a configured api_key_ref outranks it.
_KEY_ENV: dict[ProviderName, str] = {
    ProviderName.DASHSCOPE: "ali_api_key",
    ProviderName.OPENAI_GA: "api_key",
}


@dataclass(frozen=True, slots=True)
class LinkRequest:
    """Everything any adapter might need, so the factory's arms stay uniform.

    A dataclass rather than a widening keyword list: the arms differ in which
    fields they read, and a new provider that needs one more input should not
    make every caller pass it.
    """

    endpoint: Endpoint
    settings: Settings

    voice: str = ""
    """Command-line override. Empty falls back to the provider's config."""

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


def _hosted_key(request: LinkRequest, provider: ProviderName) -> str:
    """Config reference first, path.sh second — the order the key already used.

    Raises:
        SystemExit: Neither layer had one, naming both places to fix.
    """
    section = getattr(request.settings.speech, provider.value)
    key = secrets.resolve(section.api_key_ref) or request.env.get(_KEY_ENV[provider], "")
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
    key = _hosted_key(request, provider)
    url = with_model(request.endpoint.url, request.endpoint.model, explicit=request.model_explicit)
    return BuiltLink(
        HostedLink(
            url,
            provider,
            headers={"Authorization": f"Bearer {key}"},
            turn=section.turn,
            voice=request.voice or section.voice,
            session_cap_min=section.session_cap_min,
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
                S2SLink(request.endpoint.url, text_replies=request.text_replies),
                request.endpoint.url,
            )

        case ProviderName.DASHSCOPE:
            return _build_hosted(request, provider)

        case ProviderName.OPENAI_GA:
            # The adapter itself is ready — HostedLink serves this profile and
            # the capability bits, config section and panel metadata are all
            # in place. What is missing is a pair of resamplers: this endpoint
            # is 24 kHz in BOTH directions while every other path here sends
            # 16 kHz uplink, and nothing in src/ resamples anything. Dialing it
            # anyway would connect, sound wrong, and read as a model fault.
            #
            # So the refusal stays, but it now names the thing that is
            # actually missing rather than "not a shipping path" — and it
            # lives in an arm of its own, so this provider is no longer
            # refused by falling off the end of a branch.
            raise SystemExit(
                "openai_ga 还差一步：它收发都是 24kHz，而这条链路的上行按 16kHz 发，"
                "中间的重采样还没写。\n"
                "先用 dashscope 或自建那两条；真要走它，得先补上收发两侧的重采样。"
            )

        case ProviderName.VOLCANO:
            from bilisama.realtime.providers.volcano import VolcanoLink

            cfg = request.settings.speech.volcano
            # Two credentials, not one, so the missing-credential message has
            # to say WHICH — "缺凭据" when only the access key is absent sends
            # people to re-check an app id that was fine.
            app_id = secrets.resolve(cfg.app_id_ref) or request.env.get("volcano_app_id", "")
            access_key = secrets.resolve(cfg.access_key_ref) or request.env.get(
                "volcano_access_key", ""
            )
            missing = [
                name
                for name, value in (("app_id_ref", app_id), ("access_key_ref", access_key))
                if not value
            ]
            if missing:
                raise SystemExit(
                    f"缺火山凭据（{'、'.join(missing)}）：配 [speech.volcano] 里对应的引用，"
                    "或先 source path.sh 设好 volcano_app_id / volcano_access_key。"
                )
            for problem in turn_type_problems(provider, "server_vad"):
                raise SystemExit(f"{problem.message} {problem.fix}")
            return BuiltLink(
                VolcanoLink(
                    request.endpoint.url,
                    app_id=app_id,
                    access_key=access_key,
                    config=cfg,
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
