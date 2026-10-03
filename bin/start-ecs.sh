#!/bin/sh
# 阿里云 ECS 等公网机器的前台启动。不探测局域网地址，不传应用的 --dev。
# 监听仍是程序默认：0.0.0.0，端口取自 BASE_URL，缺省 8787。本机请用 start.sh。
set -eu
BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
. "$BIN_DIR/runtime-env.sh"

fail() { printf '%s\n' "$1" >&2; exit 1; }

if [ -z "${BASE_URL:-}" ]; then
  fail '请设置 BASE_URL 为公网 HTTPS 源，例如 https://calendar.example.com。不要用 start.sh 的局域网探测。'
fi
case "$BASE_URL" in
  https://*) ;;
  *) fail 'BASE_URL 必须是 https 源。不要把 8787 以 HTTP 对 0.0.0.0/0 开放。' ;;
esac
origin=${BASE_URL#https://}
case "$origin" in
  ''|*'/'*|*'?'*|*'#'*|*'@'*) fail 'BASE_URL 必须是不含路径、查询和用户信息的 https 源。' ;;
esac
case "$origin" in
  '['*)
    case "$origin" in
      '['*']':*) host=${origin#\[}; host=${host%%\]*}; port=${origin##*:} ;;
      '['*']') host=${origin#\[}; host=${host%\]}; port= ;;
      *) fail 'IPv6 的 BASE_URL 必须写成 [地址] 或 [地址]:端口。' ;;
    esac
    ;;
  *:*) host=${origin%:*}; port=${origin##*:} ;;
  *) host=$origin; port= ;;
esac
case "$host" in
  ''|localhost|127.*|0.0.0.0|'::1') fail 'BASE_URL 必须是公网源，不能是回环地址。' ;;
esac
if [ -n "$port" ]; then
  case "$port" in
    *[!0-9]*) fail 'BASE_URL 的端口必须是数字。' ;;
  esac
fi

if [ -n "${CALENDAR_CERT:-}" ] || [ -n "${CALENDAR_KEY:-}" ]; then
  if [ -z "${CALENDAR_CERT:-}" ] || [ -z "${CALENDAR_KEY:-}" ]; then
    fail 'CALENDAR_CERT 与 CALENDAR_KEY 必须同时设置。只设一个不会启动。'
  fi
  [ -f "$CALENDAR_CERT" ] || fail "找不到证书：$CALENDAR_CERT"
  [ -f "$CALENDAR_KEY" ] || fail "找不到私钥：$CALENDAR_KEY"
fi

command -v uv >/dev/null 2>&1 || fail '请先运行 bin/init.sh 安装运行依赖。'
DATA_DIR=${CALENDAR_DATA_DIR:-"$BIN_DIR/data"}
mkdir -p "$DATA_DIR"
DATA_DIR=$(CDPATH= cd -- "$DATA_DIR" && pwd)
unset VIRTUAL_ENV
cd "$PROJECT_DIR/src"

printf '%s\n' '监听仍是程序默认的 0.0.0.0，端口取自 BASE_URL，未写端口时为 8787。本脚本不改绑定，也不传 --dev。' >&2
printf '%s\n' '安全组才是公网大门。订阅地址在设置页，形如 '"${BASE_URL%/}"'/c/<token>/calendar.ics，不是 /calendar.ics。' >&2
if [ -n "${CALENDAR_CERT:-}" ]; then
  printf '%s\n' '将使用 --cert/--key 在进程上终止 TLS。未写端口时手机应访问 8787 上的 HTTPS，或把 BASE_URL 写成带端口的源。' >&2
else
  printf '%s\n' '未提供证书：由反向代理终止 TLS，并把对外的 https 转到本机端口。不要把 8787 对 0.0.0.0/0 做 HTTP 发布。' >&2
fi

set -- uv run --locked --no-dev --project "$PROJECT_DIR/src" python -m app serve --data-dir "$DATA_DIR" --base-url "$BASE_URL"
if [ -n "${CALENDAR_CERT:-}" ]; then
  set -- "$@" --cert "$CALENDAR_CERT" --key "$CALENDAR_KEY"
fi
exec "$@"
