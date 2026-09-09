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

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from bilisama.bootstrap import s2s_launch
from bilisama.config import load
from tests.unit.test_s2s_launch import _fake_upstream

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE = _REPO_ROOT / "scripts" / "gate.sh"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SMOKE = _REPO_ROOT / "scripts" / "smoke_provider_b.sh"
_INTEGRATION_TESTS = _REPO_ROOT / "tests" / "integration" / "test_s2s_patches.py"
_TESTS = _REPO_ROOT / "tests"
# Every tree that holds first-party Python. `scripts` is here because it was the
# one that used to be outside all three of black, ruff and mypy.
_PYTHON_TREES = ("src", "tests", "tools", "scripts")

# Markers the gate deliberately does not run, and why. A tier that neither appears
# in a gate step nor here is a tier everyone assumes is covered — which is exactly
# what happened to `integration`.
NOT_GATED: dict[str, str] = {
    "provider_a": (
        "needs a real hosted Realtime endpoint plus a key. There is nothing for it "
        "to talk to on a dev machine, and putting a paid third party in the path of "
        "every commit is not a gate, it is an outage waiting for a bad afternoon."
    ),
}

# Markers that are excused above AND carry no tests at all. An excuse says "this
# tier cannot run here"; it quietly also reads as "this tier exists", and for a
# long time `manual` and `provider_a` were both empty — a row on the
# reconciliation table that balances forever because both sides are zero. Listing
# them separately is what makes the difference visible, and removing a name from
# here is the chore that lands the day somebody writes the first test.
EMPTY_TIERS: dict[str, str] = {
    # provider_a 于 2026-08-25 落地第一条（tests/integration/test_hosted_contract.py，
    # 台账 #68），所以它从这里摘掉了。空着的层一个都不剩——下一个注册 marker 却不写
    # 测试的人，会被上面那条检查当场拦住。
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


def _run_gate(
    tmp_path: Path,
    *,
    s2s_installed: bool,
    require: str | None = None,
    chromium_installed: bool = True,
    require_ui: str | None = None,
    eslint_installed: bool = True,
    eslint_exit: int = 0,
    require_js: str | None = None,
) -> GateRun:
    """Run the whole gate with `$PY` replaced by a recorder that always succeeds.

    Args:
        tmp_path: Scratch directory for the stub, its log and the fake venv.
        s2s_installed: Whether the s2s venv the gate looks for should exist.
        require: Value for BILISAMA_GATE_REQUIRE_INTEGRATION, the CI switch that
            turns a missing venv from a reported skip into a failure. None leaves
            the variable unset.
        chromium_installed: Whether the browser probe should succeed. The UI tier
            keys on the BROWSER, not the pip package, so the stub has to be able
            to fail that one call while succeeding at everything else.
        require_ui: Value for BILISAMA_GATE_REQUIRE_UI, the browser tier's
            equivalent CI switch.
        eslint_installed: Whether the eslint binary the gate looks for exists.
            Pointed at a stub rather than at the repo's own node_modules, so
            whether somebody ran `npm install` on this machine cannot decide
            what these runs prove.
        eslint_exit: What the eslint stub exits with, so the "it found problems"
            path can be observed without writing broken JavaScript into the tree.
        require_js: Value for BILISAMA_GATE_REQUIRE_JS, the JavaScript tier's
            equivalent CI switch.

    Returns:
        Exit code, output, and every argument list the gate handed to `$PY`.
    """
    stub = tmp_path / "recording-python"
    log = tmp_path / "calls.log"
    # Both the profile step and the browser probe run as a bare `$PY -` with a
    # heredoc, so the stub tells them apart by what the heredoc says.
    stub.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$GATE_STUB_LOG"\n'
        'if [ "$*" = "-" ]; then\n'
        "  body=$(cat)\n"
        '  case "$body" in\n'
        '    *playwright*) exit "${GATE_STUB_PROBE_EXIT:-0}" ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    venv = tmp_path / "s2s"
    if s2s_installed:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (venv / "bin" / "python").chmod(0o755)

    # Logged with a prefix of its own: eslint is not invoked through `$PY`, so
    # without one its argument list would be indistinguishable from a python call.
    eslint = tmp_path / "recording-eslint"
    if eslint_installed:
        eslint.write_text(
            f'#!/bin/sh\nprintf \'eslint %s\\n\' "$*" >> "$GATE_STUB_LOG"\nexit {eslint_exit}\n',
            encoding="utf-8",
        )
        eslint.chmod(0o755)

    env = {
        **os.environ,
        "PY": str(stub),
        "GATE_STUB_LOG": str(log),
        "BILISAMA_S2S_VENV": str(venv),
        "BILISAMA_ESLINT": str(eslint),
        "GATE_STUB_PROBE_EXIT": "0" if chromium_installed else "1",
    }
    # All three CI switches are scrubbed: whatever the host machine sets must not
    # decide what these runs prove.
    env.pop("BILISAMA_GATE_REQUIRE_INTEGRATION", None)
    env.pop("BILISAMA_GATE_REQUIRE_UI", None)
    env.pop("BILISAMA_GATE_REQUIRE_JS", None)
    if require is not None:
        env["BILISAMA_GATE_REQUIRE_INTEGRATION"] = require
    if require_ui is not None:
        env["BILISAMA_GATE_REQUIRE_UI"] = require_ui
    if require_js is not None:
        env["BILISAMA_GATE_REQUIRE_JS"] = require_js

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


@pytest.mark.parametrize("upstream_present", [False, True])
def test_gate_cli_really_renders_s2s_without_changing_default_provider(
    tmp_path: Path, upstream_present: bool
) -> None:
    """Run the real CLI block, not the successful recorder used for tier flow."""
    source = _GATE.read_text(encoding="utf-8")
    smoke = source.split('step "CLI 冒烟"', 1)[1].split('step "profile 覆盖层"', 1)[0]
    base = _REPO_ROOT / "config/bilisama.toml"
    before = base.read_bytes()
    cfg = load(base, strict=False, user_profiles_root=tmp_path / "profiles")
    upstream = tmp_path / "upstream"
    if upstream_present:
        _fake_upstream(upstream, s2s_launch.render(cfg.speech.s2s))
    completed = subprocess.run(
        ["bash", "-eu", "-c", smoke],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "PY": sys.executable,
            "WORK": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
            "BILISAMA_S2S_ROOT": str(upstream),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads((tmp_path / "s2s.json").read_text(encoding="utf-8"))
    assert rendered == s2s_launch.render(cfg.speech.s2s)
    assert base.read_bytes() == before
    if not upstream_present:
        assert "没有上游检出" in completed.stdout


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


def markers_tests_carry(tests_root: Path) -> set[str]:
    """Marker names any test file actually applies.

    Reads the source rather than collecting with pytest: collection would import
    every test module, which is a whole test run's worth of work to answer a
    question about text.

    Args:
        tests_root: The tests/ directory.

    Returns:
        Names from `@pytest.mark.<name>` and from `pytestmark = pytest.mark.<name>`.
    """
    found: set[str] = set()
    for path in tests_root.rglob("*.py"):
        found |= set(re.findall(r"pytest\.mark\.(\w+)", path.read_text(encoding="utf-8")))
    return found


def test_an_excused_marker_that_no_test_carries_is_named_as_such() -> None:
    """An excuse for an empty tier balances the table without covering anything.

    NOT_GATED answers "why does the gate not run this". It does not answer "is
    there anything to run", and reading the first as the second is how a marker
    with zero tests sat on the reconciliation table looking accounted for.
    """
    excused_and_empty = set(NOT_GATED) - markers_tests_carry(_TESTS)
    assert excused_and_empty == set(EMPTY_TIERS), (
        f"excused markers carrying no tests: {sorted(excused_and_empty)}; "
        f"EMPTY_TIERS says {sorted(EMPTY_TIERS)}. Write the first test for it and drop "
        "it from EMPTY_TIERS, or drop the marker and its excuse."
    )


def test_a_marker_nothing_carries_is_the_thing_that_check_looks_for() -> None:
    """The planted violation, so the check above is known to bite."""
    # provider_a 现在有测试了（#68），拿 ui_browser 之外任一不存在的名字当靶子。
    assert "no_such_tier" not in markers_tests_carry(_TESTS)
    assert "integration" in markers_tests_carry(_TESTS), (
        "the scan found no @pytest.mark.integration anywhere — it is reading the "
        "wrong thing, and every marker would look empty"
    )


# ------------------------------------------------------------ What the gate covers


def _dependency_names(pyproject: str) -> set[str]:
    """Distribution names from [project] dependencies, without their versions."""
    raw = tomllib.loads(pyproject)["project"]["dependencies"]
    assert isinstance(raw, list)
    return {re.split(r"[<>=!~\[ ]", str(entry), maxsplit=1)[0].strip() for entry in raw}


def _imported_top_level_modules() -> set[str]:
    """Every top-level module name imported anywhere first-party code lives.

    Vendored code counts: src/bilisama/ingest/bilibili/_vendor is what pulls in
    brotli and pure-protobuf, and it ships.
    """
    names: set[str] = set()
    for tree in _PYTHON_TREES:
        for path in (_REPO_ROOT / tree).rglob("*.py"):
            tree_ast = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree_ast):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                # level > 0 is a relative import: it names no distribution.
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names.add(node.module.split(".")[0])
    return names


def test_every_runtime_dependency_is_actually_imported() -> None:
    """A dependency nobody imports is download size the streamer pays for nothing.

    Ledger #51: numpy, pysbd and pydantic-settings were installed and never
    referenced — about 38 MiB measured in .venv, against a plan (section 6.5)
    that budgets the whole product at "tens to low hundreds of MB". They were
    added for work that took a different shape, and nothing noticed because
    nothing looks.

    Import name, not distribution name: `pure-protobuf` arrives as
    `pure_protobuf`. Optional-dependency groups are out of scope — mlx and the
    dev group are opt-in by definition.
    """
    imported = _imported_top_level_modules()
    unused = {
        name
        for name in _dependency_names(_PYPROJECT.read_text(encoding="utf-8"))
        if name.replace("-", "_") not in imported and name not in imported
    }
    assert not unused, (
        f"declared as runtime dependencies and imported nowhere: {sorted(unused)}. "
        "Use them or drop them — this is weight in every install."
    )


def test_the_lint_and_type_gates_cover_every_python_tree() -> None:
    """scripts/ used to be outside all three, so nothing read 389 lines of it.

    black and ruff take their paths from gate.sh; mypy takes its from
    [tool.mypy] files, which is what makes a bare `mypy` check the same tree the
    gate checks. Both spellings are asserted here because both were wrong in the
    same direction, under a step labelled 「mypy（全量，不只 src）」.
    """
    gate = _GATE.read_text(encoding="utf-8")
    for tool in ("black --check", "ruff check"):
        line = next((ln for ln in gate.splitlines() if tool in ln), "")
        assert line, f"gate.sh no longer runs {tool}"
        for tree in _PYTHON_TREES:
            assert re.search(rf"\b{tree}\b", line), f"{tool} does not cover {tree}/: {line!r}"

    files = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]["files"]
    assert set(_PYTHON_TREES) <= set(files), f"mypy files misses {set(_PYTHON_TREES) - set(files)}"


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


def test_the_gate_runs_the_browser_tier_when_chromium_is_there(tmp_path: Path) -> None:
    run = _run_gate(tmp_path, s2s_installed=True, chromium_installed=True)

    assert run.returncode == 0, run.stderr
    assert any(
        "-m pytest tests/ui -m ui_browser" in call for call in run.calls
    ), f"chromium was there and the browser tier still did not run: {run.calls}"
    assert "界面层" in run.last_line and "没跑" not in run.last_line, run.last_line


def test_the_gate_says_so_when_chromium_is_missing(tmp_path: Path) -> None:
    """The pip package alone is not the tier.

    With playwright installed but no browser, every test skips itself and pytest
    still exits 0 — so a gate that keyed on the import would run nothing and
    print a full pass. It has to key on the browser.
    """
    run = _run_gate(tmp_path, s2s_installed=True, chromium_installed=False)

    assert run.returncode == 0, run.stderr
    assert not any(
        "ui_browser" in call for call in run.calls
    ), f"ran the browser tier with no browser to run it in: {run.calls}"
    assert "跳过" in run.stdout, "the skip was not reported at all"
    assert "playwright install chromium" in run.stdout, "no way to act on the skip"
    assert (
        "没跑" in run.last_line
    ), f"the closing line reads as a full pass over a tier that never ran: {run.last_line!r}"


def test_the_gate_can_be_told_that_skipping_the_browser_tier_is_not_allowed(
    tmp_path: Path,
) -> None:
    run = _run_gate(tmp_path, s2s_installed=True, chromium_installed=False, require_ui="1")

    assert run.returncode != 0, "a missing browser passed the gate that was told to require it"
    assert "playwright install chromium" in run.stderr, run.stderr
    assert "全部通过" not in run.stdout, "printed a success banner on the way out of a failure"


def test_requiring_the_browser_tier_can_be_switched_off_with_a_zero(tmp_path: Path) -> None:
    run = _run_gate(tmp_path, s2s_installed=True, chromium_installed=False, require_ui="0")

    assert run.returncode == 0, run.stderr
    assert "跳过" in run.stdout


def test_the_closing_line_names_every_missing_tier(tmp_path: Path) -> None:
    """The far corner of the honesty matrix: none of the three optional tiers ran.

    Named one by one rather than as "some tiers": the line is read by somebody
    deciding whether they are done, and 「有的没跑」 tells them nothing.
    """
    run = _run_gate(tmp_path, s2s_installed=False, chromium_installed=False, eslint_installed=False)

    assert run.returncode == 0, run.stderr
    assert "单元层全部通过" in run.last_line, run.last_line
    for tier in ("集成层", "界面层", "JavaScript"):
        assert tier in run.last_line, f"{tier} missing from the closing line: {run.last_line!r}"
    assert "没跑" in run.last_line, run.last_line


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


# ------------------------------------------------------------ the JavaScript tier


def test_the_gate_lints_the_javascript_when_eslint_is_installed(tmp_path: Path) -> None:
    """Ledger #52: ~2700 lines of shipped JS with no gate of any kind.

    The Python beside it goes through black, ruff and mypy --strict. This is the
    step that stops the page and the Electron shell being the one tree where a
    typo'd identifier ships.
    """
    run = _run_gate(tmp_path, s2s_installed=True)

    assert run.returncode == 0, run.stderr
    assert any(
        call.startswith("eslint") for call in run.calls
    ), f"eslint was installed and the gate did not run it: {run.calls}"
    assert "JavaScript" in run.last_line and "没跑" not in run.last_line, run.last_line


def test_the_gate_says_so_when_eslint_is_missing(tmp_path: Path) -> None:
    """Boundary: no node_modules, so the tier cannot run — and the gate says so.

    Same contract as the s2s and browser tiers: `npm install` is a download, and
    a laptop without one still commits. What it must not do is read as a pass.
    """
    run = _run_gate(tmp_path, s2s_installed=True, eslint_installed=False)

    assert run.returncode == 0, run.stderr
    assert not any(call.startswith("eslint") for call in run.calls), run.calls
    assert "跳过" in run.stdout, "the skip was not reported at all"
    assert "npm install" in run.stdout, "no way to act on the skip"
    assert "没跑" in run.last_line, run.last_line


def test_the_gate_can_be_told_that_skipping_the_javascript_lint_is_not_allowed(
    tmp_path: Path,
) -> None:
    """Error path: the switch CI would set, and the failure it produces."""
    run = _run_gate(tmp_path, s2s_installed=True, eslint_installed=False, require_js="1")

    assert run.returncode != 0, "a missing eslint passed the gate that was told to require it"
    assert "npm install" in run.stderr, run.stderr
    assert "全部通过" not in run.stdout, "printed a success banner on the way out of a failure"


def test_requiring_the_javascript_tier_can_be_switched_off_with_a_zero(tmp_path: Path) -> None:
    run = _run_gate(tmp_path, s2s_installed=True, eslint_installed=False, require_js="0")

    assert run.returncode == 0, run.stderr
    assert "跳过" in run.stdout


def test_a_failing_eslint_fails_the_gate(tmp_path: Path) -> None:
    """The step has to be able to say no.

    `set -e` is what carries this, and it is one stray `|| true` away from a lint
    step that runs, prints, and passes regardless.
    """
    run = _run_gate(tmp_path, s2s_installed=True, eslint_exit=1)

    assert run.returncode != 0, "eslint reported errors and the gate still passed"
    assert "全部通过" not in run.stdout, run.stdout


# ------------------------------------------------------------ the eslint config itself

_ESLINT_BIN = _REPO_ROOT / "node_modules" / ".bin" / "eslint"


def _eslint(source: str, *, filename: str) -> subprocess.CompletedProcess[str]:
    """Lint one snippet through the repo's real config, without writing a file."""
    return subprocess.run(
        [str(_ESLINT_BIN), "--stdin", "--stdin-filename", filename],
        cwd=_REPO_ROOT,
        input=source,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(
    not _ESLINT_BIN.exists() or shutil.which("node") is None,
    reason="eslint 没装：先在仓库根跑 npm install",
)
def test_the_eslint_config_rejects_an_undeclared_name() -> None:
    """The rule that pays for the tier: a typo'd identifier is a runtime crash.

    Checked against the config the gate uses rather than a config written here,
    because the failure mode being guarded against is a config that matches
    nothing and reports a clean tree.
    """
    done = _eslint("export const go = () => documnet.body;", filename="src/bilisama/ui/web/js/x.js")

    assert done.returncode != 0, f"a misspelt `document` passed the lint: {done.stdout}"
    assert "no-undef" in done.stdout, done.stdout


@pytest.mark.skipif(
    not _ESLINT_BIN.exists() or shutil.which("node") is None,
    reason="eslint 没装：先在仓库根跑 npm install",
)
def test_the_eslint_config_knows_the_browser_and_the_worklet_apart() -> None:
    """Control: the same names that must fail above have to pass where they exist.

    A config that flagged `document` in a page module, or `sampleRate` in the
    capture worklet, would be turned off within a week.
    """
    page = _eslint("export const go = () => document.body;", filename="src/bilisama/ui/web/js/x.js")
    assert page.returncode == 0, page.stdout

    worklet = _eslint(
        "export const rate = () => sampleRate;",
        filename="src/bilisama/ui/web/js/capture-worklet.js",
    )
    assert worklet.returncode == 0, worklet.stdout
