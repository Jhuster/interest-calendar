#!/bin/sh
# 本机与可信局域网的前台启动。未设置 BASE_URL 时探测局域网地址。不传应用的 --dev。
# 公网云虚拟机使用 start-ecs.sh，不要用本脚本。
set -eu
BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
. "$BIN_DIR/runtime-env.sh"
command -v uv >/dev/null 2>&1 || { printf '%s\n' '请先运行 bin/init.sh 安装运行依赖。' >&2; exit 1; }
DATA_DIR=${CALENDAR_DATA_DIR:-"$BIN_DIR/data"}
if [ -z "${BASE_URL:-}" ]; then
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)
  LAN_IP=${LAN_IP:-127.0.0.1}
  BASE_URL="http://${LAN_IP}:8787"
fi
# Resolve caller-relative data paths before changing the working directory.
mkdir -p "$DATA_DIR"
DATA_DIR=$(CDPATH= cd -- "$DATA_DIR" && pwd)
unset VIRTUAL_ENV
cd "$PROJECT_DIR/src"
exec uv run --locked --no-dev --project "$PROJECT_DIR/src" python -m app serve --data-dir "$DATA_DIR" --base-url "$BASE_URL"
