"""The provider registry and the capability checks that read it."""

from __future__ import annotations

import pytest

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.realtime import capabilities as caps_mod
from bilisama.realtime import dialect as dia
from bilisama.realtime.providers import PROFILES, profile_for, turn_type_problems


def test_every_provider_name_has_a_profile() -> None:
    """Adding a ProviderName without registering it must fail here, not in an
    adapter three stages later."""
    assert set(PROFILES) == set(ProviderName)


def test_every_hosted_provider_has_a_socket_path() -> None:
    """The address builder staples this onto a bare host. A hosted provider
    that left it empty dials the host's root — a 404 that reports as a refused
    handshake, which sends people checking a key that was never the problem."""
    hosted = set(ProviderName) - {ProviderName.S2S}
    assert all(PROFILES[p].socket_path for p in hosted)


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
    default = PROFILES[ProviderName.DASHSCOPE].default_model
    assert default, "注册表没给 DashScope 兜底模型，下面三条断言就没在测东西"

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


def test_every_provider_gets_a_quiet_window_from_its_own_numbers() -> None:
    """Plan section 3.3 rule 1, and the reason it moved out of the assembly.

    dev_talk computed this with `if s2s: ... else: dashscope`, so a fourth
    provider did not fail loudly — it silently got DashScope's endpointing.
    Volcengine waits 1.5 s before it calls a pause finished against
    DashScope's 300 ms, so the gate opened about 0.9 s early and she could cut
    in while the streamer was still mid-pause.
    """
    from bilisama.realtime.providers import quiet_window_s

    settings = Settings()
    windows = {p: quiet_window_s(settings, p) for p in ProviderName}
    assert all(w > 0 for w in windows.values())
    # Not the same number for everyone, which is the whole point: a constant
    # would pass a test that only checked "positive".
    assert windows[ProviderName.VOLCANO] != windows[ProviderName.DASHSCOPE]
    # Read off the shipped defaults, so a config change moves these with it.
    assert windows[ProviderName.VOLCANO] == pytest.approx(
        settings.speech.volcano.end_smooth_window_ms / 1000 + 0.3
    )
    assert windows[ProviderName.DASHSCOPE] == pytest.approx(
        settings.speech.dashscope.turn.silence_duration_ms / 1000 + 0.3
    )


def test_the_quiet_window_covers_the_s2s_two_stage_grace() -> None:
    """s2s is the one whose grace is two numbers, not one: the speculative
    reopen window, plus the extra delay an 'incomplete' verdict adds. Using
    either alone leaves a gap the rule exists to close."""
    from bilisama.realtime.providers import quiet_window_s

    settings = Settings()
    turn = settings.speech.s2s.turn
    assert quiet_window_s(settings, ProviderName.S2S) == pytest.approx(
        (turn.smart_turn_max_wait_ms + turn.smart_turn_incomplete_delay_ms) / 1000 + 0.3
    )
