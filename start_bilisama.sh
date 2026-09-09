#!/usr/bin/env bash
# The streamer's entrance: checks the environment in Chinese, fills sensible
# defaults, then hands over to `bilisama dev-talk --director`.
#
# The script OWNS five knobs (--provider/--model/--voice/--room/--input-device, each
# flag > BILISAMA_* env > default) because it has something to add to them:
# credential checks, mic auto-detection, room validation. Every other flag
# passes through to dev-talk untouched, so this file never becomes another
# place that has to learn a new dev-talk option by name.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
BILISAMA="$REPO_ROOT/.venv/bin/bilisama"
PET_DIR="$REPO_ROOT/desktop/preview"
ELECTRON="$PET_DIR/node_modules/.bin/electron"
ELECTRON_RUNTIME="$PET_DIR/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
ENDPOINT_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/bilisama/ui/endpoint.json"

# Volcano SC2.0 is the shipped streamer path. Provider-specific defaults are
# filled only after argument parsing, so overriding the provider never leaks a
# Volcano model or speaker into DashScope or a local backend.
PROVIDER="${BILISAMA_PROVIDER:-volcano}"
MODEL="${BILISAMA_REALTIME_MODEL:-}"
VOICE="${BILISAMA_VOICE:-}"
ROOM_ID="${BILISAMA_ROOM_ID:-}"
INPUT_DEVICE="${BILISAMA_INPUT_DEVICE:-}"

die() {
  printf '\033[31m启动失败：%s\033[0m\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：./start_bilisama.sh [选项] [其余 dev-talk 参数]

自己认的选项（也可用同名 BILISAMA_* 环境变量，选项优先）：
  --provider <名字>       语音后端（默认 volcano；BILISAMA_PROVIDER）
  --model <模型名>        托管服务的模型（BILISAMA_REALTIME_MODEL）。火山默认
                          2.2.0.0；DashScope 默认 qwen-audio-3.0-realtime-flash
  --voice <音色>          火山默认 saturn_zh_female_keainvsheng_tob
                          （BILISAMA_VOICE）；换版本时必须一起换匹配音色
  --room <房间号>         连真实直播间，正整数（默认沙箱模式；BILISAMA_ROOM_ID）
  --input-device <编号>   麦克风设备（默认自动找内置麦；BILISAMA_INPUT_DEVICE）
  --check                 只做环境检查，不启动
  -h, --help              看这份说明

其余参数原样交给 dev-talk，常用的比如：
  --skin kirby            本次换皮肤包（不改配置文件）
  --persona hanako        临时换人设
  --open                  界面起来后自动开浏览器
  --no-pet                只要浏览器界面，不要悬浮窗
完整清单见 .venv/bin/bilisama dev-talk --help。

例子：
  ./start_bilisama.sh
  ./start_bilisama.sh --room 21452505 --skin kirby
  ./start_bilisama.sh --provider dashscope --voice longanlingxin
EOF
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

# --- 参数解析：五个自有选项收走，其余原样透传 --------------------------------

need_value() {
  # $1 = flag name, $2 = remaining arg count after the flag
  [ "$2" -ge 1 ] || die "$1 后面要跟一个值。"
}

CHECK_ONLY=0
EXTRA_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --check) CHECK_ONLY=1 ;;
    --provider)   shift; need_value --provider "$#";     PROVIDER="$1" ;;
    --provider=*) PROVIDER="${1#*=}" ;;
    --model)      shift; need_value --model "$#";        MODEL="$1" ;;
    --model=*)    MODEL="${1#*=}" ;;
    --voice)      shift; need_value --voice "$#";        VOICE="$1" ;;
    --voice=*)    VOICE="${1#*=}" ;;
    --room)       shift; need_value --room "$#";         ROOM_ID="$1" ;;
    --room=*)     ROOM_ID="${1#*=}" ;;
    --input-device)   shift; need_value --input-device "$#"; INPUT_DEVICE="$1" ;;
    --input-device=*) INPUT_DEVICE="${1#*=}" ;;
    *) EXTRA_ARGS+=("$1") ;;
  esac
  shift
done

if [ -z "$MODEL" ]; then
  case "$PROVIDER" in
    volcano) MODEL="2.2.0.0" ;;
    dashscope) MODEL="qwen-audio-3.0-realtime-flash" ;;
  esac
fi
if [ -z "$VOICE" ] && [ "$PROVIDER" = "volcano" ]; then
  VOICE="saturn_zh_female_keainvsheng_tob"
fi

has_extra() {
  local wanted="$1" arg
  for arg in ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; do
    [ "$arg" = "$wanted" ] && return 0
  done
  return 1
}

cd "$REPO_ROOT"

[ -x "$PYTHON" ] && [ -x "$BILISAMA" ] || die "Python 环境没装好。先在仓库运行 uv sync。"
[ -f "$REPO_ROOT/path.sh" ] || die "仓库根目录缺少 path.sh。"
[ -x "$ELECTRON_RUNTIME" ] || die "桌面 UI 没装好。先在 $PET_DIR 运行 npm install。"

# shellcheck disable=SC1091
source "$REPO_ROOT/path.sh"
if [ "$PROVIDER" = "dashscope" ]; then
  [ -n "${ali_api_key:-}" ] || die "path.sh 里缺少 ali_api_key。"
  [ -n "${dashscope_url:-}" ] || die "path.sh 里缺少 dashscope_url。"
fi
# 其它后端（volcano/s2s）的凭据与可达性由 dev-talk 启动时自检，缺什么它会用
# 中文说清楚——这里不重复造一套检查，免得把钥匙串里配好引用的人误拦在门外。

if [ -z "$INPUT_DEVICE" ]; then
  if ! INPUT_DEVICE="$(find_builtin_input)"; then
    if has_extra --wav || printf '%s\n' ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} | grep -q '^--wav='; then
      INPUT_DEVICE=""  # 喂 WAV 的场次用不上麦克风，别为此拦下启动
    else
      die "找不到 MacBook 内置麦克风。运行 .venv/bin/python -m sounddevice 查看编号，再用 --input-device 或 BILISAMA_INPUT_DEVICE 指定。"
    fi
  fi
fi

if [ -n "$ROOM_ID" ] && [[ ! "$ROOM_ID" =~ ^[1-9][0-9]*$ ]]; then
  die "房间号必须是正整数，当前是：$ROOM_ID"
fi

describe_plan() {
  local room_text="沙箱模式"
  [ -n "$ROOM_ID" ] && room_text="真实房间 $ROOM_ID"
  local model_text="${MODEL:-从 [speech.$PROVIDER] 读}"
  local device_text="$INPUT_DEVICE"
  [ -z "$device_text" ] && device_text="无（WAV 模式）"
  printf '后端 %s，模型 %s' "$PROVIDER" "$model_text"
  if [ -n "$VOICE" ]; then
    printf '，音色 %s' "$VOICE"
  fi
  printf '，输入设备 %s，%s' "$device_text" "$room_text"
  if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
    printf '，透传参数：%s' "${EXTRA_ARGS[*]}"
  fi
  printf '\n'
}

if [ "$CHECK_ONLY" = 1 ]; then
  printf '启动检查通过：'
  describe_plan
  exit 0
fi

if backend_pid="$(running_backend_pid)"; then
  printf '检测到正在运行的 BiliSama（PID %s）。\n' "$backend_pid"
  if [ "${#EXTRA_ARGS[@]}" -gt 0 ] || [ -n "$ROOM_ID" ]; then
    printf '注意：这次给的参数对已经在跑的后端不生效。想换配置先退出它\n'
    printf '（右键桌宠形象选退出，或 kill %s），再重新启动。\n' "$backend_pid"
  fi
  start_pet_for_running_backend
  exit 0
fi

launch_args=(
  dev-talk
  --director
  --provider "$PROVIDER"
)
[ -n "$INPUT_DEVICE" ] && launch_args+=(--input-device "$INPUT_DEVICE")
[ -n "$MODEL" ] && launch_args+=(--model "$MODEL")
[ -n "$VOICE" ] && launch_args+=(--voice "$VOICE")
[ -n "$ROOM_ID" ] && launch_args+=(--room "$ROOM_ID")
launch_args+=(${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"})

printf '正在启动 BiliSama：'
describe_plan
exec "$BILISAMA" "${launch_args[@]}"
