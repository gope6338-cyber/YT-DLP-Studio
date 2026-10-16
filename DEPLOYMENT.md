# YT-DLP Studio — Deployment Guide

## Runtime shape

The application keeps its queue, task table and SSE listeners in process memory,
so it must run as **a single process with a single worker**. Horizontal scaling
(several replicas) is not supported: each replica would have its own queue, and
a browser talking to replica B would never see jobs created on replica A.

| Requirement | Why |
|---|---|
| 1 process, 1 gunicorn worker | queue/tasks/SSE state is in-process |
| `ffmpeg` on PATH | merging streams and cutting clips |
| Persistent storage for `DEFAULT_SAVE_DIR` | downloads and clips must survive restarts |
| Persistent storage for `APP_CACHE_DIR` | analysis cache + Hugging Face model weights |
| Enough RAM for the AI models | `bart-large` needs ~1.5 GB resident when loaded |

## Build and run

```bash
# Local development (loopback, no auth)
python app.py

# Local, production-like
sh ./start.sh

# Docker
docker build -t yt-dlp-studio .
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v ytdlp_data:/data \
  yt-dlp-studio
```

`start.sh` intentionally uses the **gevent** worker. The UI keeps a server sent
event stream open for as long as a tab is open; the threaded worker class runs
out of threads (measured: the whole app stops responding at four open tabs),
whereas an idle stream costs nothing under gevent.

## Security

The app has **no authentication by default** — that is only safe on loopback.

Set `APP_TOKEN` before exposing it to any network:

```bash
APP_TOKEN=$(openssl rand -hex 32)
```

With a token configured:

* every page and `/api/*` route requires a signed-in session;
* the token is entered once on `/login` and stored in an httpOnly cookie;
* scripts can send `X-App-Token: <token>` instead of using a session;
* behind HTTPS also set `SESSION_COOKIE_SECURE=true`.

Independently of the token, **downloads are constrained to `DEFAULT_SAVE_DIR`**
(plus any paths in `EXTRA_SAVE_DIRS`). A request with a `save_dir` outside those
roots is rejected with HTTP 400. This is enforced in `config.resolve_save_dir`
and covered by tests.

## Storage

| Variable | Purpose |
|---|---|
| `DEFAULT_SAVE_DIR` | downloads and exported clips |
| `APP_CACHE_DIR` | analysis cache and model weights |
| `APP_TEMP_DIR` | scratch space while a clip is being cut |
| `EXTRA_SAVE_DIRS` | extra allowed output roots, `:`-separated |

The container runs as the non-root `app` user. `/data` is created and chowned in
the image so a fresh named volume inherits the right ownership; if you bind
mount a host directory instead, make sure it is writable by uid 10001.

## Memory and the AI path

Running the models locally needs roughly 1.5 GB on top of the interpreter. If
your host cannot provide that, either:

* set `USE_HF_INFERENCE_API=true` to call the Hugging Face serverless API
  instead (add `HF_TOKEN` to avoid rate limits), or
* keep `AI_IDLE_UNLOAD_SECONDS` at its default so models are freed after 15
  minutes of inactivity and reloaded on demand.

Hosts that scale to zero (Render Free, Koyeb Free) will also lose the model
weights and the in-memory queue between requests, and do not offer persistent
disks. Use a host with an always-on process and a mounted volume.

## Reverse proxy

If you terminate TLS in front of the app, forward the `Host` header and allow
streaming responses without buffering, otherwise SSE updates arrive late:

```nginx
location / {
    proxy_pass         http://127.0.0.1:8000;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_buffering    off;   # required for /api/stream
    proxy_read_timeout 3600s;
}
```

## Health check

`GET /api/health` returns `{"status": "ok", "tasks": {...}}` and needs no
authentication — point your platform's health probe at it.
