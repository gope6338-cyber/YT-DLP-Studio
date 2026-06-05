# Calm Lovelace Deployment Guide

## What changed

- Added env-driven runtime configuration for host, port, save path, cache path, temp path, and binary locations.
- Kept all existing routes, payloads, queue semantics, SSE behavior, AI endpoints, and frontend workflows unchanged.
- Replaced Windows-only process cancellation internals with cross-platform subprocess-group handling.
- Added canonical container deployment files and platform configs for Render, Railway, Koyeb, and Vercel.
- Reduced `requirements.txt` to direct runtime dependencies actually used by this project.

## Why these changes were necessary

- Hosted platforms cannot rely on `C:\Users\...` paths, so save/cache/temp directories must come from environment variables.
- Linux hosts need `ffmpeg`, a portable startup command, and non-Windows process-group handling.
- The in-memory queue and SSE stream require a single app process, so the runtime contract is now explicit.
- Free serverless and scale-to-zero platforms can break long-running downloads, persistent output files, and large AI inference memory usage; platform docs below call out those limits instead of degrading features.

## Build and start commands

- Local Flask dev: `python app.py`
- Local production-like: `sh ./start.sh`
- Docker build: `docker build -t calm-lovelace .`
- Docker run: `docker run --rm -p 8000:8000 --env-file .env -v calm_lovelace_data:/data calm-lovelace`

## Environment variables

- `HOST`: bind address. Local default `127.0.0.1`.
- `PORT`: listening port. Local default `5000`.
- `DEFAULT_SAVE_DIR`: persistent output directory shown in UI and used for downloads/clips.
- `APP_CACHE_DIR`: app cache root, including AI transcript cache.
- `APP_TEMP_DIR`: temp working directory for clip assembly.
- `YT_DLP_BIN`: `yt-dlp` executable path.
- `FFMPEG_BIN`: `ffmpeg` executable path.
- `HF_HOME`, `HF_HUB_CACHE`, `TRANSFORMERS_CACHE`: optional model cache overrides.
- `USE_HF_INFERENCE_API`: set to `true` to enable Hugging Face Serverless Inference API fallback (instead of running heavy models locally). Highly recommended for free-tier deployments (512MB RAM) to avoid OOM crashes. Default `false` (runs locally).
- `HF_TOKEN` (or `HF_API_KEY`): optional Hugging Face API token to avoid rate limits when using the Inference API.

## Platform matrix

- `Railway Free/Trial`: deployable with `railway.json` + `Dockerfile`, but not full-fidelity on free resources because current AI path and persistent output needs exceed practical free-tier RAM/storage guarantees.
- `Render Free`: deployable with `render.yaml`, but not full-fidelity because Free web services spin down and do not support persistent disks.
- `Koyeb Free`: deployable template via `koyeb.yaml`, but not full-fidelity because Free instances have 512MB RAM, 2GB SSD, no volumes, and scale to zero.
- `Vercel`: proxy-only frontend deployment through `vercel.json`; must point `/api/*` to a separate full backend. Vercel alone is not full-fidelity for this app.
- `Docker-compatible hosts`: full-fidelity if they allow one always-on container, persistent mounted storage, and enough RAM for the current AI models.

## Vercel note

- Replace `https://replace-with-full-backend.example.com` in `vercel.json` before deploying.
- This keeps the same browser routes and UI while forwarding API and SSE traffic to the real backend.

## Required runtime shape for full fidelity

- Single replica / single process web service.
- Persistent mounted storage for `DEFAULT_SAVE_DIR`, `APP_CACHE_DIR`, and `APP_TEMP_DIR`.
- `ffmpeg` installed in the runtime image.
- Enough RAM for `torch` + `transformers` pipelines used by `ai_helper.py`.
