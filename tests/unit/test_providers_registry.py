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


def test_dashscope_still_declares_the_suspect_value() -> None:
    """DASHSCOPE lists "smart_turn", which is speech-to-speech's own feature
    name, not a protocol value — flagged for the section 13 item 5 endpoint
    test. This pin makes sure resolving that question is a deliberate edit
    here, not a silent drift."""
    assert "smart_turn" in PROFILES[ProviderName.DASHSCOPE].caps.turn_detection_types
