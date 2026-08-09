"""Check-then-commit, enforced by reading the shim instead of trusting its comments.

tools/s2s_shim/bilisama_s2s_shim/patches.py promises atomicity: a patch verifies
every symbol, field and signature it depends on, and only then writes to upstream.
That promise is what makes zero-patch mode a real fallback — a patch that raises
after swapping one attribute leaves a process that is neither patched nor pristine,
and there is nothing to fall back to.

The promise used to live in a comment. Moving one write above the last check passed
the entire suite, because the one test that asserted atomicity injected a drift that
tripped a check near the top of the function either way. So the last check added —
the signature check — could fire with GenerateResponseRequest already replaced.

tests/integration/test_s2s_patches.py now replays every drift it knows about and
fails if a rejected patch touched anything. This file covers what that cannot: a
check added tomorrow, below the write, with no drift row of its own. It is also the
half that runs without the 385 MiB speech-to-speech venv, which is the difference
between a rule that is enforced and a rule that is enforced on some machines.

Like tests/unit/test_dependency_direction.py, the checker is a plain function over
source text, run both over the real file and over planted violations — a gate whose
teeth are never exercised is a gate nobody can trust.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_PATCHES_PY = (
    Path(__file__).resolve().parents[2] / "tools" / "s2s_shim" / "bilisama_s2s_shim" / "patches.py"
)

# The single funnel every upstream write has to go through, and the check helper
# that has to run before it.
_COMMIT = "_commit"
_REQUIRE = "_require"


def _source() -> str:
    return _PATCHES_PY.read_text(encoding="utf-8")


def _own_nodes(fn: ast.FunctionDef) -> Iterator[ast.stmt | ast.expr]:
    """Every node in a function body except the bodies of the functions it defines.

    A patch builds its replacements as nested defs. What those do when upstream
    calls them later is not a patch-time write: `state.current_response_params =
    ...` inside patched_handler runs on a live turn, long after the patch returned.

    Args:
        fn: The function to walk.

    Yields:
        Statements and expressions belonging to `fn` itself, in no particular
        order. Contexts and operators are skipped: they carry no line number, and
        every rule here is about where in the function something happens.
    """
    stack: list[ast.stmt | ast.expr] = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt | ast.expr):
                stack.append(child)


def _called_name(node: ast.AST) -> str | None:
    """Name of a plain `foo(...)` call, or None for anything else."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def registered_patches(source: str) -> dict[str, int]:
    """The functions the `_PATCHES` registry dispatches to.

    Reading the registry rather than matching on a `patch_` prefix is deliberate:
    a new patch is reachable only by being registered, so this cannot be sidestepped
    by naming the function something else.

    Args:
        source: Contents of patches.py.

    Returns:
        Function name -> line the registry names it on.
    """
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        # Annotated or not: the real registry carries a dict[str, Callable]
        # annotation, and dropping it must not turn the checker off.
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_PATCHES" for t in targets):
            continue
        if isinstance(node.value, ast.Dict):
            for value in node.value.values:
                if isinstance(value, ast.Name):
                    found[value.id] = value.lineno
    return found


def atomicity_violations(source: str) -> list[str]:
    """Every way a registered patch breaks check-then-commit.

    Args:
        source: Contents of patches.py.

    Returns:
        One line per violation, in source order. Empty means every registered patch
        runs all of its checks and only then writes, through one `_commit` call.
    """
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    problems: list[tuple[int, str]] = []

    for name, lineno in registered_patches(source).items():
        fn = functions.get(name)
        if fn is None:
            problems.append((lineno, f"line {lineno}: {name} is registered but not defined here"))
            continue

        nodes = list(_own_nodes(fn))
        commits = sorted(n.lineno for n in nodes if _called_name(n) == _COMMIT)
        if len(commits) != 1:
            problems.append(
                (
                    fn.lineno,
                    f"line {fn.lineno}: {name} has {len(commits)} {_COMMIT}() calls, want 1",
                )
            )
        commit_line = commits[0] if commits else 10**9

        for node in nodes:
            if node.lineno <= commit_line:
                continue
            if _called_name(node) == _REQUIRE or isinstance(node, ast.Raise):
                problems.append(
                    (node.lineno, f"line {node.lineno}: {name} checks after it has written")
                )

        for node in nodes:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            if (
                any(isinstance(t, ast.Attribute) for t in targets)
                or _called_name(node) == "setattr"
            ):
                problems.append(
                    (
                        node.lineno,
                        f"line {node.lineno}: {name} writes to upstream outside {_COMMIT}()",
                    )
                )

        tail = fn.body[-2:]
        commit_is_last = (
            len(tail) == 2
            and isinstance(tail[0], ast.Expr)
            and _called_name(tail[0].value) == _COMMIT
            and isinstance(tail[1], ast.Return)
        )
        if commits and not commit_is_last:
            problems.append(
                (
                    commit_line,
                    f"line {commit_line}: {name} does something after {_COMMIT}() "
                    "other than return",
                )
            )

    return [message for _, message in sorted(problems)]


def _planted(body: str) -> str:
    """A minimal patches.py whose single registered patch has the given body."""
    return f"def patch_thing():\n{body}\n\n\n_PATCHES = {{'thing': patch_thing}}\n"


_CLEAN = _planted(
    "    import upstream\n"
    "    _require(hasattr(upstream, 'thing'), 'gone')\n"
    "    _commit(((upstream, 'thing', None),))\n"
    "    return PatchResult('thing', True)\n"
)


def test_the_checker_has_patches_to_check() -> None:
    """Says out loud how many patches were read.

    A registry the checker cannot parse would make every assertion below vacuous,
    and a green run over zero patches reads exactly like a green run over two.
    """
    names = registered_patches(_source())
    assert names, "no registered patches found — the checker read nothing"
    assert set(names) == {"patch_text_modality", "patch_raw_instructions"}, (
        f"the registry changed shape: {sorted(names)}. A new patch has to obey "
        "check-then-commit too — add it here on purpose, not by accident."
    )


def test_the_shim_checks_everything_before_it_writes() -> None:
    """The real file, against the rule its own comments claim."""
    assert atomicity_violations(_source()) == []


def test_a_clean_patch_passes() -> None:
    """The checker is not simply always red."""
    assert atomicity_violations(_CLEAN) == []


def test_a_write_above_the_last_check_is_caught() -> None:
    """The exact edit that used to survive the whole suite.

    A stray assignment before the final check leaves upstream half-patched on the
    drift that check exists to catch.
    """
    planted = _planted(
        "    import upstream\n"
        "    upstream.thing = None\n"
        "    _require(hasattr(upstream, 'other'), 'gone')\n"
        "    _commit(((upstream, 'other', None),))\n"
        "    return PatchResult('thing', True)\n"
    )
    assert atomicity_violations(planted) == [
        "line 3: patch_thing writes to upstream outside _commit()"
    ]


def test_a_check_after_the_write_is_caught() -> None:
    """The same defect from the other side: the check moves instead of the write."""
    planted = _planted(
        "    import upstream\n"
        "    _commit(((upstream, 'thing', None),))\n"
        "    _require(hasattr(upstream, 'other'), 'gone')\n"
        "    return PatchResult('thing', True)\n"
    )
    problems = atomicity_violations(planted)
    assert "patch_thing checks after it has written" in " ".join(problems)
    assert "other than return" in " ".join(problems)


def test_a_raise_after_the_write_is_caught() -> None:
    """`raise PatchError(...)` spelled out is still a check, and still too late."""
    planted = _planted(
        "    import upstream\n"
        "    _commit(((upstream, 'thing', None),))\n"
        "    if not hasattr(upstream, 'other'):\n"
        "        raise PatchError('gone')\n"
        "    return PatchResult('thing', True)\n"
    )
    assert any("checks after it has written" in p for p in atomicity_violations(planted))


def test_a_bare_setattr_is_caught() -> None:
    """setattr() is the other spelling of an attribute write, and it is not exempt."""
    planted = _planted(
        "    import upstream\n"
        "    setattr(upstream, 'thing', None)\n"
        "    _commit(())\n"
        "    return PatchResult('thing', True)\n"
    )
    assert any("outside _commit()" in p for p in atomicity_violations(planted))


@pytest.mark.parametrize("count", [0, 2])
def test_a_patch_must_commit_exactly_once(count: int) -> None:
    """Two funnels are no funnel, and none means the writes went somewhere else."""
    calls = "    _commit(((upstream, 'thing', None),))\n" * count
    planted = _planted("    import upstream\n" + calls + "    return PatchResult('thing', True)\n")
    assert any(f"has {count} _commit() calls" in p for p in atomicity_violations(planted))


def test_a_registered_name_that_is_not_a_function_is_caught() -> None:
    """A registry entry pointing at something else must not silently skip the rule."""
    planted = "patch_thing = None\n\n\n_PATCHES = {'thing': patch_thing}\n"
    assert atomicity_violations(planted) == [
        "line 4: patch_thing is registered but not defined here"
    ]


def test_writes_inside_a_replacement_are_not_patch_time_writes() -> None:
    """A nested def's body runs on a live turn, not while the patch is applying.

    patched_handler assigns state.current_response_params on every VAD turn. Reading
    that as a patch-time write would make the rule unsatisfiable and get it deleted.
    """
    planted = _planted(
        "    import upstream\n"
        "    def replacement(state):\n"
        "        state.current_response_params = None\n"
        "        return None\n"
        "    _require(hasattr(upstream, 'thing'), 'gone')\n"
        "    _commit(((upstream, 'thing', replacement),))\n"
        "    return PatchResult('thing', True)\n"
    )
    assert atomicity_violations(planted) == []
