#!/bin/sh
set -eu
BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
. "$BIN_DIR/runtime-env.sh"
DATA_DIR=${CALENDAR_DATA_DIR:-"$BIN_DIR/data"}
if [ -z "${BASE_URL:-}" ]; then
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)
  LAN_IP=${LAN_IP:-127.0.0.1}
  BASE_URL="http://${LAN_IP}:8787"
fi
mkdir -p "$DATA_DIR"
DATA_DIR=$(CDPATH= cd -- "$DATA_DIR" && pwd)
LOG_FILE=${CALENDAR_LOG_FILE:-"$DATA_DIR/server.log"}
mkdir -p "$(dirname -- "$LOG_FILE")"
export BASE_URL
export CALENDAR_DATA_DIR="$DATA_DIR"
command -v uv >/dev/null 2>&1 || { printf '%s\n' '请先运行 bin/init.sh 安装运行依赖。' >&2; exit 1; }
# Rotate before this launch opens the log. A later rename would not affect the already-open file.
(cd "$PROJECT_DIR/src" && uv run --locked --no-dev --project "$PROJECT_DIR/src" python -c 'from app.services.maintenance import rotate_log; import sys; rotate_log(sys.argv[1])' "$LOG_FILE")
chmod 0600 "$LOG_FILE"
touch "$LOG_FILE"
LOG_START=$(wc -l < "$LOG_FILE" | tr -d ' ')
nohup "$BIN_DIR/start.sh" >>"$LOG_FILE" 2>&1 </dev/null &
SERVICE_PID=$!
printf '后台进程：%s\n日志文件：%s\n' "$SERVICE_PID" "$LOG_FILE"
ATTEMPTS=0
while [ "$ATTEMPTS" -lt 30 ]; do
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    printf '启动失败，请查看日志：%s\n' "$LOG_FILE" >&2
    exit 1
  fi
  if tail -n +"$((LOG_START + 1))" "$LOG_FILE" | grep -q 'Application startup complete'; then
    printf '浏览器访问地址：%s/\n' "${BASE_URL%/}"
    ADMIN_TOKEN=
    if [ -f "$DATA_DIR/credentials.json" ]; then
      ADMIN_TOKEN=$(sed -n 's/.*"admin_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$DATA_DIR/credentials.json" | head -n 1)
    fi
    if [ -z "$ADMIN_TOKEN" ] && [ -f "$DATA_DIR/first-run-credentials.txt" ]; then
      ADMIN_TOKEN=$(sed -n 's/^ADMIN_TOKEN=//p' "$DATA_DIR/first-run-credentials.txt" | head -n 1)
    fi
    if [ -n "$ADMIN_TOKEN" ]; then
      printf '管理令牌（请保存，不会写入日志）：%s\n' "$ADMIN_TOKEN"
      printf '令牌文件：%s\n' "$DATA_DIR/credentials.json"
    else
      printf '%s\n' '管理令牌原文不可恢复；请使用已保存令牌或在本机轮换后再次启动。'
    fi
    exit 0
  fi
  sleep 1
  ATTEMPTS=$((ATTEMPTS + 1))
done
printf '服务仍在启动，完成后的地址：%s/\n进度请查看日志：%s\n' "${BASE_URL%/}" "$LOG_FILE"
