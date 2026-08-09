#!/usr/bin/env bash
# 提交前跑一遍。每一步都要能证明「行为没变」。
#
# 为什么带 CLI 冒烟：拆 config 包那次，validate.py 少了一个运行时 import，
# 52 个单元测试全绿，因为**没有一个测试构造过 Settings**。是 CLI 冒烟抓到的。
# 覆盖缺口补上之前，这一层不能省。
set -euo pipefail

PY="${PY:-.venv/bin/python}"
cd "$(dirname "$0")/.."

# Scratch lives in one directory per run, removed on the way out. Fixed /tmp
# names collide when two people (or two agents) run the gate at the same time,
# and the loser sees a failure that has nothing to do with their change.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bilisama-gate.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\033[36m▸ %s\033[0m\n' "$*"; }

step "black"
$PY -m black --check src tests tools

step "ruff"
$PY -m ruff check src tests tools

step "mypy（全量，不只 src）"
# No file arguments on purpose: the set comes from [tool.mypy] files in
# pyproject.toml, so `mypy` on a developer machine checks exactly what the gate
# checks. Spelling it twice is how the two drift. Paired with mypy_path there,
# which is what stops a narrower run from poisoning .mypy_cache — see the
# comment on pyproject.toml:69-78.
$PY -m mypy

step "单元测试"
$PY -m pytest -q --no-header

step "CLI 冒烟"
$PY -m bilisama.cli config validate --config config/bilisama.toml >/dev/null
$PY -m bilisama.cli config show --config config/bilisama.toml >/dev/null
$PY -m bilisama.cli config chattiness >/dev/null
$PY -m bilisama.cli config render-s2s \
    --config config/bilisama.toml \
    --out "$WORK/s2s.json" \
    --s2s-root "${BILISAMA_S2S_ROOT:-../speech-to-speech}" >/dev/null

step "profile 覆盖层"
BILISAMA_GATE_WORK="$WORK" $PY - <<'EOF'
import os
import shutil
import sys
from pathlib import Path

from bilisama.config import load

tmp = Path(os.environ["BILISAMA_GATE_WORK"]) / "profiles-check"
(tmp / "profiles").mkdir(parents=True, exist_ok=True)
for f in Path("config/profiles").glob("*.toml"):
    shutil.copy(f, tmp / "profiles" / f.name)
base = Path("config/bilisama.toml").read_text(encoding="utf-8")

expected = {"debug": "debug", "normal": "info", "hype": "info"}
for name, level in expected.items():
    out = tmp / "bilisama.toml"
    out.write_text(base.replace('active_profile = "normal"', f'active_profile = "{name}"'), encoding="utf-8")
    got = load(out).runtime.log_level
    if got != level:
        sys.exit(f"profile {name}: log_level 应该是 {level}，实际 {got}")
EOF

printf '\033[32m全部通过\033[0m\n'
