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


# Volcano refuses a blank voice now, and rightly: the server's default one
# brings its own character and she answers 「豆包」. Tests that are about
# something else still have to name one.
_VOLCANO_OK = {"volcano": {"speaker": "zh_female_vv_jupiter_bigtts"}}


def _request(provider: ProviderName, *, url: str = "", **kw: Any) -> LinkRequest:
    default: dict[str, Any] = {"provider": provider}
    if provider is ProviderName.VOLCANO:
        default |= _VOLCANO_OK
    settings = kw.pop("settings", None) or _settings(**default)
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


def test_openai_ga_builds_now_that_the_uplink_converts() -> None:
    """It was refused for months as 「not a shipping path」, which was true and
    useless: the adapter, capability bits, config section and panel metadata
    were all there. The actual blocker was one rate — 24 kHz uplink against
    the 16 kHz this chain captures — and naming it is what made it fixable."""
    built = build_link(
        _request(
            ProviderName.OPENAI_GA,
            url="wss://api.openai.com/v1/realtime",
            model="gpt-realtime-2.1",
            env={"api_key": "k"},
        )
    )
    assert isinstance(built.link, HostedLink)


def test_only_the_provider_that_needs_it_converts_the_uplink() -> None:
    """A resampler on a path that was already correct is pure cost on the hot
    path — one interpolation pass per 20 ms frame, fifty times a second."""
    from bilisama.realtime.providers import PROFILES
    from bilisama.realtime.resample import Resampler

    for provider, profile in PROFILES.items():
        converts = not Resampler(source_rate=16000, target_rate=profile.uplink_rate).passthrough
        assert converts is (
            provider is ProviderName.OPENAI_GA
        ), f"{provider.value} 的上行转换状态不对：uplink_rate={profile.uplink_rate}"


# ---------------------------------------------------------------- volcengine


def test_volcano_accepts_the_older_pair_too() -> None:
    """An account that predates API Key management only has these two."""
    settings = _settings(
        provider=ProviderName.VOLCANO,
        volcano={
            "app_id_ref": "env:VOLC_APP",
            "access_key_ref": "env:VOLC_TOKEN",
            **_VOLCANO_OK["volcano"],
        },
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


# ---------------------------------------------- credentials, voice and timing


def test_the_env_fallback_name_comes_off_the_profile() -> None:
    """It used to be a module-level dict keyed by ProviderName — a fifth table
    beside the four `ProviderProfile` exists to have absorbed, and the only one
    whose miss is a KeyError at a streamer's connect rather than at import.
    Neither mypy nor the factory's `assert_never` can see that.
    """
    from bilisama.realtime.providers import PROFILES

    for provider in ProviderName:
        name = PROFILES[provider].key_env
        if not name:
            continue
        built = build_link(_request(provider, env={name: "从环境来的"}))
        assert isinstance(built.link, HostedLink | VolcanoLink)


def test_the_older_pairs_access_token_is_not_read_as_an_api_key() -> None:
    """The two credential shapes are alternatives, not complements — this file,
    the schema comment and the runbook each say so, and the endpoint agrees:
    an API Key in the X-Api-Access-Key slot draws 401 「requested grant not
    found」. Reading `volcano_access_key` into the x-api-key header was the
    same mixture in the other direction, and no document admitted it existed.
    """
    with pytest.raises(SystemExit) as excinfo:
        build_link(_request(ProviderName.VOLCANO, env={"volcano_access_key": "老账号那半个"}))

    assert "缺火山凭据" in str(excinfo.value)


def test_the_command_line_voice_reaches_volcano_and_is_judged_on_the_way() -> None:
    """`--voice` beats the config on every other provider, and was dropped on
    the one where a wrong voice is fatal: `validate` makes the
    model/generation pairing a hard refusal, and passing the other voice is
    the obvious way to try the other generation. Silently ignoring it left the
    streamer watching a config value they thought they had overridden.
    """
    settings = _settings(
        provider=ProviderName.VOLCANO,
        volcano={"api_key_ref": "", "model": "1.2.1.1", "speaker": "zh_female_vv_jupiter_bigtts"},
    )
    built = build_link(
        _request(
            ProviderName.VOLCANO,
            settings=settings,
            env={"volcano_api_key": "k"},
            voice="zh_female_test",
        )
    )
    assert isinstance(built.link, VolcanoLink)
    assert built.link._speaker == "zh_female_test"

    # ...and the pairing is judged on the override, not on the config.
    with pytest.raises(SystemExit) as excinfo:
        build_link(
            _request(
                ProviderName.VOLCANO,
                settings=settings,
                env={"volcano_api_key": "k"},
                voice="saturn_zh_female_keainvsheng_tob",
            )
        )
    assert "克隆音色" in str(excinfo.value)


def test_every_link_answers_how_long_the_floor_holds() -> None:
    """L3 asks the LINK rather than doing arithmetic over provider config.

    Both earlier arrangements had the caller computing it: first a two-armed
    branch in dev_talk whose `else` handed a fourth provider DashScope's
    endpointing, then a registry function that still left two readers of the
    same fields at two altitudes. The numbers differ per backend, so a shared
    default would be the same bug wearing a hat.
    """
    windows = {}
    for provider in ProviderName:
        built = build_link(
            _request(provider, env={"ali_api_key": "k", "api_key": "k", "volcano_api_key": "k"})
        )
        windows[provider] = built.link.quiet_window_s
        assert windows[provider] > 0, provider

    assert windows[ProviderName.S2S] != windows[ProviderName.DASHSCOPE], "两条路的判停完全不同"


# ------------------------------------------------------- who may read what


# LinkRequest is one shape for every arm, which keeps the factory uniform and
# means nothing stops an arm reading a field that was meant for another
# provider. That is a real cost of the union, and this table is what turns it
# from "nobody has done it yet" into a rule. Shared fields are not listed;
# only the ones with an owner.
_FIELD_OWNERS = {
    "text_replies": {"S2S"},
    "bot_name": {"VOLCANO"},
    "voice": {"DASHSCOPE", "OPENAI_GA", "VOLCANO"},
}


def _request_fields_by_arm() -> dict[str, set[str]]:
    """Which `request.<field>` each arm of `build_link` reads, itself plus the
    helpers it calls."""
    import ast
    import inspect
    import textwrap

    from bilisama.realtime.providers import factory as mod

    module = ast.parse(textwrap.dedent(inspect.getsource(mod)))
    helpers = {
        node.name: node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }

    def reads(node: ast.AST, seen: set[str]) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "request"
            ):
                found.add(child.attr)
            # Only a helper handed the whole request can read its fields.
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in helpers
                and child.func.id not in seen
                and any(isinstance(a, ast.Name) and a.id == "request" for a in child.args)
            ):
                found |= reads(helpers[child.func.id], seen | {child.func.id})
        return found

    build = helpers["build_link"]
    match_stmt = next(n for n in ast.walk(build) if isinstance(n, ast.Match))
    out: dict[str, set[str]] = {}
    for case in match_stmt.cases:
        body = ast.Module(body=case.body, type_ignores=[])
        for name in ast.unparse(case.pattern).replace("ProviderName.", "").split(" | "):
            out.setdefault(name.strip(), set())
            out[name.strip()] |= reads(body, set())
    return out


def test_no_arm_reads_a_field_that_belongs_to_another_provider() -> None:
    """`text_replies` is s2s's, `bot_name` is volcano's, and the request object
    hands both to every arm. Nothing in the type system objects, and the
    dependency gate cannot see it — a provider quietly reading another's field
    would work, right up until the two disagree about what it means.
    """
    by_arm = _request_fields_by_arm()
    assert "VOLCANO" in by_arm, f"没解析出 volcano 那一臂：{sorted(by_arm)}"

    trespass = [
        f"{arm} 读了 {field}（那是 {'/'.join(sorted(owners))} 的）"
        for arm, fields in by_arm.items()
        for field, owners in _FIELD_OWNERS.items()
        if field in fields and arm not in owners
    ]
    assert not trespass, trespass


def test_the_ownership_table_still_describes_real_fields() -> None:
    """A table naming a field that no longer exists guards nothing."""
    for field in _FIELD_OWNERS:
        assert field in LinkRequest.__dataclass_fields__, field
