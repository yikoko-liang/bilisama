"""The branch matrix of `validate.check`, rule by rule.

`check` is the only thing standing between a plausible-looking TOML and a stream
that goes wrong in a way nobody can diagnose from the config file. Each rule is
tested twice — it fires when it should, and it stays quiet when it should not —
because a rule that fires on everything gets ignored just as fast as one that
never fires.

The other half is plan §7.6: a problem is only useful if the streamer can act on
it. `fix` has to name an action, so that bar is asserted here rather than left to
review. Where the CLI turns these into printed output is test_config_validation.py.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel

from bilisama.config import ConfigProblem, ProviderName, Settings, check

ExpressionSource = Literal["tag", "lexicon", "tool_call"]
OutputRoute = Literal["virtual", "direct"]
EchoGuard = Literal["duck", "off"]
Patch = Literal["text_modality", "raw_instructions"]

# What the streamer is being told to go and do. Plan §7.6 asks for a fix that
# names an action, not one that restates the problem in other words.
ACTION_VERBS = ("改", "填", "换", "开", "关", "装", "戴", "选", "扫码", "点")

# A fix that opens with any of these is describing the state, not an action —
# "没有登录凭据" tells the streamer what we already said in the message.
DIAGNOSIS_OPENERS = ("没有", "缺少", "不", "无法", "会", "当前")


def _settings(
    *,
    provider: ProviderName = ProviderName.S2S,
    llm_model: str = "our-s2t-v1",
    patches: tuple[Patch, ...] = ("text_modality", "raw_instructions"),
    endpoint: str = "wss://example.invalid/realtime",
    expression_source: ExpressionSource = "tag",
    output_route: OutputRoute = "virtual",
    echo_guard: EchoGuard = "duck",
    room_id: int = 0,
    credential_ref: str = "",
) -> Settings:
    """Shipped defaults with a model id, and one axis moved off it.

    Built through `model_validate` rather than by assigning to a loaded object:
    the models do not validate on assignment, so a mutated Settings can hold a
    value the schema would have refused, and the test would be checking a state
    that cannot occur.
    """
    speech: dict[str, Any] = {
        "provider": provider,
        "s2s": {"llm_model": llm_model, "patches": patches},
    }
    if provider is not ProviderName.S2S:
        speech[provider.value] = {"endpoint": endpoint}
    return Settings.model_validate(
        {
            "speech": speech,
            "avatar": {"expression_source": expression_source},
            "audio": {"output_route": output_route, "echo_guard": echo_guard},
            "room": {"room_id": room_id, "credential_ref": credential_ref},
        }
    )


def _fields(s: Settings) -> list[str]:
    return [p.field for p in check(s)]


def _one(s: Settings, field: str) -> ConfigProblem:
    """The single problem on `field`, or a failure naming everything reported."""
    matches = [p for p in check(s) if p.field == field]
    assert len(matches) == 1, f"expected exactly one problem on {field}, got {_fields(s)}"
    return matches[0]


def _resolves(path: str) -> bool:
    """Does a dotted field path name a real Settings field?

    The settings page uses `field` to jump to the control, so a stale path is a
    dead link rather than a crash — nothing else would notice it.
    """
    model: type[BaseModel] = Settings
    *parents, leaf = path.split(".")
    for part in parents:
        info = model.model_fields.get(part)
        annotation = info.annotation if info is not None else None
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return False
        model = annotation
    return leaf in model.model_fields


# One deliberately broken config per rule. Reused by the two tests that have to
# see every problem `check` can produce, not just the ones they happen to trip.
BROKEN_ONE_WAY_EACH = {
    "avatar.expression_source": _settings(patches=("raw_instructions",), expression_source="tag"),
    "audio.output_route": _settings(output_route="direct", echo_guard="off"),
    "speech.s2s.llm_model": _settings(llm_model=""),
    "speech.dashscope.endpoint": _settings(
        provider=ProviderName.DASHSCOPE, endpoint="", expression_source="lexicon"
    ),
    "room.credential_ref": _settings(room_id=12345, credential_ref=""),
}


# ------------------------------------------------------------ the quiet case


def test_clean_config_reports_nothing() -> None:
    """Defaults plus a model id start up clean.

    Also pins the baseline every single-axis test below leans on: if the helper's
    own default were already reporting something, "stays quiet" would be checking
    nothing at all.
    """
    shipped = Settings.model_validate({"speech": {"s2s": {"llm_model": "our-s2t-v1"}}})
    assert check(shipped) == []
    assert check(_settings()) == []


# ------------------------------------------------------------ per-rule matrix


def test_s2s_without_llm_model_is_fatal() -> None:
    """Self-hosted speech-to-speech cannot pick a model for us."""
    problem = _one(_settings(llm_model=""), "speech.s2s.llm_model")
    assert problem.fatal is True
    assert problem.message and problem.fix


def test_the_llm_model_rule_ignores_a_hosted_provider() -> None:
    """`[speech.s2s]` keeps its values while a hosted provider is selected, and a
    stale model id there is nobody's problem."""
    hosted = _settings(provider=ProviderName.DASHSCOPE, llm_model="", expression_source="lexicon")
    assert "speech.s2s.llm_model" not in _fields(hosted)


@pytest.mark.parametrize("provider", [ProviderName.DASHSCOPE, ProviderName.OPENAI_GA])
def test_hosted_provider_without_endpoint_is_fatal(provider: ProviderName) -> None:
    """Guards the `getattr(s.speech, provider.value)` indirection.

    Rename a sub-model on SpeechConfig and that lookup starts reading the wrong
    object — or raising AttributeError inside a validator — with no other signal.
    """
    missing = _settings(provider=provider, endpoint="", expression_source="lexicon")
    problem = _one(missing, f"speech.{provider.value}.endpoint")
    assert problem.fatal is True
    assert problem.message and problem.fix

    configured = _settings(provider=provider, expression_source="lexicon")
    assert check(configured) == []


@pytest.mark.parametrize(
    ("provider", "patches", "expression_source", "flagged"),
    [
        # The provider does not speak for us: our own TTS reads the text, and the
        # tag is stripped before it gets there.
        (ProviderName.S2S, ("text_modality", "raw_instructions"), "tag", False),
        # Same provider, patch dropped: it produces audio itself and reads the
        # tag out loud.
        (ProviderName.S2S, ("raw_instructions",), "tag", True),
        (ProviderName.DASHSCOPE, ("text_modality", "raw_instructions"), "tag", True),
        (ProviderName.DASHSCOPE, ("text_modality", "raw_instructions"), "lexicon", False),
    ],
)
def test_inline_tags_flagged_only_when_the_provider_owns_tts(
    provider: ProviderName,
    patches: tuple[Patch, ...],
    expression_source: ExpressionSource,
    flagged: bool,
) -> None:
    """The one branch whose truth table is not obvious.

    Who owns TTS depends on the provider *and* on whether the text_modality patch
    is applied, so the same `expression_source = "tag"` is fine under one pair and
    read aloud to the audience under another.
    """
    s = _settings(provider=provider, patches=patches, expression_source=expression_source)
    assert ("avatar.expression_source" in _fields(s)) is flagged


def test_echo_guard_problem_is_advisory_not_fatal() -> None:
    """Only the pair is a problem, so fixing either side clears it.

    Advisory on purpose: plenty of people run direct output on headphones, and
    refusing to start would be wrong for them.
    """
    problem = _one(_settings(output_route="direct", echo_guard="off"), "audio.output_route")
    assert problem.fatal is False

    assert check(_settings(output_route="direct", echo_guard="duck")) == []
    assert check(_settings(output_route="virtual", echo_guard="off")) == []


def test_anonymous_room_is_advisory_and_silent_before_setup() -> None:
    """Nagging about credentials before a room is even configured trains people to
    ignore the whole list."""
    problem = _one(_settings(room_id=12345), "room.credential_ref")
    assert problem.fatal is False

    assert check(_settings(room_id=0)) == []
    assert check(_settings(room_id=12345, credential_ref="keychain:bili")) == []


# ------------------------------------------------------------ the whole list


def test_problems_accumulate_and_never_short_circuit() -> None:
    """Three independent faults come back as three problems.

    Reporting the first one only would mean three restarts to find out about all
    three, which is how a setup session turns into a support ticket.
    """
    s = _settings(
        llm_model="",
        output_route="direct",
        echo_guard="off",
        room_id=12345,
    )
    problems = check(s)
    assert sorted(p.field for p in problems) == [
        "audio.output_route",
        "room.credential_ref",
        "speech.s2s.llm_model",
    ]
    assert [p.field for p in problems if p.fatal] == ["speech.s2s.llm_model"]


def test_every_rule_is_covered_by_the_broken_fixtures() -> None:
    """Keeps the two tests below honest.

    They only assert over problems they can produce, so a new rule that no fixture
    trips would sail past both of them.
    """
    for field, s in BROKEN_ONE_WAY_EACH.items():
        assert field in _fields(s), f"the fixture for {field} no longer trips that rule"


def test_every_problem_is_actionable() -> None:
    """Plan §7.6, rule 6: the streamer gets a next step, never a bare diagnosis.

    A `fix` that only rephrases the message leaves them exactly where they were.
    """
    for s in BROKEN_ONE_WAY_EACH.values():
        for p in check(s):
            assert p.message, f"{p.field} has no message"
            assert p.fix, f"{p.field} has no fix"
            assert any(
                verb in p.fix for verb in ACTION_VERBS
            ), f"{p.field}'s fix names no action: {p.fix}"
            assert not p.fix.startswith(
                DIAGNOSIS_OPENERS
            ), f"{p.field}'s fix reads as a diagnosis: {p.fix}"
            assert (
                p.fix not in p.message and p.message not in p.fix
            ), f"{p.field}'s fix just restates the message: {p.fix}"
            # Both strings are read by a Chinese streamer (§1.4 rule 16).
            # 一-鿿 is the CJK Unified Ideographs block.
            assert any(
                "一" <= ch <= "鿿" for ch in p.message + p.fix
            ), f"{p.field} is not written in Chinese: {p.message} / {p.fix}"


def test_every_problem_points_at_a_real_settings_field() -> None:
    """A field path the settings page cannot resolve is a dead link, and the only
    thing that would ever notice is a person clicking it."""
    for s in BROKEN_ONE_WAY_EACH.values():
        for p in check(s):
            assert _resolves(p.field), f"{p.field} is not a Settings field"
