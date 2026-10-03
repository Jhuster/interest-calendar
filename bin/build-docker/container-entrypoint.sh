#!/bin/sh
set -eu
: "${BASE_URL:=http://127.0.0.1:8787}"
unset VIRTUAL_ENV
cd /opt/interest-calendar/src
set -- uv run --locked --no-dev --project /opt/interest-calendar/src python -m app serve \
  --data-dir /opt/interest-calendar/bin/data \
  --base-url "$BASE_URL"
if [ -n "${CALENDAR_CERT:-}" ] || [ -n "${CALENDAR_KEY:-}" ]; then
  if [ -z "${CALENDAR_CERT:-}" ] || [ -z "${CALENDAR_KEY:-}" ]; then
    printf '%s\n' 'CALENDAR_CERT 与 CALENDAR_KEY 必须同时设置。' >&2
    exit 1
  fi
  set -- "$@" --cert "$CALENDAR_CERT" --key "$CALENDAR_KEY"
fi
exec "$@"
