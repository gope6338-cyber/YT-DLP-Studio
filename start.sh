#!/usr/bin/env sh
set -eu

mkdir -p "${DEFAULT_SAVE_DIR:-/data/downloads}"
mkdir -p "${APP_CACHE_DIR:-/data/cache}"
mkdir -p "${APP_TEMP_DIR:-/data/tmp}"

exec python -m gunicorn \
  --workers 1 \
  --worker-class gthread \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-0}" \
  --bind "${HOST:-0.0.0.0}:${PORT:-8000}" \
  app:app
