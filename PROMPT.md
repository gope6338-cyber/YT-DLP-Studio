# Change prompt — YT-DLP Studio

**How to use this file**

This is the task prompt for the change made on this branch: *make YT-DLP Studio
safe to expose beyond localhost, stop it stalling under normal use, and rebuild
the AI feature so it genuinely goes transcript → analyse → clip cuts.*

Hand section 2 to any coding agent (or a fresh session) to redo the work from a
clean checkout. Section 3 lists the traps that are easy to get wrong, and
section 4 is the acceptance checklist.

- Redo from scratch: point the agent at the pre-change commit `47b9f4a`.
- Extend what is already there: point it at the current branch and ignore
  anything already satisfied.

---

## 1. One-paragraph version

> YT-DLP Studio is a single-process Flask app that downloads video with yt-dlp,
> cuts clips with ffmpeg, and suggests clip ranges from a video transcript using
> Hugging Face models. Three things are wrong with it. First, it is unsafe to
> expose: `save_dir` from the request body is passed straight to `makedirs()` and
> yt-dlp, so anyone who can reach the port can create directories and fetch
> remote content anywhere on the disk, and there is no authentication at all.
> Second, it stalls: every open browser tab holds an SSE connection, and the
> threaded gunicorn worker runs out of threads at four tabs, leaving the app
> permanently unresponsive. Third, the AI feature is backwards — it computes
> per-chunk embeddings that nothing consumes, its search endpoint is never
> called by the UI, and clip export downloads `mp4/best`, which resolves to the
> 360p progressive stream, so exported clips are low quality or silent.
>
> Fix all of that. Constrain all writes to `DEFAULT_SAVE_DIR`, add optional
> token auth, switch the server to an async worker, make clip export use
> `bv*+ba/b` with frame-accurate cuts, and rebuild the feature as a queued job
> that extracts the transcript, scores transcript chunks, and turns the best
> ones into timestamped clip regions on the timeline. Delete every UI control
> and API route that has no function.

---

## 2. Full specification

### 2.1 Context

Single-process Flask app, Python 3.12, no database. State lives in three
module-level globals: `tasks` (dict), `listeners` (SSE queues) and one
`queue.Queue`. A single background worker thread consumes that queue and runs
`yt-dlp` and `ffmpeg` as subprocesses.

| File | Role |
|---|---|
| `app.py` | Flask routes, queue worker, downloads, clip export, SSE |
| `ai_helper.py` | transcript extraction, scoring, section building |
| `config.py` | env-driven config dataclass |
| `static/` | single-page UI (index.html, app.js, style.css) |
| `test_app.py` | test suite |

Stack: Flask 3.1.3, gunicorn 23.0.0, gevent 26.9.0, torch 2.11.0,
transformers 5.8.1, yt-dlp 2026.3.17. `ffmpeg` must be on PATH.

Because the queue and SSE listeners are in-process, the app must stay at
**one process with one worker**. Do not add replicas.

### 2.2 The idea of the change

The feature is not "summarise a video". It is **"turn a transcript into cut
points"**. The output that matters is a list of `{start, end, label}` ranges,
because that is what the timeline editor and the ffmpeg exporter consume.

So the pipeline must run in exactly this order, and be visible in the UI as
three steps:

1. **Extract** — fetch the subtitles (manual or auto-generated), parse them into
   timed caption cues, and collapse rolling/karaoke captions where each cue
   repeats the last one.
2. **Analyse** — chunk the transcript into ~60 second blocks and score each
   block for how much of a "highlight" or "tutorial/process" it is, versus
   filler chat.
3. **Sections → clips** — group the strongest adjacent chunks into clip ranges,
   bound their length, merge overlaps, cap the count, and give each one a short
   label. Push those onto the timeline as editable regions, ready to export.

The analysis must run as a **queued job with live progress**, not inside the
request, because it takes a minute and would otherwise sit on a request thread.

### 2.3 Hard requirements

**Security**

- A caller-supplied `save_dir` must resolve inside `DEFAULT_SAVE_DIR` (or a root
  listed in `EXTRA_SAVE_DIRS`). Anything else is HTTP 400. Guard against prefix
  tricks: `/data/downloads-evil` is not inside `/data/downloads`.
- Add optional authentication: when `APP_TOKEN` is set, every page and
  `/api/*` route requires a signed-in session (cookie from `/login`) or an
  `X-App-Token` header. When it is empty, behaviour is unchanged (open, for
  loopback). Leave `GET /api/health` open for platform probes.
- Validate any caller-supplied yt-dlp format expression against a safe charset;
  reject shell metacharacters.
- Build the UI with `textContent` and DOM nodes. Remote metadata (format ids,
  `format_note`, video titles) must never be interpolated into `innerHTML`.
- The container must not run as root.

**Keeping it responsive**

- The server must not use `--worker-class gthread`. An SSE stream per open tab
  exhausts a small thread pool and wedges the whole app. Use an async worker
  (gevent) so an idle stream costs nothing.
- Use a real request timeout instead of `--timeout 0`.
- Guard `tasks` and `listeners` with a lock and snapshot before iterating.
- Cap retained task history (`MAX_TASK_HISTORY`) so memory does not grow
  without bound.

**Clip and download quality**

- Downloads and the clip source must use a merged spec such as `bv*+ba/b`, not
  a bare `best` or `mp4`.
- A bare format id chosen from the quality dropdown must be merged with an audio
  stream (`137` → `137+ba/b`), otherwise the user gets a silent file.
- Clipping must offer a precise mode that re-encodes so the first frame lands
  on the requested timestamp (transcript timings are only useful if they are
  honoured), and a fast mode that stream-copies for speed.
- Never call `wait()` on a process with undrained pipes. Use `communicate()`,
  or redirect to `DEVNULL`, and surface the tail of stderr in the error.
- Cleanup after a failed job must delete only the files that job created, not
  every `.part` in the directory.

**AI pipeline**

- Attempt English subtitles first, then fall back to any available language, so
  non-English videos still produce cuts.
- Load models under a lock, and free them after a configurable idle period
  instead of unloading them after every request.
- Cache the analysis per video with a TTL, written atomically, and serialise
  concurrent analyses of the same video so two browsers do not run the models
  twice.
- Report progress (percent + a short message) so the UI can show which of the
  three steps is running.
- A missing transcript must surface as a clear task error, not a 500.

### 2.4 Remove, do not keep

Delete anything that has no function:

- **Backend**: the `/api/ai/search` route, `query_transcript()`, the embedding
  model and every per-chunk embedding. No UI code ever called the search
  endpoint and the embeddings were stripped from the response before it was sent,
  so the app paid for a model load and a forward pass per chunk for nothing.
- **UI controls never read by any JS**: cookie-file input, download speed limit,
  the global "search videos" box, and the clips search box.
- **UI that pretends to work**: export presets (Short/Reels/TikTok/Podcast) whose
  handler only set a status string, and format pills whose `data-fmt` was never
  read.
- **Static fake readouts**: a hardcoded `01:23:45 / 02:45:30` clock and a
  `0 GB / 0 GB` storage figure.
- **Docs that lie**: instructions in `DEPLOYMENT.md` telling the reader to edit
  `render.yaml`, `railway.json`, `koyeb.yaml` and `vercel.json` — none of those
  files exist.
- **Dead code**: the title-extraction block in the download loop whose entire
  body is `pass`.

Keep bookmarks. They are only timeline markers, but they work and users rely on
them while scrubbing.

### 2.5 Constraints

- One process, one worker. Do not introduce a database, Redis, or Celery.
- Do not add heavy new dependencies. `gevent` is justified because it fixes the
  SSE stall; a speech-to-text model is not.
- Keep every existing route path and payload shape unless the change is the fix
  itself, and keep the env-driven config style.
- Python 3.12; the pinned versions in `requirements.txt` all resolve on PyPI.

---

## 3. Traps (all of these were verified by running the code)

1. **`-f mp4` does not mean "MP4 video".** `mp4` is in yt-dlp's list of video
   extensions, and its filter is
   `ext == "mp4" and acodec != "none" and vcodec != "none"` — i.e. a *pre-merged*
   MP4. On YouTube that is the legacy 360p progressive stream. When no merged
   MP4 exists the fallback is video-only, which produces **silent** clips. Check
   your spec against `YoutubeDL.build_format_selector`, not by intuition.
2. **A bare format id downloads no audio.** `info['formats']` filtered to
   `vcodec != "none" and acodec == "none"` yields video-only streams. Passing
   one to `-f` gives a silent file. Merge `+ba/b` onto it.
3. **gthread + SSE is a hard limit, not a slow-down.** With `--threads 4` the
   app stops answering entirely at the fourth concurrent stream. Verify by
   holding N sockets open and then requesting an endpoint.
4. **`Popen(..., stdout=PIPE, stderr=PIPE)` followed by `wait()` deadlocks** as
   soon as the child writes more than the ~64 KiB pipe buffer. ffmpeg does this
   readily on a problematic source.
5. **yt-dlp buffers progress when piped.** Without `--newline`, progress lines
   are `\r`-joined and `readline()` returns them in one lump, so the progress
   bar jumps instead of moving.
6. **`CookieJar` is falsy when empty** — `if jar:` skips installing
   `HTTPCookieProcessor` in test helpers that build an opener. Use
   `if jar is not None:`.
7. **A frozen dataclass cannot have a defaulted field before a required one.**
   Order fields so every defaulted field comes last.
8. **`use_hf_inference_api` and local models follow different code paths.**
   Whichever you mock in tests, make sure the other path is still covered;
   mocking `APP_CONFIG` with a `MagicMock` breaks numeric comparisons
   (`ai_importance_threshold`) — use a real config object.

---

## 4. Definition of done

- [ ] `POST /api/enqueue` with a `save_dir` outside the configured roots returns
      HTTP 400 and creates nothing.
- [ ] With `APP_TOKEN` set: `/api/*` returns 401 without a session, `/` redirects
      to `/login`, a correct token grants a session, `X-App-Token` works for
      scripts. With it unset: everything is open.
- [ ] 20 concurrent SSE clients held open; `GET /api/queue` still answers.
- [ ] A clip export whose ffmpeg writes 200 KB to stderr completes in seconds
      instead of hanging.
- [ ] Choosing a specific resolution downloads that video **with audio**.
- [ ] Clip export produces files named from the video title and region label,
      one per enabled region, at the requested timestamps.
- [ ] Failing one job does not delete another job's `.part` file, and leaves no
      scratch directories behind.
- [ ] The AI flow: enqueue → progress updates → completed → `GET
      /api/ai/result/<task_id>` returns chunks with scores and sections with
      `{start, end, label}`; sections land on the timeline as regions.
- [ ] A video with no captions produces a readable task error, not a 500.
- [ ] No UI control that does nothing; no reference in the docs to files that
      do not exist.
- [ ] `python -m pytest test_app.py -q` passes, including end-to-end download
      and clip jobs run against offline `yt-dlp`/`ffmpeg` stubs.

Suggested verification stubs live in `tests/fakebin/`: a fake `yt-dlp` that
emits progress lines and writes a file, and a fake `ffmpeg` that can be made
chatty or made to fail via its output filename.

---

## 5. Out of scope (say so rather than half-building them)

- Speech-to-text fallback for videos with no captions (needs Whisper; heavy).
- Vertical 9:16 reframing for Shorts/Reels/TikTok. The preset buttons that
  implied this were removed.
- Multi-user accounts, per-user quotas, or any persistent storage beyond the
  filesystem.
- Transcoding presets, thumbnail pickers, or waveform analysis of the audio
  track (the timeline background is transcript density, not real audio data).
