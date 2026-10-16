#!/usr/bin/env sh
set -eu

mkdir -p "${DEFAULT_SAVE_DIR:-/data/downloads}"
mkdir -p "${APP_CACHE_DIR:-/data/cache}"
mkdir -p "${APP_TEMP_DIR:-/data/tmp}"

# The app streams queue updates over server sent events, and every open browser
# tab holds one connection open for the life of the tab. The threaded worker
# class runs out of threads (the app stops responding entirely once they are
# all held by SSE streams), so we use the gevent worker, where an idle stream
# costs nothing.
#
# --workers must stay at 1: the queue, task dict and SSE listeners are all
# in-process state.
exec python -m gunicorn \
  --workers 1 \
  --worker-class gevent \
  --worker-connections "${GUNICORN_WORKER_CONNECTIONS:-1000}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
  --bind "${HOST:-0.0.0.0}:${PORT:-8000}" \
  app:app
