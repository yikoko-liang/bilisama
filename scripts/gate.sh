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
if $PY -c "import playwright" 2>/dev/null; then
  step "界面测试（浏览器驱动）"
  $PY -m pytest tests/ui -m ui_browser -q --no-header
  ui_ran=yes
elif [ "${BILISAMA_GATE_REQUIRE_UI:-0}" != 0 ]; then
  printf '\033[31m✗ 界面测试跑不了：playwright 没装\033[0m\n' >&2
  printf '  这台机器要求必须跑。先装：uv pip install playwright && %s -m playwright install chromium\n' "$PY" >&2
  exit 1
else
  printf '\033[33m▸ 界面测试：跳过 —— playwright 没装\033[0m\n'
  printf '  这一层开真浏览器验桌宠页面（气泡、面板、降级、暗色），本机这次没验过。\n'
  printf '  要跑：uv pip install playwright && %s -m playwright install chromium\n' "$PY"
fi

if [ "$integration_ran" = yes ] && [ "$ui_ran" = yes ]; then
  printf '\033[32m全部通过（含集成层与界面层）\033[0m\n'
elif [ "$integration_ran" = yes ]; then
  printf '\033[32m通过\033[0m\033[33m，界面层没跑（见上）\033[0m\n'
elif [ "$ui_ran" = yes ]; then
  printf '\033[32m通过\033[0m\033[33m，集成层没跑（见上）\033[0m\n'
else
  printf '\033[32m单元层全部通过\033[0m\033[33m，集成层与界面层没跑（见上）\033[0m\n'
fi
