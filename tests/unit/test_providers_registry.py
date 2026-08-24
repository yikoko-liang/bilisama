"""The provider registry and the capability checks that read it."""

from __future__ import annotations

import pytest

from bilisama.config.enums import ProviderName
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.providers import PROFILES, profile_for, turn_type_problems


def test_every_provider_name_has_a_profile() -> None:
    """Adding a ProviderName without registering it must fail here, not in an
    adapter three stages later."""
    assert set(PROFILES) == set(ProviderName)


def test_every_hosted_provider_has_a_socket_path() -> None:
    """The address builder indexes this table. A hosted provider missing from
    it is a KeyError at connect time — on the streamer's machine, mid-setup."""
    from bilisama.realtime.providers import _HOSTED_PATHS

    hosted = set(ProviderName) - {ProviderName.S2S}
    assert hosted <= set(_HOSTED_PATHS)


@pytest.mark.parametrize(
    ("provider", "caps", "codec"),
    [
        (ProviderName.S2S, caps_mod.S2S, dia.GA),
        (ProviderName.DASHSCOPE, caps_mod.DASHSCOPE, dia.BETA),
        (ProviderName.OPENAI_GA, caps_mod.OPENAI_GA, dia.GA),
    ],
)
def test_the_bindings_are_the_documented_ones(
    provider: ProviderName, caps: caps_mod.Capabilities, codec: dia.Codec
) -> None:
    """Plan section 3.1's table, as code: s2s and OpenAI speak GA, DashScope
    speaks the retired beta dialect."""
    profile = profile_for(provider)
    assert profile.caps is caps
    assert profile.codec is codec


def test_an_undeclared_turn_type_is_refused_with_a_fix() -> None:
    """Section 3.3: an unsupported type errors, it never silently downgrades."""
    problems = turn_type_problems(ProviderName.OPENAI_GA, "smart_turn")
    assert len(problems) == 1
    problem = problems[0]
    assert problem.field == "speech.openai_ga.turn.type"
    assert problem.fatal
    # Section 7.6: the fix names an action and the choices, not just the problem.
    assert "server_vad" in problem.fix


def test_a_declared_turn_type_passes() -> None:
    assert turn_type_problems(ProviderName.OPENAI_GA, "server_vad") == []
    assert turn_type_problems(ProviderName.DASHSCOPE, "semantic_vad") == []


def test_semantic_vad_is_refused_on_the_model_that_refuses_it() -> None:
    """Turn detection is a per-MODEL fact on DashScope, not a per-provider one
    (probed live 2026-08-10, capabilities.py's DASHSCOPE note). Checking only
    the provider let the factory default — qwen-audio-3.0-realtime-flash with
    semantic_vad — pass validation and then be refused by the real endpoint,
    which is worse than no check: it promises the config was looked at.
    """
    problems = turn_type_problems(
        ProviderName.DASHSCOPE, "semantic_vad", model="qwen-audio-3.0-realtime-flash"
    )
    assert len(problems) == 1
    assert problems[0].field == "speech.dashscope.turn.type"
    # The fix lists what THIS model takes, not what some other one does.
    assert "server_vad" in problems[0].fix
    assert "semantic_vad" not in problems[0].fix


def test_the_omni_model_keeps_semantic_vad() -> None:
    """A provider-level intersection would have refused it here too, and that
    is exactly the over-refusal the narrowing has to avoid."""
    assert (
        turn_type_problems(
            ProviderName.DASHSCOPE, "semantic_vad", model="qwen3.5-omni-flash-realtime"
        )
        == []
    )


@pytest.mark.parametrize("model", ["", "  ", "某个还没探过的模型"])
def test_an_unprobed_model_falls_back_to_the_provider_declaration(model: str) -> None:
    """We refuse what a model is KNOWN to refuse. Guessing about an unprobed
    one would block a working config on nothing but our own ignorance."""
    assert turn_type_problems(ProviderName.DASHSCOPE, "semantic_vad", model=model) == []


def test_the_factory_default_model_is_covered_by_the_narrowing() -> None:
    """The scenario the check exists for: config leaves [speech.dashscope].model
    blank, resolve_endpoint fills in this name, and the turn type is validated
    against it."""
    from bilisama.realtime.providers import _DEFAULT_MODELS

    default = _DEFAULT_MODELS[ProviderName.DASHSCOPE]
    assert turn_type_problems(ProviderName.DASHSCOPE, "semantic_vad", model=default)
    assert turn_type_problems(ProviderName.DASHSCOPE, "server_vad", model=default) == []
    assert turn_type_problems(ProviderName.DASHSCOPE, "smart_turn", model=default) == []


def test_dashscope_declares_smart_turn_and_the_single_slot() -> None:
    """Resolved by the real-endpoint probe (2026-08-10): smart_turn is genuine
    on DashScope — accepted and echoed — and the slot is single, which the old
    guess had backwards. The scheduler's merge strategy leans on these two."""
    caps = PROFILES[ProviderName.DASHSCOPE].caps
    assert "smart_turn" in caps.turn_detection_types
    assert caps.single_response_slot
    assert not caps.out_of_band_exempt_from_slot
