"""What scripts/gate.sh actually runs, and what it is allowed to claim afterwards.

CONTRIBUTING points at one command before a commit. For a long stretch that command
ran the unit tier and nothing else: pyproject's `addopts` deselects `integration`,
gate.sh ran a bare `pytest`, and the fifteen tests that pin the speech-to-speech
shim's drift checks never executed. They were green in the sense that nobody ran
them. That is worse than an uncovered behaviour, because the gate reported success
and everyone read it as "the shim is checked".

The tier genuinely cannot be unconditional — it needs a separate ~385 MiB venv — so
the rule is not "always run it" but "never be quiet about not running it", the same
line tests/unit/test_dependency_direction.py takes when it skips with "checked 0
modules" rather than passing over an empty package.

Two things are pinned here:

- Reconciliation: every marker `addopts` deselects is either run by a gate step or
  written down below with a reason. Adding a fourth deselected marker forces that
  decision instead of quietly shrinking what the gate covers.
- Behaviour: the gate is executed end to end with a stub interpreter, so its control
  flow and its final line are observed rather than read. The stub is why this costs
  milliseconds — black, mypy and pytest are tested by being themselves elsewhere;
  what is under test here is which of them the gate decides to call, and what it
  says when it decides not to.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE = _REPO_ROOT / "scripts" / "gate.sh"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SMOKE = _REPO_ROOT / "scripts" / "smoke_provider_b.sh"
_INTEGRATION_TESTS = _REPO_ROOT / "tests" / "integration" / "test_s2s_patches.py"

# Markers the gate deliberately does not run, and why. A tier that neither appears
# in a gate step nor here is a tier everyone assumes is covered — which is exactly
# what happened to `integration`.
NOT_GATED: dict[str, str] = {
    "provider_a": (
        "needs a real hosted Realtime endpoint plus a key. There is nothing for it "
        "to talk to on a dev machine, and putting a paid third party in the path of "
        "every commit is not a gate, it is an outage waiting for a bad afternoon."
    ),
    "manual": (
        "one-off probes, kept so the next investigation starts from a working script "
        "rather than a blank file. They assert nothing a regression could break."
    ),
}

# Where speech-to-speech gets installed. Three files have to agree on this: the
# script that installs it, the gate that looks for it, and the tests that skip
# without it. If they drift, the gate says "没装" forever while the tests run fine.
_VENV_HOME = ".local/share/bilisama/engines/s2s"


def _ini_options(pyproject: str) -> dict[str, object]:
    options = tomllib.loads(pyproject)["tool"]["pytest"]["ini_options"]
    assert isinstance(options, dict)
    return options


def deselected_markers(pyproject: str) -> set[str]:
    """Markers a bare `pytest` skips, read out of addopts.

    Args:
        pyproject: Contents of pyproject.toml.

    Returns:
        Every name appearing as `not <name>` in the default marker expression.
    """
    addopts = _ini_options(pyproject).get("addopts", "")
    assert isinstance(addopts, str)
    return set(re.findall(r"\bnot\s+(\w+)", addopts))


def declared_markers(pyproject: str) -> set[str]:
    """Marker names registered in pyproject, without their descriptions.

    Args:
        pyproject: Contents of pyproject.toml.

    Returns:
        The bare names, e.g. `integration`.
    """
    markers = _ini_options(pyproject).get("markers", [])
    assert isinstance(markers, list)
    return {str(entry).split(":", 1)[0].strip() for entry in markers}


def markers_the_gate_runs(gate: str) -> set[str]:
    """Markers gate.sh selects explicitly with `pytest -m <marker>`.

    Args:
        gate: Contents of gate.sh.

    Returns:
        Names from every `-m` that follows the word `pytest` on a line. The `-m`
        in `python -m pytest` does not match — this wants the one *after* pytest,
        which is the one that picks tests.
    """
    return set(re.findall(r"pytest\b[^\n]*?\s-m\s+(\S+)", gate))


@dataclass(frozen=True)
class GateRun:
    """One end-to-end run of gate.sh against a stub interpreter."""

    returncode: int
    stdout: str
    stderr: str
    calls: tuple[str, ...]

    @property
    def last_line(self) -> str:
        """The line the operator actually reads before deciding they are done."""
        lines = [line for line in self.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else ""


def _run_gate(tmp_path: Path, *, s2s_installed: bool, require: str | None = None) -> GateRun:
    """Run the whole gate with `$PY` replaced by a recorder that always succeeds.

    Args:
        tmp_path: Scratch directory for the stub, its log and the fake venv.
        s2s_installed: Whether the s2s venv the gate looks for should exist.
        require: Value for BILISAMA_GATE_REQUIRE_INTEGRATION, the CI switch that
            turns a missing venv from a reported skip into a failure. None leaves
            the variable unset.

    Returns:
        Exit code, output, and every argument list the gate handed to `$PY`.
    """
    stub = tmp_path / "recording-python"
    log = tmp_path / "calls.log"
    stub.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GATE_STUB_LOG"\nexit 0\n', encoding="utf-8"
    )
    stub.chmod(0o755)

    venv = tmp_path / "s2s"
    if s2s_installed:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (venv / "bin" / "python").chmod(0o755)

    env = {
        **os.environ,
        "PY": str(stub),
        "GATE_STUB_LOG": str(log),
        "BILISAMA_S2S_VENV": str(venv),
    }
    env.pop("BILISAMA_GATE_REQUIRE_INTEGRATION", None)
    if require is not None:
        env["BILISAMA_GATE_REQUIRE_INTEGRATION"] = require

    proc = subprocess.run(
        ["bash", str(_GATE)],
        env=env,
        capture_output=True,
        text=True,
        # The profile step feeds python a heredoc; the stub never reads stdin, and
        # an inherited terminal would leave it waiting for one.
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return GateRun(proc.returncode, proc.stdout, proc.stderr, tuple(calls))


# ------------------------------------------------------------ Reconciliation


def _unaccounted(pyproject: str, gate: str) -> set[str]:
    """Deselected markers that neither the gate runs nor NOT_GATED explains."""
    return deselected_markers(pyproject) - markers_the_gate_runs(gate) - set(NOT_GATED)


def test_every_deselected_marker_is_gated_or_written_off() -> None:
    """No tier may quietly stop being covered.

    A marker in addopts is invisible to the gate's plain `pytest` step. Either the
    gate runs it on purpose, or somebody has said in NOT_GATED why it cannot be —
    and a new marker fails this until one of those is true.
    """
    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    assert deselected_markers(pyproject), "addopts deselects nothing — nothing left to check"

    unaccounted = _unaccounted(pyproject, _GATE.read_text(encoding="utf-8"))
    assert not unaccounted, (
        f"markers {sorted(unaccounted)} are deselected by default and never run by "
        "scripts/gate.sh. Give them a gate step, or a reason in NOT_GATED."
    )


def test_a_new_deselected_marker_forces_a_decision() -> None:
    """The planted violation, so the check above is known to bite.

    Deselecting a tier is a one-word edit in addopts, and it takes effect with no
    output anywhere. This is the thing that turns it into a conversation.
    """
    pyproject = _PYPROJECT.read_text(encoding="utf-8").replace(
        "not ui_browser'", "not ui_browser and not slow'"
    )
    assert _unaccounted(pyproject, _GATE.read_text(encoding="utf-8")) == {"slow"}


def test_the_integration_tier_is_one_of_the_gated_ones() -> None:
    """The specific tier this file exists for.

    Spelled out separately from the reconciliation above so that moving
    `integration` into NOT_GATED — which would satisfy that check — fails here
    instead. The venv is a download, not an impossibility.
    """
    assert "integration" in markers_the_gate_runs(_GATE.read_text(encoding="utf-8")), (
        "scripts/gate.sh no longer runs the integration tier; the s2s shim's drift "
        "checks would go back to being pinned by tests nobody runs"
    )


def test_the_marker_scan_reads_the_right_dash_m() -> None:
    """`python -m pytest` is not a marker selection, and `-m integration` is.

    Every line in the gate starts `$PY -m ...`. A scan that took that `-m` would
    report the unit step as covering a marker called `pytest` and count the tier as
    gated no matter what the gate does.
    """
    assert markers_the_gate_runs("$PY -m pytest -q --no-header\n") == set()
    assert markers_the_gate_runs("$PY -m pytest -m integration -q\n") == {"integration"}


def test_no_marker_is_both_gated_and_excused() -> None:
    """A stale excuse reads like an uncovered tier and hides a covered one."""
    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    both = markers_the_gate_runs(_GATE.read_text(encoding="utf-8")) & set(NOT_GATED)
    assert not both, f"{sorted(both)} are gated; drop their NOT_GATED entries"

    stale = set(NOT_GATED) - deselected_markers(pyproject)
    assert not stale, f"NOT_GATED explains {sorted(stale)}, which nothing deselects any more"


def test_deselected_markers_are_registered() -> None:
    """A typo in addopts deselects nothing and reports no error.

    pytest resolves an unknown name in a marker expression to false rather than
    complaining, so `not integraton` would silently run the tier — or, on the other
    side of the expression, silently drop one.
    """
    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    unregistered = deselected_markers(pyproject) - declared_markers(pyproject)
    assert not unregistered, f"addopts names markers that do not exist: {sorted(unregistered)}"


def test_everyone_looks_for_the_s2s_venv_in_the_same_place() -> None:
    """Install here, look there, and the gate skips forever without lying once.

    smoke_provider_b.sh puts the venv somewhere, gate.sh decides whether to run the
    tier by looking for it, and the tests skip on the same path. Two of the three
    agreeing is enough to make the gate's skip notice permanent and invisible.
    """
    for path in (_GATE, _SMOKE, _INTEGRATION_TESTS):
        text = path.read_text(encoding="utf-8")
        assert "BILISAMA_S2S_VENV" in text, f"{path.name} ignores the override"
        assert _VENV_HOME in text, f"{path.name} does not default to ~/{_VENV_HOME}"


# ------------------------------------------------------------ What the gate does


def test_the_gate_runs_the_integration_tier_when_the_venv_is_there(tmp_path: Path) -> None:
    """Normal path: both tiers run, and the banner says so."""
    run = _run_gate(tmp_path, s2s_installed=True)

    assert run.returncode == 0, run.stderr
    assert any(
        call.startswith("-m pytest -q") for call in run.calls
    ), f"the unit tier stopped running: {run.calls}"
    assert any(
        "-m pytest -m integration" in call for call in run.calls
    ), f"the venv was there and the integration tier still did not run: {run.calls}"
    assert "集成层" in run.last_line and "没跑" not in run.last_line, run.last_line


def test_the_gate_says_so_when_it_skips_the_integration_tier(tmp_path: Path) -> None:
    """Boundary: no venv, so the tier cannot run — and the gate never pretends it did.

    The last line is the assertion that matters. Everything above it scrolls past;
    that line is what somebody reads before they commit.
    """
    run = _run_gate(tmp_path, s2s_installed=False)

    assert run.returncode == 0, run.stderr
    assert not any(
        "-m integration" in call for call in run.calls
    ), f"ran the integration tier with no venv to run it against: {run.calls}"
    assert "跳过" in run.stdout, "the skip was not reported at all"
    assert "smoke_provider_b.sh install" in run.stdout, "no way to act on the skip"
    assert (
        "没跑" in run.last_line
    ), f"the closing line reads as a full pass over a tier that never ran: {run.last_line!r}"


def test_the_gate_can_be_told_that_skipping_is_not_allowed(tmp_path: Path) -> None:
    """Error path: CI sets the switch, and a missing venv becomes a failure.

    Reporting the skip is the right default for a laptop. It is the wrong one for
    the machine everybody trusts to have run everything.
    """
    run = _run_gate(tmp_path, s2s_installed=False, require="1")

    assert run.returncode != 0, "a missing venv passed the gate that was told to require it"
    assert "smoke_provider_b.sh install" in run.stderr, run.stderr
    assert "全部通过" not in run.stdout, "printed a success banner on the way out of a failure"


def test_requiring_the_integration_tier_can_be_switched_off_with_a_zero(
    tmp_path: Path,
) -> None:
    """`=0` means off, which is the only thing it can plausibly mean.

    A presence test would read 0 as "yes, require it" and fail a laptop run that
    was explicitly told not to bother. Someone would then delete the whole switch.
    """
    run = _run_gate(tmp_path, s2s_installed=False, require="0")

    assert run.returncode == 0, run.stderr
    assert "跳过" in run.stdout
