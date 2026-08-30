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

# scripts/ is on both lines because it used to be on neither, under a mypy step
# labelled "全量，不只 src" that did not include it either. mypy takes its own
# list from [tool.mypy] files, which now names scripts as well, so that a bare
# `mypy` still checks what the gate checks — tests/unit/test_gate.py holds all
# three spellings to the same set of trees.
step "black"
$PY -m black --check src tests tools scripts

step "ruff"
$PY -m ruff check src tests tools scripts

step "mypy（全量，不只 src）"
# No file arguments on purpose: the set comes from [tool.mypy] files in
# pyproject.toml, so `mypy` on a developer machine checks exactly what the gate
# checks. Spelling it twice is how the two drift. Paired with mypy_path there,
# which is what stops a narrower run from poisoning .mypy_cache — see the
# comment on pyproject.toml:69-78.
$PY -m mypy

step "mypy（假装 Windows）"
# Platform-gated branches are invisible to the run above: on this machine
# `if sys.platform == "win32"` is dead code, so a typo'd msvcrt call with the
# wrong argument type passed clean (measured 2026-08-24). Re-reading the tree
# as Windows is what checks the branches no CI here can execute. Cheap —
# a second parse, no test run — and it is the only cover the Windows path has
# until someone runs it on Windows.
$PY -m mypy --platform win32

step "单元测试"
# pyproject 的 addopts 把 integration / provider_a / manual 三个标记摘掉了，
# 所以这一步只跑单元层。integration 那层在下面单独跑。
$PY -m pytest -q --no-header

step "CLI 冒烟"
$PY -m bilisama.cli config validate --config config/bilisama.toml >/dev/null
$PY -m bilisama.cli config show --config config/bilisama.toml >/dev/null
$PY -m bilisama.cli config chattiness >/dev/null
# Reconcile field names against the upstream checkout when it exists. Without
# one the render still has to run — a missing sibling directory must not fail
# the whole gate on a machine that never installed s2s (D10).
S2S_UPSTREAM="${BILISAMA_S2S_ROOT:-../speech-to-speech}"
if [ -d "$S2S_UPSTREAM" ]; then
  $PY -m bilisama.cli config render-s2s \
      --config config/bilisama.toml \
      --out "$WORK/s2s.json" \
      --s2s-root "$S2S_UPSTREAM" >/dev/null
else
  printf '\033[33m▸ render-s2s：没有上游检出（%s），这次没对账字段名\033[0m\n' "$S2S_UPSTREAM"
  $PY -m bilisama.cli config render-s2s \
      --config config/bilisama.toml \
      --out "$WORK/s2s.json" >/dev/null
fi

step "profile 覆盖层"
BILISAMA_GATE_WORK="$WORK" XDG_DATA_HOME="$WORK/xdg-data" $PY - <<'EOF'
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

# The integration tier: the s2s shim's self-checks against a real speech-to-speech
# install. It needs a separate ~385 MiB venv, so it cannot run unconditionally —
# but it must not be able to go missing quietly either. `pytest` above deselects
# the marker, so for a long time this whole tier simply never ran on the way to a
# commit, and five of the shim's drift checks were pinned only by tests nobody
# executed. A skipped tier is now something the gate says out loud, and the last
# line below never claims a pass it did not earn. Same principle as
# tests/unit/test_dependency_direction.py reporting "checked 0 modules" instead of
# passing silently over an empty package.
#
# Keep this default in step with scripts/smoke_provider_b.sh (which installs it)
# and tests/integration/test_s2s_patches.py (which skips on it) — if they drift,
# the gate reports "没装" forever while the tests happily run.
S2S_VENV="${BILISAMA_S2S_VENV:-$HOME/.local/share/bilisama/engines/s2s}"
integration_ran=no
if [ -x "$S2S_VENV/bin/python" ]; then
  step "集成测试（s2s 补丁）"
  $PY -m pytest -m integration -q --no-header
  integration_ran=yes
# Compared against 0 rather than tested for emptiness, so that setting it to 0 to
# turn it off does what it looks like it does.
elif [ "${BILISAMA_GATE_REQUIRE_INTEGRATION:-0}" != 0 ]; then
  # CI sets this. There, 「没装所以跳过」 is not an acceptable answer.
  printf '\033[31m✗ 集成测试跑不了：%s 里没有 speech-to-speech\033[0m\n' "$S2S_VENV" >&2
  printf '  这台机器要求必须跑。先装：scripts/smoke_provider_b.sh install\n' >&2
  exit 1
else
  printf '\033[33m▸ 集成测试：跳过 —— %s 里没装 speech-to-speech（约 385 MiB）\033[0m\n' "$S2S_VENV"
  printf '  这一层管的是 s2s 补丁的自检，本机这次没验过。\n'
  printf '  要跑：scripts/smoke_provider_b.sh install\n'
fi

# The browser tier: real chromium driving the real pet page (tests/ui). Same
# contract as the s2s tier above — needs a one-time browser download, so it
# cannot run unconditionally, and a skip is said out loud instead of passing
# silently. The manual remainder lives in CONTRIBUTING「界面改动的人工验收」.
ui_ran=no
# Probe the BROWSER, not just the pip package: with playwright installed but
# chromium missing, every test skips itself, pytest exits 0, and keying on the
# import alone would let the final line claim a tier that ran nothing — the
# quiet pass this tier exists to prevent. Same shape as the s2s check above,
# which keys on the installed artifact.
if $PY - <<'PROBE' 2>/dev/null
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as play:
        # Raises (or points at nothing) when `playwright install` never ran.
        found = Path(play.chromium.executable_path).exists()
except Exception:
    found = False
raise SystemExit(0 if found else 1)
PROBE
then
  step "界面测试（浏览器驱动）"
  $PY -m pytest tests/ui -m ui_browser -q --no-header
  ui_ran=yes
elif [ "${BILISAMA_GATE_REQUIRE_UI:-0}" != 0 ]; then
  printf '\033[31m✗ 界面测试跑不了：playwright 或 chromium 没装\033[0m\n' >&2
  printf '  这台机器要求必须跑。先装：uv pip install playwright && %s -m playwright install chromium\n' "$PY" >&2
  exit 1
else
  printf '\033[33m▸ 界面测试：跳过 —— playwright 或 chromium 没装\033[0m\n'
  printf '  这一层开真浏览器验桌宠页面（气泡、面板、降级、暗色），本机这次没验过。\n'
  printf '  要跑：uv pip install playwright && %s -m playwright install chromium\n' "$PY"
fi

# The JavaScript tier: eslint over the page modules, the Electron shell and the
# shell's test harness (ledger #52). Same contract as the two tiers above — it
# needs `npm install` at the repo root, so it cannot run unconditionally, and a
# skip is said out loud instead of passing quietly. Until this step existed, the
# only cover the ~2700 shipped lines of JS had was the browser tier, which skips
# whole on a machine without chromium and never loads desktop/preview at all.
#
# Keyed on the installed binary rather than on `npx`, for the same reason the s2s
# tier keys on the venv: npx exists on every machine with node and would fetch
# eslint from the network mid-gate.
ESLINT="${BILISAMA_ESLINT:-node_modules/.bin/eslint}"
js_ran=no
if [ -x "$ESLINT" ]; then
  step "JavaScript 检查（eslint）"
  "$ESLINT" .
  js_ran=yes
elif [ "${BILISAMA_GATE_REQUIRE_JS:-0}" != 0 ]; then
  printf '\033[31m✗ JavaScript 检查跑不了：%s 不存在\033[0m\n' "$ESLINT" >&2
  printf '  这台机器要求必须跑。先装：npm install\n' >&2
  exit 1
else
  printf '\033[33m▸ JavaScript 检查：跳过 —— %s 不存在\033[0m\n' "$ESLINT"
  printf '  这一层查页面、Electron 壳和壳的测试桩里的未定义名、没用到的变量，本机这次没验过。\n'
  printf '  要跑：npm install\n'
fi

# One line naming every tier that did not run, instead of one branch per
# combination: three optional tiers is eight branches, and the fourth would be
# sixteen. What the line must never do is read as a full pass over a tier that
# never ran.
skipped=""
[ "$integration_ran" = yes ] || skipped="${skipped}集成层、"
[ "$ui_ran" = yes ] || skipped="${skipped}界面层、"
[ "$js_ran" = yes ] || skipped="${skipped}JavaScript 层、"
if [ -z "$skipped" ]; then
  printf '\033[32m全部通过（含集成层、界面层与 JavaScript 层）\033[0m\n'
else
  printf '\033[32m单元层全部通过\033[0m\033[33m，%s没跑（见上）\033[0m\n' "${skipped%、}"
fi
