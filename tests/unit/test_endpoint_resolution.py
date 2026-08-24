"""Which layer decides where we dial, and in what order.

The pieces existed and disagreed. The key already did it right —
`secrets.resolve(api_key_ref) or env_key` — and so did the voice,
`args.voice or settings...voice`. But the provider, the address and the model
never joined them: dev-talk's argparse filled in defaults, and a default that
always has a value always wins, so `[speech]` was decoration. Four fields
nobody read, and a packaged app with no terminal and no path.sh had no way to
say where to connect.

One order, matching the two that were already right: command line, then
config, then environment.
"""

from __future__ import annotations

import pytest

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.realtime.providers import resolve_endpoint


def _settings(**speech: object) -> Settings:
    return Settings.model_validate({"speech": speech} if speech else {})


def test_the_command_line_wins() -> None:
    s = _settings(provider="dashscope", dashscope={"endpoint": "wss://配置里的/rt"})
    got = resolve_endpoint(s, provider="s2s", url="ws://命令行给的/rt", model=None, env={})
    assert got.provider is ProviderName.S2S
    assert got.url == "ws://命令行给的/rt"
    assert got.source == "命令行"


def test_config_wins_when_the_command_line_is_silent() -> None:
    """The whole point: an unspecified flag must not outrank the config."""
    s = _settings(provider="dashscope", dashscope={"endpoint": "wss://配置里的/rt", "model": "m1"})
    got = resolve_endpoint(s, provider=None, url=None, model=None, env={})
    assert got.provider is ProviderName.DASHSCOPE
    assert got.url == "wss://配置里的/rt"
    assert got.model == "m1"
    assert got.source == "配置"


def test_the_environment_is_the_last_resort() -> None:
    """path.sh keeps working on a dev box, but it stops being the only way."""
    s = _settings(provider="dashscope")
    got = resolve_endpoint(
        s, provider=None, url=None, model=None, env={"dashscope_url": "https://主机/x"}
    )
    assert got.url == "wss://主机/api-ws/v1/realtime"
    assert got.source == "环境变量"


def test_an_empty_config_field_counts_as_unset() -> None:
    """`endpoint = ""` ships in the factory file. It must not shadow the env."""
    s = _settings(provider="dashscope", dashscope={"endpoint": "", "model": ""})
    got = resolve_endpoint(s, provider=None, url=None, model=None, env={"dashscope_url": "主机"})
    assert got.url == "wss://主机/api-ws/v1/realtime"


def test_s2s_falls_back_to_its_own_configured_endpoint() -> None:
    s = _settings(provider="s2s", s2s={"endpoint": "ws://127.0.0.1:9999/v1/realtime"})
    got = resolve_endpoint(s, provider=None, url=None, model=None, env={})
    assert got.provider is ProviderName.S2S
    assert got.url == "ws://127.0.0.1:9999/v1/realtime"


def test_nothing_anywhere_says_so_in_chinese_with_a_fix() -> None:
    """A packaged app has no path.sh; the error has to name the other route."""
    s = _settings(provider="dashscope", dashscope={"endpoint": ""})
    with pytest.raises(SystemExit) as caught:
        resolve_endpoint(s, provider=None, url=None, model=None, env={})
    message = str(caught.value)
    assert "[speech.dashscope]" in message, message
    assert "endpoint" in message, message


def test_a_host_only_environment_value_still_becomes_a_socket_url() -> None:
    """path.sh holds an https base; the wire needs wss and a path."""
    for raw in ("主机", "https://主机", "https://主机/compatible-mode/v1"):
        got = resolve_endpoint(
            _settings(provider="dashscope"),
            provider=None,
            url=None,
            model=None,
            env={"dashscope_url": raw},
        )
        assert got.url == "wss://主机/api-ws/v1/realtime", raw


def test_an_already_complete_socket_url_is_left_alone() -> None:
    """Config carries the finished address; do not staple a second path on."""
    s = _settings(provider="dashscope", dashscope={"endpoint": "wss://主机/api-ws/v1/realtime"})
    got = resolve_endpoint(s, provider=None, url=None, model=None, env={})
    assert got.url == "wss://主机/api-ws/v1/realtime"


def test_openai_ga_gets_its_own_socket_path() -> None:
    """One hosted path for two providers was DashScope's. OpenAI GA serves
    Realtime on /v1/realtime — the same path our own s2s server uses
    (config/schema.py's S2SConfig.endpoint default), because both speak GA.
    Stapling /api-ws/v1/realtime on it dials a 404 and reports it as a refused
    handshake, which sends people checking their key.
    """
    s = _settings(provider="openai_ga", openai_ga={"endpoint": "api.openai.com"})
    got = resolve_endpoint(s, provider=None, url=None, model=None, env={})
    assert got.provider is ProviderName.OPENAI_GA
    assert got.url == "wss://api.openai.com/v1/realtime"


def test_the_dashscope_env_fallback_does_not_answer_for_openai() -> None:
    """`dashscope_url` is one vendor's address under one vendor's name. Letting
    it stand in for openai_ga sends OpenAI credentials at DashScope."""
    s = _settings(provider="openai_ga")
    with pytest.raises(SystemExit) as caught:
        resolve_endpoint(
            s, provider=None, url=None, model=None, env={"dashscope_url": "https://主机"}
        )
    message = str(caught.value)
    assert "[speech.openai_ga]" in message, message


def test_the_model_falls_back_to_the_known_default() -> None:
    """The flag used to carry it. Moved here, it stops outranking the config.

    `dev-talk --provider dashscope` on a box whose config leaves the field
    blank has to keep working, so the well-known name survives — just as the
    last layer instead of the first.
    """
    s = _settings(provider="dashscope", dashscope={"endpoint": "主机"})
    assert resolve_endpoint(s, provider=None, url=None, model=None, env={}).model == (
        "qwen-audio-3.0-realtime-flash"
    )


def test_a_configured_model_beats_the_fallback() -> None:
    s = _settings(provider="dashscope", dashscope={"endpoint": "主机", "model": "自己挑的"})
    assert resolve_endpoint(s, provider=None, url=None, model=None, env={}).model == "自己挑的"


def test_the_model_flag_beats_everything() -> None:
    s = _settings(provider="dashscope", dashscope={"endpoint": "主机", "model": "配置里的"})
    got = resolve_endpoint(s, provider=None, url=None, model="命令行的", env={})
    assert got.model == "命令行的"
