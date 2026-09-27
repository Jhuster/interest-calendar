#!/bin/sh
set -eu
: "${BASE_URL:=http://127.0.0.1:8787}"
unset VIRTUAL_ENV
cd /opt/interest-calendar/src
exec uv run --locked --no-dev --project /opt/interest-calendar/src python -m app serve \
  --data-dir /opt/interest-calendar/bin/data \
  --base-url "$BASE_URL"
