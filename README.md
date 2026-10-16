# YT-DLP Studio

A self-hosted web studio for downloading, clipping and cutting up video from
YouTube and the ~1,000 other sites [yt-dlp](https://github.com/yt-dlp/yt-dlp)
supports. Paste a URL, scrub a timeline, export clips.

## What it does

**Download** — fetch a video at the best available quality, or just the audio.

**Clip** — mark regions on a canvas timeline and export them with ffmpeg, with
your choice of precise (re-encoded, lands exactly on the timestamp) or fast
(stream copy, snaps to the nearest keyframe) cutting.

**AI Smart Clips** — turn a transcript into cut points:

```
1. extract   download the subtitles and parse them into timed caption cues
2. analyse   chunk the transcript (~60s) and score each chunk for importance
3. sections  group the strongest chunks into clip ranges and label them
```

The result is a set of `{start, end, label}` ranges that land directly on the
timeline as editable clip regions, ready to export. This is the part of the app
that was previously wired up backwards: it produced embeddings that nothing
consumed, and no UI ever called its search endpoint.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional but recommended: ffmpeg for merging and cutting
python app.py          # http://127.0.0.1:5000
```

Then open the page, paste a video URL and press **Generate AI Cuts**.

For anything exposed beyond loopback, read [DEPLOYMENT.md](DEPLOYMENT.md) first —
you will want `APP_TOKEN` set and the gevent-based `start.sh` entrypoint.

## Configuration

All configuration is environment driven; see [.env.example](.env.example) for
the full list with commentary. The important ones:

| Variable | Default | Notes |
|---|---|---|
| `APP_TOKEN` | *empty* | empty = no auth (loopback only); set it to require sign-in |
| `DEFAULT_SAVE_DIR` | `~/Downloads` | downloads are restricted to this root |
| `EXTRA_SAVE_DIRS` | *empty* | additional allowed output roots, `:` separated |
| `CLIP_CUT_MODE` | `precise` | `precise` re-encodes, `fast` stream-copies |
| `USE_HF_INFERENCE_API` | `false` | use the HF API instead of local models |
| `AI_MAX_SECTIONS` | `8` | how many clip ranges the analysis returns |
| `AI_MIN/MAX_SECTION_SECONDS` | `15` / `240` | length bounds for a suggested clip |
| `AI_CACHE_TTL_SECONDS` | `604800` | analysis cache lifetime |
| `MAX_TASK_HISTORY` | `200` | finished tasks kept in memory |

## Architecture

```
browser ──REST + SSE──▶ Flask (app.py)
                          │
                          ├── queue.Queue ──▶ single worker thread
                          │                     ├── yt-dlp subprocess   (download)
                          │                     ├── yt-dlp + ffmpeg     (clip export)
                          │                     └── ai_helper           (analysis)
                          └── static/ (index.html, app.js, style.css)
```

One worker thread runs jobs one at a time on purpose: downloads, ffmpeg and
BART inference are all heavy, and serialising them keeps memory predictable and
avoids two model loads racing.

## Tests

```bash
python -m pytest test_app.py -q
```

53 tests cover configuration, save-path containment, format selection, clip
argument building, subtitle parsing, section building, the analysis pipeline
(with the model calls mocked), the REST API, and the auth flow.

## Notes and limits

* The embedded preview uses the YouTube IFrame API, so it only works for
  YouTube URLs. Metadata, downloads, AI cuts and exports work for any site
  yt-dlp supports.
* Transcript extraction depends on the video having subtitles or automatic
  captions. English is tried first, then any available language. There is no
  speech-to-text fallback.
* Everything is in memory: restarting the server clears the queue (finished
  downloads on disk are unaffected).

## Files

| File | Role |
|---|---|
| `app.py` | Flask app, job queue, SSE, downloads, clip export |
| `ai_helper.py` | transcript extraction, scoring, section building |
| `config.py` | env-driven configuration |
| `static/` | single-page UI |
| `test_app.py` | test suite |
| `tests/fakebin/` | offline `yt-dlp`/`ffmpeg` stubs used for local probing |
| `ANALYSIS.md` | the audit that drove the current rewrite |
