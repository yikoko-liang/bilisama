"""Rendering the speech-to-speech launch config, and reconciling it upstream.

test_turn_fields_match_upstream is the gate that keeps two promises at once: every
turn-detection parameter is passed through, and a misspelled key cannot slip past
upstream's allow_extra_keys parsing.

The reconciliation tests come in two flavours. The ones marked skipif need a real
checkout because they assert things about upstream itself. The ones built on
_fake_upstream assert things about our own three-state logic, so they run anywhere.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import pytest

import bilisama.bootstrap.s2s_launch as s2s_launch
from bilisama import cli
from bilisama.config import S2SConfig, TurnConfig

# The upstream checkout is normally a sibling of this repo. The env var wins so CI
# can point elsewhere.
_REPO_ROOT = Path(__file__).resolve().parents[2]
S2S_ROOT = Path(os.environ.get("BILISAMA_S2S_ROOT", _REPO_ROOT.parent / "speech-to-speech"))

_MINIMAL_CONFIG = """
config_version = 1
[speech]
provider = "s2s"
[speech.s2s]
llm_model = "our-s2t-v1"
"""


def _cfg(**kw: object) -> S2SConfig:
    return S2SConfig(llm_model="our-s2t-v1", **kw)


def _fake_upstream(root: Path, fields: Iterable[str]) -> Path:
    """Write the one upstream file `upstream_field_names` scans, holding `fields`.

    Args:
        root: Stands in for a speech-to-speech checkout. Created if absent.
        fields: Field names upstream should be seen to accept.

    Returns:
        `root`, so callers can pass it straight to s2s_root.
    """
    arg_dir = root / "src" / "speech_to_speech" / s2s_launch._UPSTREAM_ARG_DIR
    arg_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    {name}: str = ''" for name in sorted(fields))
    (arg_dir / "vad_arguments.py").write_text(
        f"class VADHandlerArguments:\n{body}\n", encoding="utf-8"
    )
    return root


def test_render_skips_stt_and_pins_chat_completions() -> None:
    payload = s2s_launch.render(_cfg())
    assert payload["stt"] == "none"
    assert payload["llm_backend"] == "chat-completions"
    assert payload["num_pipelines"] == 1
    # Burns CPU for nothing once STT is skipped.
    assert payload["enable_live_transcription"] is False


def test_render_never_sets_mac_optimal_settings() -> None:
    # It quietly moves a dozen other defaults, which is one more layer to debug.
    assert "mac_optimal_settings" not in s2s_launch.render(_cfg())


def test_render_drops_infinite_max_speech() -> None:
    # inf is not valid JSON, and upstream reads a missing value as "no limit".
    assert "max_speech_ms" not in s2s_launch.render(_cfg())
    payload = s2s_launch.render(_cfg(turn=TurnConfig(max_speech_ms=30_000)))
    assert payload["max_speech_ms"] == 30_000


def test_render_carries_every_turn_field() -> None:
    payload = s2s_launch.render(_cfg())
    for name in TurnConfig.model_fields:
        if name == "max_speech_ms":
            continue  # defaults to inf, covered separately above
        assert name in payload, f"turn-detection parameter {name} was not passed through"


def test_port_parsed_from_endpoint() -> None:
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1:9999/v1/realtime"))["port"] == 9999
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1/v1/realtime"))["port"] == 8765


def test_write_rejects_unknown_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key upstream would swallow stops the write.

    Runs against a fake checkout on purpose. The old version of this test pointed at
    a local clone, so on a machine without one it did not raise and passed for the
    wrong reason.
    """
    root = _fake_upstream(tmp_path / "upstream", s2s_launch.render(_cfg()))
    original = s2s_launch.render

    def with_a_typo(cfg: S2SConfig) -> dict[str, object]:
        payload = original(cfg)
        payload["smart_turn_treshold"] = payload.pop("smart_turn_threshold")
        return payload

    monkeypatch.setattr(s2s_launch, "render", with_a_typo)
    dest = tmp_path / "c.json"
    with pytest.raises(s2s_launch.S2SConfigError, match="上游不认识"):
        s2s_launch.write(_cfg(), dest, s2s_root=root)
    assert not dest.exists()


def test_not_requested_and_unavailable_are_distinguishable(tmp_path: Path) -> None:
    """Opting out of the check and failing the check must not look alike.

    Both leave unknown_keys empty, which is why the state has to be carried
    separately: see upstream_field_names' docstring on treating empty as
    "could not check".
    """
    skipped = s2s_launch.render_checked(_cfg(), None)
    failed = s2s_launch.render_checked(_cfg(), tmp_path / "not-a-checkout")

    assert skipped.unknown_keys == ()
    assert failed.unknown_keys == ()
    assert skipped.reconciliation is not failed.reconciliation


def test_render_checked_marks_reconciliation_not_requested() -> None:
    result = s2s_launch.render_checked(_cfg(), None)
    assert result.reconciliation is s2s_launch.Reconciliation.NOT_REQUESTED


def test_render_checked_marks_wrong_directory_unavailable(tmp_path: Path) -> None:
    result = s2s_launch.render_checked(_cfg(), tmp_path / "not-a-checkout")
    assert result.reconciliation is s2s_launch.Reconciliation.UNAVAILABLE


def test_render_checked_marks_real_checkout_checked(tmp_path: Path) -> None:
    root = _fake_upstream(tmp_path / "upstream", s2s_launch.render(_cfg()))
    result = s2s_launch.render_checked(_cfg(), root)
    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.unknown_keys == ()


# ------------------------------------------------- missing_turn_fields
#
# The four tests below pin both halves of the one expression at s2s_launch.py:137.
# The CLI turns its result into "提醒：这些判停参数没有透传", so a wrong one either
# cries wolf on every ordinary config or stays quiet when a knob really did not
# reach the engine. `result.missing_turn_fields == ()` on a clean config cannot
# tell those apart: it is also what a function that always returns () produces.


def _render_without(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `render` drop one key, standing in for a pass-through that got lost.

    Faked rather than configured, because every turn field is passed through today
    and the point is to describe what happens the day one stops being.

    Args:
        field: Payload key to remove.
        monkeypatch: Undoes the patch at the end of the test.
    """
    original = s2s_launch.render

    def render_minus_one(cfg: S2SConfig) -> dict[str, object]:
        payload = original(cfg)
        del payload[field]
        return payload

    monkeypatch.setattr(s2s_launch, "render", render_minus_one)


def test_missing_turn_fields_names_a_parameter_that_never_reached_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal path: upstream accepts the knob, we stopped sending it, so say so.

    Upstream then falls back to its own default while the streamer keeps tuning a
    number that never arrives. Not an error — the pipeline still runs — which is
    exactly why it has to be said out loud.
    """
    _render_without("speech_pad_ms", monkeypatch)
    root = _fake_upstream(tmp_path / "upstream", [*s2s_launch.render(_cfg()), "speech_pad_ms"])

    result = s2s_launch.render_checked(_cfg(), root)

    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.missing_turn_fields == ("speech_pad_ms",)


def test_a_parameter_upstream_does_not_accept_is_not_called_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: same dropped field, but upstream has never heard of it.

    Then there is nothing to pass through and naming it is a false alarm that sends
    someone hunting for a knob the engine does not have. The warning only carries
    information once it has been reconciled against upstream's own names, which is
    why the test is `f in known` and not just `f not in payload`.
    """
    _render_without("speech_pad_ms", monkeypatch)
    root = _fake_upstream(tmp_path / "upstream", s2s_launch.render(_cfg()))

    result = s2s_launch.render_checked(_cfg(), root)

    # Without this line the silence below is ambiguous: a failed check is quiet too.
    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.unknown_keys == ()
    assert result.missing_turn_fields == ()


def test_an_omission_we_chose_is_not_reported_as_missing(tmp_path: Path) -> None:
    """Boundary: max_speech_ms is left out on purpose, and upstream still accepts it.

    inf is not valid JSON and a missing value already means "no limit" upstream, so
    the key is dropped deliberately (s2s_launch.py:104-106). Reporting it would put a
    提醒 on every default config, and a warning that fires always is one nobody reads
    — which is the whole reason _intentionally_omitted is subtracted here.
    """
    root = _fake_upstream(tmp_path / "upstream", [*s2s_launch.render(_cfg()), "max_speech_ms"])

    result = s2s_launch.render_checked(_cfg(), root)

    assert "max_speech_ms" not in result.payload  # the omission really happened
    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.missing_turn_fields == ()


def test_a_finite_max_speech_that_goes_missing_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: the exemption is conditional on inf, not a blanket one.

    With a real limit configured the key stops being an intentional omission, and
    losing it means the engine cuts the streamer off at its own default instead of
    at the configured one. Staying quiet about that is the silence the 提醒 exists
    to break.
    """
    cfg = _cfg(turn=TurnConfig(max_speech_ms=30_000))
    _render_without("max_speech_ms", monkeypatch)
    root = _fake_upstream(tmp_path / "upstream", [*s2s_launch.render(cfg), "max_speech_ms"])

    result = s2s_launch.render_checked(cfg, root)

    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.missing_turn_fields == ("max_speech_ms",)


def test_write_refuses_when_reconciliation_was_asked_for_but_impossible(tmp_path: Path) -> None:
    """An unverified launch config must not reach disk.

    A live stream launches from this file and smoke_provider_b.sh:62 only checks
    that it exists, so writing it while claiming nothing is the bad outcome.
    """
    dest = tmp_path / "s2s.json"
    with pytest.raises(s2s_launch.S2SConfigError, match="对账没做成"):
        s2s_launch.write(_cfg(), dest, s2s_root=tmp_path / "not-a-checkout")
    assert not dest.exists()


def test_write_still_writes_when_reconciliation_was_not_requested(tmp_path: Path) -> None:
    """Opting out of the check stays legal. Only asking and failing is an error."""
    dest = tmp_path / "s2s.json"
    result = s2s_launch.write(_cfg(), dest, s2s_root=None)
    assert dest.exists()
    assert result.reconciliation is s2s_launch.Reconciliation.NOT_REQUESTED


def test_cli_render_s2s_fails_loudly_on_a_wrong_s2s_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pointing --s2s-root somewhere wrong used to print 已写入 and exit 0."""
    config = tmp_path / "bilisama.toml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    out = tmp_path / "s2s.json"

    code = cli.main(
        [
            "config",
            "render-s2s",
            "--config",
            str(config),
            "--out",
            str(out),
            "--s2s-root",
            str(tmp_path / "not-a-checkout"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "对账没做成" in captured.err
    assert "已写入" not in captured.out
    assert not out.exists()


def test_cli_render_s2s_says_whether_it_reconciled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two non-error states have to read differently in the terminal."""
    config = tmp_path / "bilisama.toml"
    config.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    argv = ["config", "render-s2s", "--config", str(config), "--out", str(tmp_path / "s2s.json")]

    assert cli.main(argv) == 0
    skipped = capsys.readouterr().out
    assert "跳过了跟上游字段名的对账" in skipped

    root = _fake_upstream(tmp_path / "upstream", s2s_launch.render(_cfg()))
    assert cli.main([*argv, "--s2s-root", str(root)]) == 0
    checked = capsys.readouterr().out
    assert "已跟上游字段名对账" in checked
    assert "跳过了" not in checked


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="no local speech-to-speech checkout")
def test_turn_fields_match_upstream() -> None:
    """Our turn-detection field names must match upstream's word for word.

    Goes red if upstream renames something, and equally if we misspell something.
    """
    known = s2s_launch.upstream_field_names(S2S_ROOT)
    assert known, "scanned no field names from upstream, so this check proves nothing"

    ours = set(TurnConfig.model_fields)
    unknown = ours - known
    assert not unknown, f"upstream does not know these and would swallow them: {sorted(unknown)}"

    # And the other direction: anything upstream added that we have not picked up.
    vad_file = S2S_ROOT / "src/speech_to_speech/arguments_classes/vad_arguments.py"
    upstream_vad = set(s2s_launch._FIELD_RE.findall(vad_file.read_text(encoding="utf-8")))
    # These two are overridden by module_arguments, so we deliberately skip them.
    upstream_vad -= {"enable_realtime_transcription", "realtime_processing_pause"}
    missing = upstream_vad - ours
    assert (
        not missing
    ), f"upstream added turn-detection parameters we have not adopted: {sorted(missing)}"


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="no local speech-to-speech checkout")
def test_render_checked_reports_clean() -> None:
    result = s2s_launch.render_checked(_cfg(), S2S_ROOT)
    # Without this line the two below are vacuous: an empty tuple is also what a
    # check that never ran returns.
    assert result.reconciliation is s2s_launch.Reconciliation.CHECKED
    assert result.unknown_keys == ()
    assert result.missing_turn_fields == ()
