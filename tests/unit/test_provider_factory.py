"""The one branch that turns an endpoint into an adapter.

Everything here used to be an if/elif inside dev-talk, where it was reachable
only by running the whole director. The refusals in particular went untested:
each one is a message a streamer reads at the moment something is already
wrong, and a message that names the wrong fix costs more than no message.
"""

from __future__ import annotations

from typing import Any

import pytest

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.realtime.providers import Endpoint, resolve_endpoint
from bilisama.realtime.providers.factory import LinkRequest, build_link
from bilisama.realtime.providers.hosted import HostedLink
from bilisama.realtime.providers.s2s import S2SLink
from bilisama.realtime.providers.volcano import VolcanoLink


def _settings(**speech: Any) -> Settings:
    return Settings.model_validate({"speech": speech})


def _request(provider: ProviderName, *, url: str = "", **kw: Any) -> LinkRequest:
    settings = kw.pop("settings", None) or _settings(provider=provider)
    endpoint = Endpoint(provider, url or "wss://example.invalid/x", kw.pop("model", ""), "测试")
    return LinkRequest(endpoint=endpoint, settings=settings, **kw)


def test_every_provider_in_the_enum_has_an_arm() -> None:
    """Not one silently falling through to a refusal.

    The old `else` did exactly that: a provider wired into the registry, the
    capability table and the config schema still came out as "not supported",
    with nothing to say which of the four it was actually missing from.
    """
    for provider in ProviderName:
        try:
            build_link(_request(provider, env={"ali_api_key": "k", "api_key": "k"}))
        except SystemExit as exc:
            # A refusal is a legitimate answer, but it has to be a considered
            # one — the old catch-all message named no fix at all.
            assert str(exc).strip(), f"{provider.value} 被拒了却没说为什么"


def test_s2s_dials_the_address_it_was_given() -> None:
    built = build_link(_request(ProviderName.S2S, url="ws://127.0.0.1:8765/v1/realtime"))
    assert isinstance(built.link, S2SLink)
    assert built.url == "ws://127.0.0.1:8765/v1/realtime"


def test_a_hosted_link_gets_the_model_stapled_onto_its_address() -> None:
    """And the URL comes back, because the start banner prints it. Printing a
    guessed one is how a swallowed --model stayed invisible for weeks."""
    built = build_link(
        _request(
            ProviderName.DASHSCOPE,
            url="wss://host/api-ws/v1/realtime",
            model="qwen-flash",
            env={"ali_api_key": "k"},
        )
    )
    assert isinstance(built.link, HostedLink)
    assert built.url == "wss://host/api-ws/v1/realtime?model=qwen-flash"


def test_a_missing_hosted_key_names_both_places_to_put_one() -> None:
    with pytest.raises(SystemExit) as caught:
        build_link(_request(ProviderName.DASHSCOPE, env={}))
    message = str(caught.value)
    assert "api_key_ref" in message
    assert "path.sh" in message


def test_an_unsupported_turn_type_is_refused_before_the_socket_opens() -> None:
    """semantic_vad is real on qwen3.5-omni and refused by the registry's
    default model. Refusing here beats a session.update that gets silently
    ignored — the whole reason turn_type_problems exists."""
    settings = _settings(
        provider=ProviderName.DASHSCOPE,
        dashscope={"turn": {"type": "semantic_vad"}},
    )
    with pytest.raises(SystemExit) as caught:
        build_link(
            _request(
                ProviderName.DASHSCOPE,
                settings=settings,
                model="qwen-audio-3.0-realtime-flash",
                env={"ali_api_key": "k"},
            )
        )
    assert "semantic_vad" in str(caught.value)


def test_openai_ga_is_refused_for_the_reason_that_is_actually_blocking_it() -> None:
    """It used to be refused as "not a shipping path", which is true and
    useless: the adapter, capability bits, config section and panel metadata
    are all in place. What is missing is resampling — 24 kHz both ways against
    a 16 kHz uplink — and that is what the message has to say, because it is
    what someone would have to go build."""
    with pytest.raises(SystemExit) as caught:
        build_link(_request(ProviderName.OPENAI_GA, env={"api_key": "k"}))
    message = str(caught.value)
    assert "24" in message and "16" in message
    assert "重采样" in message


# ---------------------------------------------------------------- volcengine


def test_volcano_accepts_the_older_pair_too() -> None:
    """An account that predates API Key management only has these two."""
    settings = _settings(
        provider=ProviderName.VOLCANO,
        volcano={"app_id_ref": "env:VOLC_APP", "access_key_ref": "env:VOLC_TOKEN"},
    )
    import os

    os.environ["VOLC_APP"], os.environ["VOLC_TOKEN"] = "123456789", "token"
    try:
        built = build_link(_request(ProviderName.VOLCANO, settings=settings))
        assert isinstance(built.link, VolcanoLink)
    finally:
        del os.environ["VOLC_APP"], os.environ["VOLC_TOKEN"]


def test_half_a_legacy_pair_is_refused_with_both_shapes_named() -> None:
    """Two credential shapes, and half of one is not a credential. Probed live
    2026-08-27: an API Key put in the access-key slot draws 401 「requested
    grant not found」, so the message has to describe both shapes rather than
    let someone move a value into the wrong half of the other one."""
    with pytest.raises(SystemExit) as caught:
        build_link(_request(ProviderName.VOLCANO, env={"volcano_app_id": "app"}))
    message = str(caught.value)
    assert "API Key" in message
    assert "Access Token" in message


def test_an_api_key_alone_is_a_whole_credential() -> None:
    """The vendor's own words: 「在任意接口中，填入 header 即可，不用填写
    appid」. Confirmed live 2026-08-27 — x-api-key plus the resource headers
    completes the handshake with no App ID anywhere."""
    built = build_link(_request(ProviderName.VOLCANO, env={"volcano_api_key": "key"}))
    assert isinstance(built.link, VolcanoLink)


def test_volcano_falls_back_to_the_public_address() -> None:
    """Its endpoint is the same for everyone, unlike DashScope's tenant
    instance. Shipping it is what lets an installed app connect at all — no
    terminal, no path.sh, nowhere in the panel to type an endpoint."""
    endpoint = resolve_endpoint(
        _settings(provider=ProviderName.VOLCANO), provider=None, url=None, model=None, env={}
    )
    assert endpoint.url.startswith("wss://openspeech.bytedance.com")
    assert endpoint.source == "内置默认"


def test_a_deliberately_exported_address_still_outranks_the_built_in_one() -> None:
    """The fallback is last for a reason: a constant compiled in here must not
    quietly win over something someone went and set."""
    endpoint = resolve_endpoint(
        _settings(provider=ProviderName.VOLCANO, volcano={"endpoint": "wss://mine/x"}),
        provider=None,
        url=None,
        model=None,
        env={},
    )
    assert endpoint.url == "wss://mine/x"
    assert endpoint.source == "配置"


def test_dashscope_still_refuses_rather_than_inventing_an_address() -> None:
    """No default_url for this one: path.sh holds a tenant's own MaaS
    instance, so there is no address that would be right for everybody."""
    with pytest.raises(SystemExit) as caught:
        resolve_endpoint(
            _settings(provider=ProviderName.DASHSCOPE),
            provider=None,
            url=None,
            model=None,
            env={},
        )
    assert "endpoint 没填" in str(caught.value)
