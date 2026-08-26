#!/usr/bin/env bash
# Launch the verified DashScope director path from Finder or a terminal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
BILISAMA="$REPO_ROOT/.venv/bin/bilisama"
PET_DIR="$REPO_ROOT/desktop/preview"
ELECTRON="$PET_DIR/node_modules/.bin/electron"
ELECTRON_RUNTIME="$PET_DIR/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
ENDPOINT_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/bilisama/ui/endpoint.json"
MODEL="${BILISAMA_REALTIME_MODEL:-qwen-audio-3.0-realtime-flash}"
ROOM_ID="${BILISAMA_ROOM_ID:-}"

die() {
  printf '\033[31m启动失败：%s\033[0m\n' "$*" >&2
  exit 1
}

running_backend_pid() {
  local pid command
  [ -f "$ENDPOINT_FILE" ] || return 1
  pid="$($PYTHON - "$ENDPOINT_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(int(payload["pid"]))
except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
  )" || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"bilisama dev-talk"* ]] || return 1
  printf '%s\n' "$pid"
}

start_pet_for_running_backend() {
  if pgrep -f "$ELECTRON_RUNTIME \." >/dev/null 2>&1; then
    printf 'BiliSama 已经在运行，桌面 UI 也已启动。\n'
    return 0
  fi
  [ -x "$ELECTRON_RUNTIME" ] || die "桌面 UI 没装好。先在 $PET_DIR 运行 npm install。"
  (
    cd "$PET_DIR"
    "$ELECTRON" . >/dev/null 2>&1 &
  )
  printf '后端已经在运行，已重新拉起桌面 UI。\n'
}

find_builtin_input() {
  "$PYTHON" - <<'PY'
import sounddevice

needles = (
    "macbook pro麦克风",
    "macbook pro microphone",
    "built-in microphone",
    "内建麦克风",
    "内置麦克风",
)
for index, device in enumerate(sounddevice.query_devices()):
    name = str(device["name"]).casefold()
    if int(device["max_input_channels"]) > 0 and any(needle in name for needle in needles):
        print(index)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

cd "$REPO_ROOT"

[ -x "$PYTHON" ] && [ -x "$BILISAMA" ] || die "Python 环境没装好。先在仓库运行 uv sync。"
[ -f "$REPO_ROOT/path.sh" ] || die "仓库根目录缺少 path.sh。"
[ -x "$ELECTRON_RUNTIME" ] || die "桌面 UI 没装好。先在 $PET_DIR 运行 npm install。"

# shellcheck disable=SC1091
source "$REPO_ROOT/path.sh"
[ -n "${ali_api_key:-}" ] || die "path.sh 里缺少 ali_api_key。"
[ -n "${dashscope_url:-}" ] || die "path.sh 里缺少 dashscope_url。"

INPUT_DEVICE="${BILISAMA_INPUT_DEVICE:-}"
if [ -z "$INPUT_DEVICE" ]; then
  INPUT_DEVICE="$(find_builtin_input)" || die \
    "找不到 MacBook 内置麦克风。运行 .venv/bin/python -m sounddevice 查看编号，再设置 BILISAMA_INPUT_DEVICE。"
fi

if [ -n "$ROOM_ID" ] && [[ ! "$ROOM_ID" =~ ^[1-9][0-9]*$ ]]; then
  die "BILISAMA_ROOM_ID 必须是正整数房间号，当前是：$ROOM_ID"
fi

if [ "${1:-}" = "--check" ]; then
  if [ -n "$ROOM_ID" ]; then
    printf '启动检查通过：模型 %s，输入设备 %s，真实房间 %s。\n' \
      "$MODEL" "$INPUT_DEVICE" "$ROOM_ID"
  else
    printf '启动检查通过：模型 %s，输入设备 %s，沙箱模式。\n' "$MODEL" "$INPUT_DEVICE"
  fi
  exit 0
fi
[ "$#" -eq 0 ] || die "未知参数：$*"

if backend_pid="$(running_backend_pid)"; then
  printf '检测到正在运行的 BiliSama（PID %s）。\n' "$backend_pid"
  start_pet_for_running_backend
  exit 0
fi

launch_args=(
  dev-talk
  --director
  --provider dashscope
  --model "$MODEL"
  --input-device "$INPUT_DEVICE"
  --mute-while-speaking
)
if [ -n "$ROOM_ID" ]; then
  launch_args+=(--room "$ROOM_ID")
  printf '正在启动 BiliSama：模型 %s，输入设备 %s，真实房间 %s。\n' \
    "$MODEL" "$INPUT_DEVICE" "$ROOM_ID"
else
  printf '正在启动 BiliSama：模型 %s，输入设备 %s，沙箱模式。\n' "$MODEL" "$INPUT_DEVICE"
fi
exec "$BILISAMA" "${launch_args[@]}"
