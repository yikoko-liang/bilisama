#!/usr/bin/env bash
# Finder entry point for the shell launcher beside this file.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/start_bilisama.sh" || {
  status=$?
  printf '\n启动没有成功。按回车关闭这个窗口。'
  read -r _
  exit "$status"
}
