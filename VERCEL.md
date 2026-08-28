# Deploying YT-DLP Studio on Vercel

This repo ships a **Vercel-native deployment** alongside the classic
self-hosted server. No external backend is required.

## Quick deploy

1. Push this repo to GitHub.
2. On [vercel.com](https://vercel.com) → **Add New → Project** → import the repo.
3. Framework Preset: **Other** (auto-detected). Leave Build Command and
   Output Directory empty — everything is configured by `vercel.json`.
4. Click **Deploy**.

That's it. Dependencies come from `requirements.txt` (Flask + yt-dlp +
imageio-ffmpeg, which bundles a static `ffmpeg` binary — Vercel has no
system ffmpeg).

## How the deployment is wired

```
vercel.json          outputDirectory: static  → UI served from the CDN
                     rewrite /api/(.*) → /api/index  → Python function
api/index.py         the single serverless function (entrypoint)
api/_shared/         all backend code, bundled with the function
  serverless_backend.py   Flask app: /api/info, /api/resolve, /api/download,
                          /api/clip, /api/ai/*, /api/health
  media_info.py           shared yt-dlp metadata helpers
  config.py / ai_helper.py  env config + AI (Inference API mode)
static/              index.html / app.js / style.css (served statically)
```

Only the `api/` directory tree is bundled into the function — that is why
the shared modules live in `api/_shared/` rather than the repo root (the
Vercel Python runtime does not include root-level modules in the bundle).
The underscore prefix keeps `_shared` from being detected as its own
function. The page detects it's talking to the serverless backend at
runtime via `GET /api/health` and switches the download queue to its
client-side implementation.

## How the Vercel deployment differs from self-hosting

Vercel functions are short-lived and stateless, so the original architecture
(background queue thread + SSE + yt-dlp subprocesses writing to a server
folder) cannot run there. The serverless backend adapts instead:

| Feature | Self-hosted (`app.py`) | Vercel (`api/index.py`) |
|---|---|---|
| Video metadata (`/api/info`) | ✅ | ✅ same |
| Download queue | server-side, SSE updates | **client-side in the browser** (sequential, real progress, cancellable) |
| Where files are saved | server folder (`DEFAULT_SAVE_DIR`) | **your computer** — save dialog / browser Downloads |
| Full video download | yt-dlp subprocess | resolved by yt-dlp, streamed through the function (progressive) or merged to MP4 in `/tmp` (best quality) |
| Audio (MP3) | ffmpeg subprocess | downloaded + converted in `/tmp`, returned as a file |
| Clip export | download full video, cut with ffmpeg | `--download-sections` (only the segment is fetched), returned as a file |
| AI Smart Clips | local torch/transformers models | **Hugging Face Inference API only** — set `USE_HF_INFERENCE_API=true` (see below) |
| Save-directory field | server path | disabled ("Browser downloads") |

The frontend detects serverless mode from the boot config injected into the
page and swaps the queue implementation automatically — same UI, same routes.

## Optional environment variables (Project → Settings → Environment Variables)

| Variable | Default | Purpose |
|---|---|---|
| `USE_HF_INFERENCE_API` | `false` | Enables AI Smart Clips via the Hugging Face Inference API (no torch in the serverless bundle). Without it, AI endpoints return a friendly 501. |
| `HF_TOKEN` (or `HF_API_KEY`) | — | Hugging Face token; avoids rate limits. Recommended when the AI feature is on. |
| `MAX_VIDEO_HEIGHT` | `1080` | Cap the resolution prepared server-side. |
| `MAX_MERGE_BYTES` | `419430400` (400 MB) | Cap for merged best-quality / MP3 downloads. |
| `MAX_CLIP_SECONDS` | `600` | Max length of one exported clip. |
| `MAX_CLIP_BYTES` | `419430400` (400 MB) | Cap for one exported clip. |

## Known limits (by design of the platform)

- **Function duration**: configured at `60`s (the Hobby-plan maximum). If a
  stream is cut off, the frontend automatically falls back to handing the
  resolved media URL to the browser as a direct download. On the Pro plan you
  can raise `maxDuration` in `vercel.json` up to 300s for large files on slow
  connections.
- **Writable storage**: only `/tmp` (~512 MB), which is why the size caps
  above exist. Processed (merged/MP3/clip) requests must fit; plain streamed
  downloads pass through without touching disk.
- **AI**: torch + transformers exceed the serverless bundle-size limit, so AI
  runs only through the Hugging Face Inference API (opt-in, above). For local
  AI models, use the Docker/self-hosted deployment.
- Very large videos on very slow connections may be better served by the
  self-hosted deployment, which has no time cap.

## Run the serverless app locally (exactly what Vercel serves)

```sh
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:8080 --chdir api 'index:app'
```

(UI + API on one port for convenience; on Vercel the UI comes from the CDN.)

## Deploy from the CLI instead

```sh
npm i -g vercel
vercel          # preview
vercel --prod   # production
```
