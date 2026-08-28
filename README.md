# YT-DLP Studio

A web studio for downloading YouTube videos, exporting clip segments, and
generating AI-suggested cuts — built on Flask, yt-dlp, and ffmpeg.

## Two deployment targets, one codebase

| Target | Entry point | Docs |
|---|---|---|
| **Vercel (serverless)** | `api/index.py` + `vercel.json` | [VERCEL.md](VERCEL.md) |
| **Self-hosted (Docker / gunicorn)** | `app.py` + `Dockerfile` / `start.sh` | [DEPLOYMENT.md](DEPLOYMENT.md) |

On Vercel, downloads are streamed to your browser (the queue runs client-side)
because serverless functions have no persistent processes or user-visible
disks. Self-hosting keeps the original server-side queue with SSE progress and
local AI models.

## Local development (self-hosted mode)

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # core deps
pip install -r requirements-full.txt       # optional: local AI models (torch)
python app.py                              # http://127.0.0.1:5000
```

`ffmpeg` must be on your `PATH` (or set `FFMPEG_BIN`).

## Local development (Vercel/serverless mode)

```sh
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8080 --chdir api 'index:app'
```

The Vercel function entrypoint is `api/index.py`; all backend code it needs
lives in `api/_shared/` (only the `api/` tree is bundled on Vercel).

## Docker (full self-hosted stack, includes AI models)

```sh
docker build -t yt-dlp-studio .
docker run --rm -p 8000:8000 --env-file .env \
  -v ytstudio_data:/data yt-dlp-studio
```

## Tests

```sh
python -m unittest test_app test_serverless
```

## Configuration

See [.env.example](.env.example), [DEPLOYMENT.md](DEPLOYMENT.md), and
[VERCEL.md](VERCEL.md) for every environment variable (paths, ports, ffmpeg /
yt-dlp binaries, AI mode, serverless size caps).
