#!/bin/sh
set -eu
DOCKER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR=$(CDPATH= cd -- "$DOCKER_DIR/.." && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$BIN_DIR/.." && pwd)
IMAGE_NAME=${IMAGE_NAME:-interest-calendar:local}
exec docker build -f "$DOCKER_DIR/Dockerfile" -t "$IMAGE_NAME" "$PROJECT_DIR"
