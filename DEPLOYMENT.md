# Calm Lovelace Deployment Guide

## What changed

- Added env-driven runtime configuration for host, port, save path, cache path, temp path, and binary locations.
- Kept all existing routes, payloads, queue semantics, SSE behavior, AI endpoints, and frontend workflows unchanged.
- Replaced Windows-only process cancellation internals with cross-platform subprocess-group handling.
- Added canonical container deployment files and platform configs for Render, Railway, Koyeb, and Vercel.
- **Vercel is now a first-class target**: the Python function `api/index.py` (with all backend code in `api/_shared/`) runs the API, `static/` is served from the Vercel CDN, and downloads stream to the browser with a client-side queue. See [VERCEL.md](VERCEL.md).
- Split Python dependencies: `requirements.txt` (serverless-safe core: Flask, yt-dlp, imageio-ffmpeg, gunicorn) and `requirements-full.txt` (local AI models: torch, transformers) — Vercel installs only the former.

## Why these changes were necessary

- Hosted platforms cannot rely on `C:\Users\...` paths, so save/cache/temp directories must come from environment variables.
- Linux hosts need `ffmpeg`, a portable startup command, and non-Windows process-group handling.
- The in-memory queue and SSE stream require a single app process, so the runtime contract is now explicit.
- Free serverless and scale-to-zero platforms can break long-running downloads, persistent output files, and large AI inference memory usage; platform docs below call out those limits instead of degrading features.

## Build and start commands

- Local Flask dev: `python app.py`
- Local production-like: `sh ./start.sh`
- Local serverless-mode (what Vercel serves): `gunicorn --bind 0.0.0.0:8080 'api.index:app'`
- Docker build: `docker build -t calm-lovelace .` (installs both `requirements.txt` and `requirements-full.txt`)
- Docker run: `docker run --rm -p 8000:8000 --env-file .env -v calm_lovelace_data:/data calm-lovelace`

## Environment variables

- `HOST`: bind address. Local default `127.0.0.1`.
- `PORT`: listening port. Local default `5000`.
- `DEFAULT_SAVE_DIR`: persistent output directory shown in UI and used for downloads/clips. (Ignored on Vercel.)
- `APP_CACHE_DIR`: app cache root, including AI transcript cache.
- `APP_TEMP_DIR`: temp working directory for clip assembly.
- `YT_DLP_BIN`: `yt-dlp` executable path.
- `FFMPEG_BIN`: `ffmpeg` executable path.
- `HF_HOME`, `HF_HUB_CACHE`, `TRANSFORMERS_CACHE`: optional model cache overrides.
- `USE_HF_INFERENCE_API`: set to `true` to enable Hugging Face Serverless Inference API fallback (instead of running heavy models locally). Required for the AI feature on Vercel (torch does not fit the serverless bundle). Default `false` (runs locally).
- `HF_TOKEN` (or `HF_API_KEY`): optional Hugging Face API token to avoid rate limits when using the Inference API.
- Serverless size/time caps (`MAX_MERGE_BYTES`, `MAX_CLIP_SECONDS`, `MAX_CLIP_BYTES`, `MAX_VIDEO_HEIGHT`): see [VERCEL.md](VERCEL.md).

## Platform matrix

- `Vercel`: **fully deployable from this repo** (`vercel.json` + `api/index.py`). Downloads/clips are delivered to the visitor's browser and the queue runs client-side; AI runs via the Hugging Face Inference API when `USE_HF_INFERENCE_API=true`. Subject to function duration/`/tmp` limits — see [VERCEL.md](VERCEL.md).
- `Railway Free/Trial`: deployable with `railway.json` + `Dockerfile`, but not full-fidelity on free resources because current AI path and persistent output needs exceed practical free-tier RAM/storage guarantees.
- `Render Free`: deployable with `render.yaml`, but not full-fidelity because Free web services spin down and do not support persistent disks.
- `Koyeb Free`: deployable template via `koyeb.yaml`, but not full-fidelity because Free instances have 512MB RAM, 2GB SSD, no volumes, and scale to zero.
- `Docker-compatible hosts`: full-fidelity if they allow one always-on container, persistent mounted storage, and enough RAM for the current AI models.

## Required runtime shape for full fidelity (self-hosted)

- Single replica / single process web service.
- Persistent mounted storage for `DEFAULT_SAVE_DIR`, `APP_CACHE_DIR`, and `APP_TEMP_DIR`.
- `ffmpeg` installed in the runtime image.
- Enough RAM for `torch` + `transformers` pipelines used by `ai_helper.py`.
