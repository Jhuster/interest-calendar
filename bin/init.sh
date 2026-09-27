#!/bin/sh
set -eu
BIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf '%s\n' '目前支持 macOS 和 Linux，请在支持的系统上运行。' >&2; exit 1 ;;
esac
[ -f "$PROJECT_DIR/src/pyproject.toml" ] || { printf '%s\n' '缺少 src 目录，请下载完整项目。' >&2; exit 1; }
. "$BIN_DIR/runtime-env.sh"
unset VIRTUAL_ENV
mkdir -p "$BIN_DIR/runtime/tools"
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' '正在从官方来源安装 uv…'
  installer=$(mktemp)
  trap 'rm -f "$installer"' EXIT
  trap 'exit 1' HUP INT TERM
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --retry 2 https://astral.sh/uv/install.sh --output "$installer"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$installer" https://astral.sh/uv/install.sh
  else
    printf '%s\n' '请先通过系统包管理器安装 curl 或 wget，再运行本脚本。' >&2
    exit 1
  fi
  UV_INSTALL_DIR="$BIN_DIR/runtime/tools" UV_NO_MODIFY_PATH=1 sh "$installer"
fi
printf '%s\n' '正在准备 Python 3.12 和项目依赖，首次运行需要联网…'
uv python install 3.12 --no-bin
uv sync --project "$PROJECT_DIR/src" --python 3.12 --managed-python --locked --no-dev
printf '%s\n' '初始化完成！运行 ./bin/start.sh 启动服务，或 ./bin/start-background.sh 后台启动。'
