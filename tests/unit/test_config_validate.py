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

import ast
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pytest
from pydantic import BaseModel

from bilisama.config import ConfigProblem, ProviderName, Settings, check
from bilisama.config import validate as validate_module
from bilisama.config.schema import CURRENT_VERSION

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
    output_route: OutputRoute = "direct",
    echo_guard: EchoGuard = "duck",
    room_id: int = 0,
    credential_ref: str = "",
    growth_voice: str = "off",
    side_base_url: str = "",
    tts_voice: str = "",
    volcano_app_id_ref: str = "",
    volcano_api_key_ref: str = "",
    volcano_model: str = "1.2.1.1",
    volcano_speaker: str = "",
    config_version: int = CURRENT_VERSION,
    gift_battery_high: int = 1000,
    gift_battery_medium: int = 100,
    protect_paid_replies: bool = False,
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
    if provider is ProviderName.VOLCANO:
        speech[provider.value]["app_id_ref"] = volcano_app_id_ref
        speech[provider.value]["api_key_ref"] = volcano_api_key_ref
        speech[provider.value]["model"] = volcano_model
        speech[provider.value]["speaker"] = volcano_speaker
    speech["side"] = {"base_url": side_base_url}
    return Settings.model_validate(
        {
            "config_version": config_version,
            "speech": speech,
            "custom_tts": {"voice": tts_voice},
            "avatar": {"expression_source": expression_source},
            "audio": {"output_route": output_route, "echo_guard": echo_guard},
            "room": {"room_id": room_id, "credential_ref": credential_ref},
            "interaction": {
                "gift_battery_high": gift_battery_high,
                "gift_battery_medium": gift_battery_medium,
                "protect_paid_replies": protect_paid_replies,
            },
            "persona": {"growth": {"voice": growth_voice}},
        }
    )


def _fields(s: Settings, config_dir: Path | None = None) -> list[str]:
    return [p.field for p in check(s, config_dir=config_dir)]


def _one(s: Settings, field: str, config_dir: Path | None = None) -> ConfigProblem:
    """The single problem on `field`, or a failure naming everything reported."""
    matches = [p for p in check(s, config_dir=config_dir) if p.field == field]
    assert (
        len(matches) == 1
    ), f"expected exactly one problem on {field}, got {_fields(s, config_dir)}"
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


class _Broken(NamedTuple):
    """One deliberately broken config, plus the config dir `check` needs to see it.

    `config_dir` is None for every rule that reads nothing off disk. The wordlist
    rule is the exception, and it is exactly the rule that went untested for as
    long as this table only held Settings objects.
    """

    settings: Settings
    config_dir: Path | None = None

    def problems(self) -> list[ConfigProblem]:
        return check(self.settings, config_dir=self.config_dir)


# A directory that does not exist, so `wordlist.txt` under it cannot either. No
# tmp_path: this table is built at import time, and a stat on a missing path is
# the whole interaction with the filesystem.
_NO_CONFIG_DIR = Path(__file__).resolve().parent / "_no_such_config_dir"


# One deliberately broken config per rule. Reused by the tests that have to see
# every problem `check` can produce, not just the ones they happen to trip.
BROKEN_ONE_WAY_EACH = {
    "avatar.expression_source": _Broken(
        _settings(patches=("raw_instructions",), expression_source="tag")
    ),
    # virtual routes her voice out through something that is not the shell,
    # which is exactly what the canceller cannot see.
    "audio.output_route": _Broken(_settings(output_route="virtual")),
    "speech.s2s.llm_model": _Broken(_settings(llm_model="")),
    "speech.dashscope.endpoint": _Broken(
        _settings(provider=ProviderName.DASHSCOPE, endpoint="", expression_source="lexicon")
    ),
    "room.credential_ref": _Broken(_settings(room_id=12345, credential_ref="")),
    "speech.side.base_url": _Broken(_settings(growth_voice="collect", side_base_url="")),
    "config_version": _Broken(_settings(config_version=99)),
    # Inverted tiers pass ge=1 alone; only the order rule catches the config
    # in which the medium branch is unreachable.
    "interaction.gift_battery_medium": _Broken(
        _settings(gift_battery_high=100, gift_battery_medium=1000)
    ),
    "safety.wordlist_path": _Broken(
        _settings(room_id=12345, credential_ref="env:BILI_SESSDATA"), _NO_CONFIG_DIR
    ),
    # The window only truly holds on s2s; a hosted backend's own barge-in
    # still cancels her, so the switch there is half a promise (ledger #91).
    "interaction.protect_paid_replies": _Broken(
        _settings(
            provider=ProviderName.DASHSCOPE, expression_source="lexicon", protect_paid_replies=True
        )
    ),
    "custom_tts.engine": _Broken(
        _settings(provider=ProviderName.DASHSCOPE, expression_source="lexicon", tts_voice="知性")
    ),
    "speech.s2s.patches": _Broken(_settings(patches=("raw_instructions",), tts_voice="知性")),
    "speech.provider": _Broken(
        _settings(provider=ProviderName.OPENAI_GA, expression_source="lexicon")
    ),
    # Volcengine takes EITHER an api key on its own OR the older pair, so the
    # broken case is half a pair and no api key. Leaving everything blank would
    # also fire, but so would a rule that only ever looked at one field.
    # Two ways to pair a voice with the wrong model generation, and neither
    # announces itself at run time — one is silence, the other is her speaking
    # as somebody else. The fixture uses the silent one.
    "speech.volcano.speaker": _Broken(
        _settings(
            provider=ProviderName.VOLCANO,
            expression_source="lexicon",
            volcano_api_key_ref="volcano_api_key",
            volcano_model="2.2.0.0",
        )
    ),
    "speech.volcano.api_key_ref": _Broken(
        _settings(
            provider=ProviderName.VOLCANO,
            expression_source="lexicon",
            volcano_app_id_ref="volcano_app_id",
        )
    ),
}


def _rule_field_patterns() -> list[str]:
    """Every `field=` that `validate.check` can report, read out of its source.

    The table above is hand-maintained, so a rule nobody wrote a fixture for is
    invisible to the three whole-list tests below — which is how the wordlist
    rule (§7.6's one hard gate) stayed uncovered. This reads the rules from the
    only place that cannot lie about them.

    An f-string field becomes a glob: `f"speech.{provider}.endpoint"` reads back
    as `speech.*.endpoint`, which one concrete fixture is enough to satisfy.
    """
    source = Path(validate_module.__file__).read_text(encoding="utf-8")
    patterns: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ConfigProblem"):
            continue
        field = next((kw.value for kw in node.keywords if kw.arg == "field"), None)
        if isinstance(field, ast.Constant) and isinstance(field.value, str):
            patterns.append(field.value)
        elif isinstance(field, ast.JoinedStr):
            patterns.append(
                "".join(
                    str(part.value) if isinstance(part, ast.Constant) else "*"
                    for part in field.values
                )
            )
        else:  # pragma: no cover - a field built some third way needs a decision
            raise AssertionError(f"看不懂 validate.py 第 {node.lineno} 行的 field=")
    return patterns


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


def _providers_needing_a_configured_endpoint() -> list[ProviderName]:
    """Derived, never hand-listed.

    The old list was `[DASHSCOPE, OPENAI_GA]`, exhaustive right up to the day a
    fourth provider arrived — and then silently not, which is how the rule
    below came to refuse the config the runbook tells people to write.
    """
    from bilisama.realtime.providers import PROFILES

    return [p for p in ProviderName if p is not ProviderName.S2S and not PROFILES[p].default_url]


@pytest.mark.parametrize("provider", _providers_needing_a_configured_endpoint())
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
    # openai_ga keeps its own sample-rate note whatever the endpoint says; the
    # endpoint rule is the only thing this test is about.
    expected = ["speech.provider"] if provider is ProviderName.OPENAI_GA else []
    assert _fields(configured) == expected


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


def test_routing_her_voice_out_of_the_shell_is_advisory_not_fatal() -> None:
    """Sending playback down a virtual cable defeats echo cancellation.

    It used to be the recommended setup, back when nothing cancelled echo and
    keeping her voice out of the air was the whole trick. Now whatever plays
    the cable back out is another process, and the canceller only subtracts
    what the shell itself played.

    Advisory, not fatal: someone routing to OBS with headphones on is fine,
    and refusing to start would be wrong for them.
    """
    problem = _one(_settings(output_route="virtual"), "audio.output_route")
    assert problem.fatal is False
    assert check(_settings(output_route="direct")) == []
    # echo_guard has nothing to do with it either way.
    assert check(_settings(output_route="direct", echo_guard="off")) == []


@pytest.mark.parametrize("mode", ["collect", "on"])
def test_growth_without_side_model_is_advisory(mode: str) -> None:
    """Turning a growth layer on without its engine is a mistake worth flagging;
    refusing to start over it would be wrong — everything else works fine."""
    problem = _one(_settings(growth_voice=mode, side_base_url=""), "speech.side.base_url")
    assert problem.fatal is False


def test_growth_rule_stays_quiet_when_off_or_when_the_side_model_is_there() -> None:
    """The shipped default (growth off, side model unset) must stay clean — the
    proactive loop's same dependency is reported at runtime, not here, so a
    fresh install does not open on a warning it cannot yet act on."""
    assert check(_settings(growth_voice="off", side_base_url="")) == []
    assert check(_settings(growth_voice="on", side_base_url="http://127.0.0.1:9010/v1")) == []


def test_anonymous_room_is_advisory_and_silent_before_setup() -> None:
    """Nagging about credentials before a room is even configured trains people to
    ignore the whole list."""
    problem = _one(_settings(room_id=12345), "room.credential_ref")
    assert problem.fatal is False

    assert check(_settings(room_id=0)) == []
    assert check(_settings(room_id=12345, credential_ref="keychain:bili")) == []


# ------------------------------------------------------------ config_version


def test_a_config_from_a_newer_build_is_fatal() -> None:
    """Nothing can read it honestly: migrations only run forwards, and a value
    whose meaning changed keeps its old name."""
    problem = _one(_settings(config_version=CURRENT_VERSION + 1), "config_version")
    assert problem.fatal is True
    assert str(CURRENT_VERSION) in problem.message


@pytest.mark.parametrize("version", [CURRENT_VERSION, CURRENT_VERSION - 1])
def test_this_version_and_older_pass_the_validator(version: int) -> None:
    """An older file is the migrator's business (`config.migrate`), not this
    one's — by the time `check` runs it has already been walked forward."""
    assert "config_version" not in _fields(_settings(config_version=version))


# ------------------------------------------------------------ the wordlist gate


def _with_wordlist(root: Path) -> Path:
    """A config dir laid out the way `load_guard` resolves "auto"."""
    (root / "safety").mkdir(parents=True)
    (root / "safety" / "wordlist.txt").write_text("测试词\n", encoding="utf-8")
    return root


def test_a_missing_wordlist_refuses_to_start(tmp_path: Path) -> None:
    """§7.6's only hard gate: no output backstop, no stream.

    Fatal on purpose — everything else in this file that can be worked around is
    advisory, and this one cannot: what it protects against reaches the audience.
    """
    live = _settings(room_id=12345, credential_ref="env:BILI_SESSDATA")
    problem = _one(live, "safety.wordlist_path", tmp_path)
    assert problem.fatal is True
    assert str(tmp_path / "safety" / "wordlist.txt") in problem.message


def test_the_wordlist_gate_is_quiet_once_the_file_is_there(tmp_path: Path) -> None:
    live = _settings(room_id=12345, credential_ref="env:BILI_SESSDATA")
    assert check(live, config_dir=_with_wordlist(tmp_path)) == []


def test_the_wordlist_gate_follows_an_explicit_path(tmp_path: Path) -> None:
    """ "auto" is resolved against the config dir; anything else is taken as
    written, the same way `director.output_guard.load_guard` does it."""
    elsewhere = tmp_path / "elsewhere.txt"
    live = Settings.model_validate(
        {
            "speech": {"s2s": {"llm_model": "our-s2t-v1"}},
            "room": {"room_id": 12345, "credential_ref": "env:BILI_SESSDATA"},
            "safety": {"wordlist_path": str(elsewhere)},
        }
    )
    assert _fields(live, tmp_path) == ["safety.wordlist_path"]
    elsewhere.write_text("测试词\n", encoding="utf-8")
    assert check(live, config_dir=tmp_path) == []


def test_the_wordlist_gate_warns_before_a_room_is_configured(tmp_path: Path) -> None:
    """No room yet: told, not blocked.

    It used to say nothing at all, which meant the rule could not fire for the
    config that ships (room_id = 0) — so `bilisama config validate`, the check
    gate.sh runs on every commit, never once looked at the file. A fresh install
    still gets to start; what it does not get is silence.
    """
    problem = _one(_settings(room_id=0), "safety.wordlist_path", tmp_path)
    assert problem.fatal is False
    assert "--director" in problem.message


def test_the_wordlist_gate_cannot_fire_without_a_config_dir(tmp_path: Path) -> None:
    """`config_dir=None` means "do not touch the filesystem", not "clean".

    Every caller that can resolve the path passes it (cli.py:144,
    dev_talk.py:901, loader.py:86); this pins that a caller which cannot is
    skipping the check rather than passing it.
    """
    live = _settings(room_id=12345, credential_ref="env:BILI_SESSDATA")
    assert check(live) == []


# ------------------------------------------------------------ TTS ownership


def test_a_hand_set_custom_tts_is_flagged_when_the_provider_speaks_for_us() -> None:
    """§7.6 row 2. Nothing reads `[custom_tts]` while the provider makes its own
    audio, so a voice picked there is silently ignored — and on DashScope it even
    has a same-named twin that IS read (`speech.dashscope.voice`)."""
    hosted = _settings(
        provider=ProviderName.DASHSCOPE, expression_source="lexicon", tts_voice="知性"
    )
    problem = _one(hosted, "custom_tts.engine")
    assert problem.fatal is False
    assert "speech.dashscope.voice" in problem.fix


def test_an_untouched_custom_tts_section_says_nothing() -> None:
    """The shipped defaults are "not configured", and nagging every hosted run
    about a section nobody filled in is how a warning list stops being read."""
    hosted = _settings(provider=ProviderName.DASHSCOPE, expression_source="lexicon")
    assert check(hosted) == []


def test_custom_tts_is_fine_when_we_own_the_synthesiser() -> None:
    """Patched s2s hands us text; our own chain is exactly what speaks it."""
    ours = _settings(patches=("text_modality", "raw_instructions"), tts_voice="知性")
    assert check(ours) == []


def test_dropping_the_text_patch_while_custom_tts_is_configured_is_flagged() -> None:
    """§7.6 row 4, and it points at the patch rather than at the TTS: on this
    provider the patch list is the thing that decides who speaks."""
    s = _settings(patches=("raw_instructions",), tts_voice="知性")
    problem = _one(s, "speech.s2s.patches")
    assert problem.fatal is False
    assert "text_modality" in problem.fix


def test_the_two_tts_rules_never_both_fire() -> None:
    """They describe the same mistake from two sides, and a streamer reading two
    warnings about one setting starts discounting both."""
    for provider in ProviderName:
        for patches in (("text_modality", "raw_instructions"), ("raw_instructions",), ()):
            s = _settings(
                provider=provider, patches=patches, expression_source="lexicon", tts_voice="知性"
            )
            fired = set(_fields(s)) & {"custom_tts.engine", "speech.s2s.patches"}
            assert len(fired) <= 1, f"{provider}/{patches} 同时报了 {fired}"


# ------------------------------------------------------------ openai_ga


def test_openai_ga_is_flagged_as_a_reference_not_a_shipping_path() -> None:
    """§7.6 row 5, rewritten once the technical half went away.

    It used to warn about 24 kHz against our 16 kHz uplink and say the
    resampling was not written. It is written now (realtime/resample.py), and
    a warning that still described a solved problem would send someone to fix
    it twice. What is left is commercial and regulatory (plan §3.1: supported
    countries, and per-hour audio pricing against an always-open microphone),
    so it stays a note rather than a refusal.
    """
    problem = _one(
        _settings(provider=ProviderName.OPENAI_GA, expression_source="lexicon"),
        "speech.provider",
    )
    assert problem.fatal is False
    assert "重采样" not in problem.message, "这条障碍已经不在了，别再教人去修它"
    assert "出货" in problem.message


def test_the_sample_rate_note_is_only_for_openai_ga() -> None:
    """Every other provider, derived — the hand-written pair stopped meaning
    「only」 the moment a fourth one existed."""
    for provider in set(ProviderName) - {ProviderName.OPENAI_GA}:
        s = _settings(provider=provider, expression_source="lexicon")
        assert "speech.provider" not in _fields(s), provider


# ------------------------------------------------------------ the whole list


def test_problems_accumulate_and_never_short_circuit() -> None:
    """Three independent faults come back as three problems.

    Reporting the first one only would mean three restarts to find out about all
    three, which is how a setup session turns into a support ticket.
    """
    s = _settings(
        llm_model="",
        output_route="virtual",
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
    """Keeps the tests below honest.

    They only assert over problems they can produce, so a new rule that no fixture
    trips would sail past all of them.
    """
    for field, broken in BROKEN_ONE_WAY_EACH.items():
        assert field in [
            p.field for p in broken.problems()
        ], f"the fixture for {field} no longer trips that rule"


def test_no_rule_in_the_source_is_missing_a_fixture() -> None:
    """The teeth behind the test above: rules come from validate.py, not from us.

    §7.6's hard gate — a missing wordlist refuses to start — sat in `check` with
    no fixture and no test at all, because the table above is written by hand and
    a rule that is absent from it is simply invisible.
    """
    covered = set(BROKEN_ONE_WAY_EACH)
    orphans = [
        pattern
        for pattern in _rule_field_patterns()
        if not any(fnmatchcase(field, pattern) for field in covered)
    ]
    assert not orphans, f"validate.py 里这些规则没有对应的坏样本：{orphans}"


def test_every_problem_is_actionable() -> None:
    """Plan §7.6, rule 6: the streamer gets a next step, never a bare diagnosis.

    A `fix` that only rephrases the message leaves them exactly where they were.
    """
    for broken in BROKEN_ONE_WAY_EACH.values():
        for p in broken.problems():
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
    for broken in BROKEN_ONE_WAY_EACH.values():
        for p in broken.problems():
            assert _resolves(p.field), f"{p.field} is not a Settings field"


# --------------------------------------------- the fourth provider is not hosted


def test_a_backend_with_a_built_in_address_is_not_refused_for_leaving_it_blank() -> None:
    """`endpoint = ""` is the shipped value and what docs/runbook.md tells
    people to leave alone (「地址留空就用内置的公网地址」), because the registry
    carries a default and `resolve_endpoint` falls back to it. Refusing here
    made every strict load — `config show`, `render-s2s`, `persona review` —
    exit 2 on the config this repo ships.
    """
    from bilisama.realtime.providers import PROFILES

    have_default = [p for p in ProviderName if PROFILES[p].default_url]
    assert have_default, "没有任何 provider 自带地址，这条测试就没有对象了"
    for provider in have_default:
        s = _settings(provider=provider, endpoint="", expression_source="lexicon")
        assert f"speech.{provider.value}.endpoint" not in _fields(s), provider


def test_every_voice_fix_names_a_field_that_exists() -> None:
    """The `[custom_tts]` advice used to hard-code `.voice`, which volcano does
    not have — and its section forbids extras, so a streamer who followed the
    instruction would have the file rejected. The fix has to point somewhere
    real on every backend."""
    import re

    for provider in set(ProviderName) - {ProviderName.S2S}:
        s = _settings(provider=provider, expression_source="lexicon", tts_voice="知性")
        problem = _one(s, "custom_tts.engine")
        named = re.findall(r"speech\.[a-z_0-9.]+", problem.fix)
        assert named, problem.fix
        assert _resolves(named[0]), f"{named[0]} 这个字段不存在（{provider}）"


def test_an_empty_volcano_voice_is_refused_before_it_can_become_doubao() -> None:
    """The combination the shipped config used to carry, and the one nothing
    had ever tested: blank means 「the server picks」, and what it picks brings
    its own character. Reproduced on the real endpoint 2026-08-28 — she answers
    「豆包」, over `dialog.bot_name` and over a 557-character persona whose first
    sentence names her, with no error anywhere.

    Fatal because there is nothing to notice at run time: she talks, she sounds
    fine, she is somebody else.
    """
    for model in ("1.2.1.1", "2.2.0.0"):
        s = _settings(
            provider=ProviderName.VOLCANO,
            volcano_model=model,
            volcano_speaker="",
            volcano_api_key_ref="env:k",
            expression_source="lexicon",
        )
        problem = _one(s, "speech.volcano.speaker")
        assert problem.fatal is True, model
        assert "豆包" in problem.message
        assert "zh_female_vv_jupiter_bigtts" in problem.fix


def test_the_shipped_config_names_a_voice() -> None:
    """The rule above is only worth having if what we ship passes it. This
    file shipped a blank one with a comment saying that was fine."""
    from bilisama.cli import DEFAULT_CONFIG
    from bilisama.config import load
    from bilisama.config.validate import volcano_voice_problems

    volcano = load(DEFAULT_CONFIG, strict=False).speech.volcano
    assert volcano.speaker, "随包配置又把音色留空了"
    assert not volcano_voice_problems(volcano.model, volcano.speaker)


def test_paid_protection_is_only_questioned_where_it_cannot_hold() -> None:
    """Ledger #91: s2s honours the window, so the switch there is silent; a
    hosted backend gets the advisory, and with the switch off nobody hears
    about it at all."""
    assert "interaction.protect_paid_replies" not in _fields(
        _settings(provider=ProviderName.S2S, protect_paid_replies=True)
    )
    assert "interaction.protect_paid_replies" not in _fields(
        _settings(provider=ProviderName.DASHSCOPE, expression_source="lexicon")
    )
    problem = _one(
        _settings(
            provider=ProviderName.DASHSCOPE, expression_source="lexicon", protect_paid_replies=True
        ),
        "interaction.protect_paid_replies",
    )
    assert problem.fatal is False, "半截保护是提醒，不是拒绝启动"
