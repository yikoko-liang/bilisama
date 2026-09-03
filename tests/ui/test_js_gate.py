"""The first gate the shipped JavaScript has ever had.

Ledger #52 (2026-08-25): a couple of thousand lines of JS — the page's modules
plus the Electron shell — had no eslint, no prettier, no tsconfig and no step in
scripts/gate.sh, while the Python beside it goes through black + ruff + mypy
--strict. The only cover was the browser tier, which skips whole on a machine
without chromium, and which never loads desktop/preview at all.

What this pins is deliberately narrow: every shipped file parses, and every
static import in it resolves to a file that exists. That is the class of
mistake nothing else here can catch — preload.cjs is loaded by no test and no
tool, and on a chromium-less machine neither is anything else. It is NOT a
linter: eslint has since landed as the gate's tenth step (eslint.config.mjs,
`npm install` at the repo root; ledger #52 closed), and this file stays because
no lint rule checks that a static import resolves to a file that exists.
prettier is still not wired.

Unmarked, so the gate's unit step runs it. Skips out loud without node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
_PARSER = _HERE / "shell" / "parse.mjs"

_NODE = shutil.which("node")
if _NODE is None:  # pragma: no cover - reported out loud, like the s2s tier
    pytest.skip(
        "node 没装，JavaScript 这层没法检查：装 node 20+ 或 `brew install node`",
        allow_module_level=True,
    )


def _shipped() -> list[Path]:
    """Every JS file that ships, and nothing that was downloaded."""
    page = sorted((_ROOT / "src" / "bilisama" / "ui" / "web" / "js").rglob("*.js"))
    shell = sorted(
        path
        for pattern in ("*.mjs", "*.cjs", "*.js")
        for path in (_ROOT / "desktop" / "preview").glob(pattern)
    )
    return page + shell


def _parse(files: list[Path]) -> list[dict[str, Any]]:
    assert _NODE is not None
    done = subprocess.run(
        [_NODE, "--experimental-vm-modules", str(_PARSER), *(str(f) for f in files)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise AssertionError(f"解析器跑不起来（{done.returncode}）：\n{done.stderr}")
    report: list[dict[str, Any]] = json.loads(done.stdout)
    return report


def test_the_inventory_is_not_empty() -> None:
    """A gate that checks nothing must say so rather than pass.

    Same trap tests/unit/test_dependency_direction.py guards against by
    reporting 「checked 0 modules」: if desktop/preview or the page's js/
    directory moves, every assertion below would be vacuously true.
    """
    files = _shipped()
    names = {path.name for path in files}
    assert len(files) >= 10, f"只找到 {len(files)} 个 JS 文件，目录八成搬家了：{sorted(names)}"
    for expected in ("main.js", "panel.js", "audio.js", "main.mjs", "preload.cjs"):
        assert expected in names, f"{expected} 不在检查范围里了"


def test_every_shipped_script_parses() -> None:
    """Syntax, on the files nothing else opens.

    preload.cjs is loaded by no test and no tool on this side; on a machine
    without chromium neither is the rest of the page. A typo used to become
    visible only when a streamer's pet came up blank.
    """
    broken = [one for one in _parse(_shipped()) if not one["ok"]]
    assert not broken, "这些 JS 文件语法就不对：\n" + "\n".join(
        f"  {one['file']}: {one['error']}" for one in broken
    )


def test_every_import_points_at_a_file_that_exists() -> None:
    """The module graph, resolved on disk.

    Browsers resolve imports at load time and a missing one is a runtime 404,
    so a renamed file leaves the page half-mounted rather than failing loudly.
    Bare specifiers are out of scope on purpose: nothing shipped here has any,
    and `electron` is supplied by the runtime rather than by the tree.
    """
    missing: list[str] = []
    for one in _parse(_shipped()):
        source = Path(one["file"])
        for dep in one["deps"]:
            if not dep.startswith("."):
                continue
            target = (source.parent / dep).resolve()
            if not target.exists():
                missing.append(f"  {source}: import {dep!r} 指向不存在的 {target}")
    assert not missing, "这些 import 落空了：\n" + "\n".join(missing)
