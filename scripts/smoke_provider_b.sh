#!/usr/bin/env bash
# 验 provider (b) 能不能装、能不能起。
#
# 这是计划 §13 第 1 条：装不上就等于备用腿不存在，必须第一周知道。
# speech-to-speech 在 macOS 上硬 pin 了 torch / transformers / mlx 的确切版本，
# 而这台机器上从来没装起来过。
#
# 用法：
#   scripts/smoke_provider_b.sh resolve   # 只解析依赖，不下载。几秒钟
#   scripts/smoke_provider_b.sh install   # 真装。macOS 约 385 MiB wheel
#   scripts/smoke_provider_b.sh serve     # 起服务并等它就绪
#   scripts/smoke_provider_b.sh all
set -euo pipefail

# 上游检出通常是本仓库的兄弟目录
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S2S_ROOT="${BILISAMA_S2S_ROOT:-$REPO_ROOT/../speech-to-speech}"
VENV="${BILISAMA_S2S_VENV:-$HOME/.local/share/bilisama/engines/s2s}"
CONFIG="${BILISAMA_S2S_CONFIG:-config/s2s/bilisama-s2s.json}"
PYVER="${BILISAMA_S2S_PYTHON:-3.12}"

# 中国大陆网络下这两条决定成败。UV_PYTHON_INSTALL_MIRROR 最容易被漏掉：
# uv 下载 CPython 走的是 GitHub Release。
export UV_NO_PROGRESS=1
export UV_NO_CONFIG=1
: "${UV_DEFAULT_INDEX:=https://pypi.tuna.tsinghua.edu.cn/simple}"
: "${UV_INDEX_URL:=$UV_DEFAULT_INDEX}"
export UV_DEFAULT_INDEX UV_INDEX_URL

log() { printf '\033[36m[smoke]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[smoke] %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$S2S_ROOT" ] || die "找不到 speech-to-speech 检出：$S2S_ROOT"
command -v uv >/dev/null || die "需要 uv。brew install uv"

cmd_resolve() {
  log "只解析依赖，不下载。这一步能几秒钟告诉我们那些硬 pin 是否可满足。"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  uv venv --python "$PYVER" "$tmp/venv" >/dev/null
  if VIRTUAL_ENV="$tmp/venv" uv pip install --dry-run --prerelease=allow \
       -e "$S2S_ROOT" 2>&1 | tail -30; then
    log "依赖可以解析。"
  else
    die "依赖解析失败。备用腿不存在，方案里 provider (b) 那一路要重新评估。"
  fi
}

cmd_install() {
  log "装到独立 venv：$VENV"
  log "它的 pin（torch/transformers/mlx）绝不能进 BiliSama 自己的环境。"
  mkdir -p "$(dirname "$VENV")"
  uv venv --python "$PYVER" "$VENV"
  # --prerelease=allow：上游 pyproject 自己设了这个，不带的话解析结果会不一致
  VIRTUAL_ENV="$VENV" uv pip install --prerelease=allow -e "$S2S_ROOT"
  VIRTUAL_ENV="$VENV" "$VENV/bin/python" -c "import speech_to_speech; print('版本', speech_to_speech.__version__)"
  log "装好了。"
}

cmd_serve() {
  [ -f "$CONFIG" ] || die "没有启动配置。先跑：bilisama config render-s2s"
  [ -x "$VENV/bin/python" ] || die "还没装。先跑：$0 install"
  log "起服务，配置：$CONFIG"
  # 官方三段管线按计划 §3.4 就是零补丁模式：它自带 TTS，隐式回复要出声。
  # 之前这里吃了 shim 的默认值（补丁全开），补丁 A 把隐式回复钉成纯文本，
  # 整场语音对话就哑了。要测产品路径（我们自己的 TTS）时显式设：
  #   BILISAMA_S2S_PATCHES=text_modality,raw_instructions
  export BILISAMA_S2S_PATCHES="${BILISAMA_S2S_PATCHES-}"
  log "补丁：${BILISAMA_S2S_PATCHES:-零补丁（官方管线默认）}"
  # 补丁走 PYTHONPATH 注入，不改上游一个字节
  PYTHONPATH="$PWD/tools/s2s_shim" \
    "$VENV/bin/python" -m bilisama_s2s_shim serve "$CONFIG" &
  local pid=$!
  trap 'kill "$pid" 2>/dev/null || true' EXIT

  local port
  port="$(python3 -c "import json,sys; print(json.load(open('$CONFIG'))['port'])")"
  for _ in $(seq 1 120); do
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
      log "端口 $port 起来了。注意：端口开了不等于模型加载完。"
      sleep 2
      log "服务就绪。Ctrl-C 停。"
      wait "$pid"
      return 0
    fi
    sleep 1
  done
  die "120 秒还没起来。看上面的日志。"
}

case "${1:-resolve}" in
  resolve) cmd_resolve ;;
  install) cmd_install ;;
  serve)   cmd_serve ;;
  all)     cmd_resolve; cmd_install; cmd_serve ;;
  *)       die "用法：$0 {resolve|install|serve|all}" ;;
esac
