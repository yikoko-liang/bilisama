"""Dependency direction: L3 must not be able to see the speech protocol.

Plan §3.7 seals the eight provider quirks from §3.3 inside the adapter so that L3
"only sees SpeechLink plus normalised LinkEvent". §10.6 turns that promise into a
check, because it is the kind of promise people keep right up until the afternoon
they are debugging a stuck turn: director/, persona/, memory/ and tools/ may not
import realtime.providers.*, and may not carry the protocol's own string literals.

The four packages hold nothing but an empty __init__.py today. That is why the
per-module checks are parametrised per file and why test_gate_has_anything_to_check
skips out loud with the count: `pytest -v` then reads as "looked at four empty
package markers, checked 0 modules", not as "L3 is clean". The planted-violation
test carries the weight in the meantime by running the real checker over a module
that breaks the rule in every shape, so the gate is known to bite before it has
anything to bite.

§10.6 says grep. This uses ast instead, deliberately: grep reddens on the docstring
and the comment that document the rule, and it misses `from ..realtime import
providers` and `("response" ".create")` entirely. Both shapes are what an ordinary
refactor produces, not evasion.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "bilisama"

# §10.6 names the first four. They are src/bilisama/tools, not the repo-root
# tools/ — that one holds the speech-to-speech shim, whose whole job is to speak
# protocol. ui joined with the desktop-pet preview (§15.12): it talks its own
# vocabulary (ui/events.py) and the normalised link events, never the wire.
GUARDED = ("director", "persona", "memory", "tools", "ui")

# The adapter layer. §3.7: provider differences live in Capabilities and in one
# providers/<name>.py, nowhere else.
FORBIDDEN_IMPORT = "bilisama.realtime.providers"

# §10.6 verbatim. The trailing dot in "response." is what makes it match the wire
# events — response.create, response.done — instead of the English word.
FORBIDDEN_LITERALS = ("response.", "conversation.item", "input_audio_buffer")

MODULES = sorted(path for package in GUARDED for path in (_SRC / package).rglob("*.py"))


def _package_of(path: Path) -> str:
    """Dotted package a file lives in, for resolving its relative imports."""
    parts = path.relative_to(_REPO_ROOT / "src").with_suffix("").parts
    return ".".join(parts[:-1])


def _imported_names(tree: ast.Module, package: str) -> Iterator[tuple[int, str]]:
    """Every module a file pulls in, with relative imports resolved to absolute.

    Args:
        tree: Parsed module.
        package: Dotted package the module lives in, for the relative forms.

    Yields:
        (line number, dotted module name). `from x import y` yields both x and
        x.y, so `from bilisama.realtime import providers` is caught as well.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from .` is this package; each extra dot climbs one level.
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                if node.module:
                    base = f"{base}.{node.module}"
            else:
                base = node.module or ""
            yield node.lineno, base
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}"


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Constant nodes that are docstrings.

    Documenting the rule is not breaking it. A docstring never reaches the wire,
    and a gate that fires on the comment explaining it is a gate someone deletes.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _is_substantive(source: str) -> bool:
    """Does the file hold anything the rule could apply to?

    A package marker with no statements — or only a docstring — cannot break the
    rule. Counting those as checked modules is how a gate ends up reporting green
    over an empty room.
    """
    body = ast.parse(source).body
    if not body:
        return False
    if len(body) == 1:
        only = body[0]
        return not (
            isinstance(only, ast.Expr)
            and isinstance(only.value, ast.Constant)
            and isinstance(only.value.value, str)
        )
    return True


SUBSTANTIVE = frozenset(
    path for path in MODULES if _is_substantive(path.read_text(encoding="utf-8"))
)


def _module_id(path: Path) -> str:
    """Parametrize id. Marks the markers, so `pytest -v` cannot read as clean."""
    name = str(path.relative_to(_SRC))
    return name if path in SUBSTANTIVE else f"{name}:empty"


def provider_imports(source: str, package: str) -> list[str]:
    """Find adapter imports.

    Args:
        source: Module source.
        package: Dotted package the module lives in.

    Returns:
        One line per offending import statement, in source order.
    """
    hits: dict[int, str] = {}
    for lineno, name in _imported_names(ast.parse(source), package):
        if name == FORBIDDEN_IMPORT or name.startswith(f"{FORBIDDEN_IMPORT}."):
            hits.setdefault(lineno, name)
    return [f"line {lineno}: imports {name}" for lineno, name in sorted(hits.items())]


def protocol_literals(source: str) -> list[str]:
    """Find protocol string literals, docstrings excepted.

    Args:
        source: Module source.

    Returns:
        One line per offending literal, in source order.
    """
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    # ast.walk is breadth-first, so sort by line number to report in source order.
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        needle = next((n for n in FORBIDDEN_LITERALS if n in node.value), None)
        if needle:
            found.append((node.lineno, f"line {node.lineno}: string literal contains {needle!r}"))
    return [message for _, message in sorted(found)]


def test_guarded_packages_exist() -> None:
    """A renamed package would disable its half of the gate without a word."""
    missing = [name for name in GUARDED if not (_SRC / name).is_dir()]
    assert not missing, f"§10.6 guards packages that are not there: {missing}"


def test_gate_has_anything_to_check() -> None:
    """Says out loud how much L3 code the gate actually read.

    Stage 0 has none, so this skips with "checked 0 modules". A green run with
    this test skipped means the rule has not been exercised on real code yet —
    which is the truth, and worth reading in the gate output rather than
    inferring from four PASSED lines over empty package markers.
    """
    if not SUBSTANTIVE:
        pytest.skip(
            f"§10.6: checked 0 modules — {len(MODULES)} empty package markers "
            f"under {'/, '.join(GUARDED)}/, no L3 code yet"
        )
    unread = [path for path in SUBSTANTIVE if path not in set(MODULES)]
    assert not unread, f"modules found but not parametrised: {unread}"


@pytest.mark.parametrize("path", MODULES, ids=_module_id)
def test_no_provider_import(path: Path) -> None:
    """L3 talks to SpeechLink. Reaching past it into an adapter fails here."""
    hits = provider_imports(path.read_text(encoding="utf-8"), _package_of(path))
    assert not hits, f"§3.7: {path.relative_to(_SRC)} must not import {FORBIDDEN_IMPORT}.*: {hits}"


@pytest.mark.parametrize("path", MODULES, ids=_module_id)
def test_no_protocol_literal(path: Path) -> None:
    """A wire event name in L3 means a §3.3 quirk leaked out of the adapter."""
    hits = protocol_literals(path.read_text(encoding="utf-8"))
    assert not hits, f"§3.7: {path.relative_to(_SRC)} must not speak protocol: {hits}"


_PLANTED = '''\
"""A module that names response.create in prose, which is allowed."""

import bilisama.realtime.providers.s2s
from bilisama.realtime import providers
from bilisama.realtime.providers import s2s
from ..realtime.providers import dashscope

# A comment naming input_audio_buffer.append is allowed too.
EVENT = "response.create"
ITEM = {"type": "conversation.item.create"}
AUDIO = f"input_audio_buffer.{'append'}"
'''


def test_checker_flags_a_planted_violation() -> None:
    """The guarded packages are empty, so prove the checker still has teeth.

    Covers every shape the rule can be broken in: plain import, package import,
    submodule import, relative import, bare literal, literal in a dict, f-string.
    """
    assert provider_imports(_PLANTED, "bilisama.director") == [
        "line 3: imports bilisama.realtime.providers.s2s",
        "line 4: imports bilisama.realtime.providers",
        "line 5: imports bilisama.realtime.providers",
        "line 6: imports bilisama.realtime.providers",
    ]
    assert protocol_literals(_PLANTED) == [
        "line 9: string literal contains 'response.'",
        "line 10: string literal contains 'conversation.item'",
        "line 11: string literal contains 'input_audio_buffer'",
    ]


def test_checker_leaves_the_normalised_vocabulary_alone() -> None:
    """A gate that fires on legal code is a gate someone switches off.

    SpeechLink, LinkEvent and the sibling packages are exactly what L3 is meant to
    import, and prose about the protocol is not protocol.
    """
    clean = '''\
"""Docstrings may explain why L3 never sends response.create."""

from bilisama.realtime.link import SpeechLink
from ..ingest.events import LiveEvent

# input_audio_buffer.append is the adapter's business, not ours.
STATE = "reply.started"
'''
    assert provider_imports(clean, "bilisama.director") == []
    assert protocol_literals(clean) == []


def test_empty_package_marker_is_not_counted_as_a_checked_module() -> None:
    """Emptiness has to be detectable, or the count above is decoration."""
    assert not _is_substantive("")
    assert not _is_substantive('"""Only a docstring."""\n')
    assert _is_substantive("X = 1\n")
    assert _is_substantive('"""Docstring."""\n\nX = 1\n')


def test_unparsable_module_is_an_error_not_a_pass() -> None:
    """A file the gate cannot read must not be reported as compliant."""
    with pytest.raises(SyntaxError):
        provider_imports("def broken(:\n", "bilisama.director")
    with pytest.raises(SyntaxError):
        protocol_literals("def broken(:\n")
