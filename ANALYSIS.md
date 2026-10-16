# YT-DLP Studio — Codebase Analysis

**Date:** 2026-10-16
**Scope:** full repository at commit `47b9f4a` (branch `arena/7ef6b894-yt-dlp-studio`)
**Size:** 17 files, ~5,900 lines (~2,900 Python, ~2,400 frontend, ~600 config/docs)

| File | Lines | Role |
|---|---|---|
| `app.py` | 655 | Flask app, job queue, yt-dlp/ffmpeg orchestration, SSE, REST API |
| `ai_helper.py` | 499 | Transcript download/parse, embeddings, zero-shot scoring, summarisation |
| `static/app.js` | 1,090 | Timeline editor, YouTube IFrame player, queue UI |
| `static/index.html` | 468 | Single-page UI |
| `static/style.css` | 862 | Styling (DaVinci-Resolve-inspired dark theme) |
| `test_app.py` | 317 | 16 unit tests |
| `config.py` | 90 | Env-driven config dataclass |

---

## 1. What this thing is

A single-process Flask web studio for YouTube/yt-dlp-host content. Three capabilities:

1. **Download** — pick a URL, choose quality, download to a server-side directory.
2. **Clip** — scrub a canvas timeline, mark in/out regions, export them via `ffmpeg -c copy`.
3. **AI analyse** — pull subtitles, chunk them into 60 s blocks, embed with MiniLM, score each
   chunk with BART-MNLI zero-shot, group the important ones into "sections", summarise each
   section with BART-CNN, and offer semantic search over the transcript.

Architecture: browser → Flask REST + SSE → one global `queue.Queue` → **one** worker thread →
`yt-dlp`/`ffmpeg` subprocesses. State lives in three module-level globals (`tasks`, `listeners`,
`active_task`). There is no database and no persistence.

**The architecture is coherent and appropriate for a personal, localhost tool.** The problems
below are what happen when that same code is put on a public host — which is exactly what
`Dockerfile`, `Procfile` and `DEPLOYMENT.md` are preparing it for (`HOST=0.0.0.0`, no auth).

---

## 2. How this analysis was done

Static reading was backed by live execution, so nothing below is speculation:

- Installed the pinned deps (`Flask==3.1.3`, `yt-dlp==2026.3.17`, `gunicorn==23.0.0`) in a venv
  and confirmed **all five version pins in `requirements.txt` exist on PyPI** (torch 2.11.0,
  transformers 5.8.1, yt-dlp 2026.3.17, Flask 3.1.3, gunicorn 23.0.0) — all HTTP 200.
- Ran the suite: **16/16 pass in 0.15 s.**
- Booted `gunicorn` with the shipped `start.sh` flags and probed the live endpoints.
- Verified format-selection semantics against **yt-dlp's own `build_format_selector`** and read
  its source (`yt_dlp/YoutubeDL.py:2546-2570`) rather than guessing.
- Reproduced the subprocess pipe deadlock with a standalone harness.

---

## 3. Critical — security

### C1. Unauthenticated arbitrary file write to any server path

`app.py:48` and `app.py:476`/`app.py:512`:

```python
def _normalize_save_dir(save_dir):
    return os.path.abspath(os.path.expanduser(save_dir or DEFAULT_SAVE_DIR))
```

The value comes straight from the JSON body and is passed to `os.makedirs(save_dir, exist_ok=True)`
(`app.py:110`) and then to yt-dlp as `-o <dir>/%(title)s.%(ext)s`. There is no containment check
against `DEFAULT_SAVE_DIR`, no allow-list, and no authentication anywhere in the app.

**Verified live:**

```
POST /api/enqueue {"url":"https://example.com/video",
                   "save_dir":"/home/user/WT-ANALYSIS-EVIDENCE/escape-test"}
  -> 200 {'success': True, 'task_id': 'dl_1792135817516'}
  -> directory created on server? True
```

Combined with the fact that any URL is accepted (yt-dlp supports 1,000+ extractors, including
`file:`, and arbitrary direct URLs), an unauthenticated caller can **create directories and fetch
remote content to any path the process can write**. The `Dockerfile` binds `0.0.0.0` and never
drops root. This is the single most serious issue in the repo.

**Fix:** resolve the requested path and reject anything that is not `DEFAULT_SAVE_DIR` or a
subdirectory of it (`os.path.commonpath`), and put auth (or at minimum a loopback bind + reverse
proxy) in front of the app.

### C2. No authentication or authorisation on any endpoint

Every route — `/api/info`, `/api/enqueue`, `/api/clip`, `/api/cancel`, `/api/remove`,
`/api/queue`, `/api/stream`, `/api/ai/*` — is open. `/api/remove` and `/api/cancel` let any
caller destroy or abort another user's jobs; `/api/stream` leaks every user's task list, titles
and URLs. There is no session, token, or basic auth.

*Mitigating factor worth knowing:* because no CORS headers are set, a cross-origin browser
`POST` with `Content-Type: application/json` triggers a preflight that Flask rejects. So the
classic drive-by CSRF is blocked by default. This is defence-by-accident, not design — it
evaporates the moment someone adds `flask-cors` or puts a permissive proxy in front.

### C3. Stored XSS via remote metadata

`static/app.js:456` interpolates yt-dlp output into `innerHTML`:

```js
customFormatSelect.innerHTML += `<option value="${f.id}">${f.resolution} (${f.ext} / ${f.fps}fps) - ${f.note}</option>`;
```

`format_id` and `format_note` come from the remote site. `static/app.js:936` and `:968` take
`activeVideoTitle.innerText` (set from the remote `title` at `:441`) and POST it as `title`, which
`static/app.js:198`/`:254` then render with `innerHTML` in the queue card.

On YouTube these fields are locally generated and safe. On other yt-dlp hosts they are
attacker-controlled. It is a real stored/reflected XSS chain; severity is **medium** because it
needs the victim to load an attacker-chosen URL. The rest of the metadata panel correctly uses
`innerText` — the fix is to build these two nodes with `textContent`/`<option>` DOM APIs too.

---

## 4. High — the app stops working

### H1. The shipped server config deadlocks at four open browser tabs

`start.sh` runs `gunicorn --worker-class gthread --threads "${GUNICORN_THREADS:-4}"`, and
`static/app.js:159` opens an SSE connection at module load:

```js
const sse = new EventSource('/api/stream');
```

Each SSE client permanently occupies one gthread worker thread. **Verified live** against
gunicorn with the shipped flags:

```
Baseline (0 SSE clients):                200 in 0.01s
With 1 concurrent SSE client(s):         200 in 0.01s
With 2 concurrent SSE client(s):         200 in 0.01s
With 3 concurrent SSE client(s):         200 in 0.01s
With 4 concurrent SSE client(s):         FAILED after 6.01s: TimeoutError
With 5 concurrent SSE client(s):         FAILED after 6.01s: TimeoutError
```

The fourth tab renders the whole application permanently unresponsive — and because
`start.sh` sets `--timeout 0` (`GUNICORN_TIMEOUT=0`), the worker never times out and recovers.
Two people using it at once on a phone and a laptop is enough to trigger this.

**Fix:** switch the worker class to `gevent` (or `eventlet`) with the matching monkey-patching,
or move SSE onto an ASGI server. `--timeout 0` should also go; a bounded timeout plus heartbeat
lets stuck threads be reaped.

### H2. Clip export silently produces low-quality or silent files

`app.py:200`:

```python
dl_args = [APP_CONFIG.yt_dlp_bin, "--no-playlist", "-f", "mp4/best", "-o", temp_full_path, url]
```

The comment above it says *"we first download the full best quality video"*. It does not. Reading
yt-dlp's source (`YoutubeDL.py:2562-2566`), because `mp4` is in `_format_selection_exts['video']`:

```python
filter_f = lambda f: f.get('ext') == 'mp4' and f.get('acodec') != 'none' and f.get('vcodec') != 'none'
seperate_fallback = lambda f: f.get('ext') == 'mp4' and f.get('vcodec') != 'none'
```

`mp4` means **"a pre-merged MP4 with both streams"** — on YouTube that is the legacy progressive
stream, i.e. **360p** (format 18). If no merged MP4 exists, the fallback yields a **video-only**
stream and every clip comes out **silent**. Verified by driving yt-dlp's own selector:

```
-f 137        -> ['137']  | has audio: False      <-- what the quality dropdown would pick
-f best       -> ['18']   | has audio: True
-f bv*+ba/b   -> ['18']   | has audio: True
-f mp4/best   -> ['18']   | has audio: True       <-- what clip export uses
```

**Fix:** use `bv*+ba/b` (as the download path correctly does at `app.py:120`), or
`bestvideo[height<=1080]+bestaudio/best`, and drop the hardcoded `.mp4` output extension in
favour of `%(ext)s` plus `--merge-output-format mp4`.

### H3. A verbose ffmpeg run hangs the only worker thread forever

`app.py:274-280` spawns ffmpeg with **both** pipes captured and then calls `p_ff.wait()` without
ever draining them:

```python
p_ff = _run_subprocess(ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
update_task(task_id, process=p_ff)
p_ff.wait()
```

Once ffmpeg writes more than the ~64 KiB pipe buffer to stderr, it blocks on write, `wait()`
blocks on exit, and the process deadlocks. **Reproduced:**

```
DEADLOCK: p.wait() never returned -> clip task hangs forever,
holding the single queue worker. Requires >64KiB of ffmpeg stderr.
```

This is latent in the `-c copy` fast path (little stderr) but triggers readily with
`Non-monotonous DTS` spam, a corrupt source, or any re-encode preset. Because there is exactly
one worker thread, one hang kills all queueing permanently.

**Fix:** `stdout, stderr = p_ff.communicate()` (or redirect to `DEVNULL`/a log file). Never
`wait()` on a process with undispatched pipes.

### H4. Cancelling or failing one download deletes other tasks' in-progress files

`app.py:363`:

```python
def cleanup_partial_downloads(task):
    save_dir = task['save_dir']
    for f in os.listdir(save_dir):
        if f.endswith('.part') or f.endswith('.ytdl'):
            os.remove(os.path.join(save_dir, f))
```

On any failure it deletes **every** `.part`/`.ytdl` file in the directory, including ones
belonging to concurrently running tasks. Fix: track the files the task actually created (from the
yt-dlp `Destination:` log line) and delete only those.

---

## 5. Medium — correctness and resource bugs

**M1. Unbounded memory growth.** `tasks` (`app.py:31`) is only ever pruned by an explicit
`/api/remove` call. Completed and failed tasks accumulate for the life of the process, and every
new SSE client receives the entire history in its `init` frame (`app.py:566`). Add a TTL or cap,
or persist to SQLite.

**M2. The AI models are not thread-safe and thrash.** `get_embeddings_model`, `get_classifier`
and `get_summarizer` (`ai_helper.py:67-90`) lazily populate module globals from request threads
with no lock, so two concurrent `/api/ai/analyze` calls both construct BART-large pipelines
(~1.5 GB each) → OOM. Worse, `unload_models()` (`ai_helper.py:92`) nulls those globals while
another request may still be using them, and it is called after *every* analysis and *every*
search (`ai_helper.py:439`, `:480`) — so each search reloads MiniLM from disk. Guard model
loading with a lock, drop the per-request unload (or make it a timer), and preload at startup.

**M3. `listeners` is mutated from many threads.** `notify_listeners` iterates the global list
(`app.py:60`) with no lock. Because the worker thread and request threads both touch it, a
concurrent `append` during iteration can cause a listener to be silently skipped — a client
misses a task update and shows stale progress until it refreshes. The `listeners.remove(q)` in
the `finally` block can also raise `ValueError`. Use a lock and `discard`-style removal.

**M4. `tasks` dict iterated while mutated.** `/api/queue` and `sse_stream` both do
`for t_id, task in tasks.items()` while the enqueue routes insert new keys. This can raise
`RuntimeError: dictionary changed size during iteration` → HTTP 500. *Not* reproduced under
2,367 concurrent requests in testing, so it is a low-frequency race rather than a certainty, but
it is unguarded. Snapshot with `list(tasks.values())` under a lock.

**M5. The AI transcript cache never expires.** `ai_helper.py:452` writes
`<CACHE_DIR>/<video_id>.json` and `analyze_video` returns it forever on hit (`ai_helper.py:300`).
No TTL, no invalidation, no size cap, and no locking around concurrent writers for the same
video (two simultaneous requests both run the full pipeline and race to write the file).

**M6. Progress parsing is fragile.** `parse_ytdlp_progress` (`app.py:72`) matches
`at\s+(\S+)` and `ETA\s+(\S+)` anywhere in the line, so post-processing noise can be reported as
speed. Harmless but occasionally wrong.

---

## 6. Dead code and unfinished features

**D1. The quality selector does nothing.** `static/app.js:454-457` carefully builds the
"Custom Quality" dropdown from `/api/info` — but `quickDownloadFull()` at `static/app.js:935`
reads `formatPresetSelect.value`, i.e. the *other* select, which only offers `best` and `audio`.
`customFormatSelect` has **zero** read references in the entire codebase. A user who picks
"1080p" gets `best`. The whole `/api/info` format-listing feature is inert.

**D2. Dead UI inputs.** These four inputs are rendered but never read by any JS:
`cookie-file` (`index.html:116`), `max-speed-preset` (`index.html:143`),
`global-search-input` (`index.html:29`), `clips-search-input` (`index.html:305`).
The backend has no rate-limit or cookie support at all, so cookies and speed limits are
advertised-but-absent features.

**D3. Vacuous title extraction.** `app.py:142-158` — a `title_extracted` flag and two regexes
whose only body is `pass`. Leftover scaffolding; delete it.

**D4. Duplicated summarisation block.** The "close section" and "handle last section" blocks in
`analyze_video` (`ai_helper.py:392-436`) are copy-paste of ~18 lines each. Extract a helper.

**D5. Import inside function.** `app.py:386` does `from datetime import datetime` inside
`format_upload_date`, on every call. Move it to module scope.

---

## 7. Documentation drift

`DEPLOYMENT.md` documents four platform config files that **do not exist in the repository**:

- `render.yaml` (Render)
- `railway.json` (Railway)
- `koyeb.yaml` (Koyeb)
- `vercel.json` (Vercel)

The "Platform matrix" and "Vercel note" sections instruct the reader to deploy with them and to
edit `https://replace-with-full-backend.example.com` in `vercel.json`. Following the guide as
written is impossible. Either add the files or delete the sections.

There is also **no `README.md`** at all — no install instructions, no feature overview, no
screenshot. For a repo whose stated purpose is deployment to several PaaS hosts, that is a gap.

Two further notes on the deployment story:
- `DEPLOYMENT.md` correctly warns that free tiers cannot run the local-model AI path, and
  recommends `USE_HF_INFERENCE_API=true`. That flag is wired up properly
  (`config.py:82`, used throughout `ai_helper.py`) — good.
- The `Dockerfile` never drops privileges (`USER`), so the container runs as root while writing
  attacker-influenced paths (see C1).

---

## 8. What is genuinely good

Worth saying explicitly, because this is a better-than-average hobby codebase:

- **Config is clean.** `config.py` is a frozen dataclass with typed env parsing and sane
  fallback logic. `apply_runtime_environment` correctly uses `os.environ.setdefault` **before**
  the lazy `transformers` import in `ai_helper`, so `HF_HOME` actually takes effect.
- **Cross-platform process control.** `start_new_session=True` on POSIX and
  `CREATE_NO_WINDOW` on Windows, with `killpg`/`taskkill` in `kill_process_tree` — this is the
  right way to make cancellation work, and cancellation genuinely works end to end.
- **Model-unloading discipline.** Actually calling `unload_models()` and `gc.collect()` is thoughtful;
  it is only the *placement* that is wrong (see M2).
- **Test suite is meaningful.** 16 tests covering config parsing, subtitle parsing, chunking,
  dedup, filename cleaning, API error contracts, and the HF-API retry path — and they all pass.
  Coverage of `app.py`'s queue/worker logic is the obvious gap.
- **Careful SSE hygiene.** The `init` frame plus 15 s `ping` keep-alive, and the
  `try/finally` listener cleanup, are both correct instincts.
- **`send_from_directory`** is used for static serving, so no path-traversal there.

---

## 9. Prioritised remediation plan

**P0 — do before this is exposed to any network beyond localhost**
1. Contain `save_dir` within `DEFAULT_SAVE_DIR` (C1).
2. Add authentication (C2) — at minimum a shared token, ideally in the reverse proxy.
3. Replace `gthread` with `gevent` and bound `--timeout` (H1).
4. Escape remote metadata in the two `innerHTML` sinks (C3).

**P1 — correctness, will bite in normal use**
5. Fix the clip format spec to `bv*+ba/b` (H2).
6. Replace `p_ff.wait()` with `communicate()` (H3).
7. Scope partial-file cleanup to the task's own files (H4).
8. Either wire up `customFormatSelect` or remove it (D1).

**P2 — robustness and hygiene**
9. Lock model loading; stop unloading per request (M2).
10. Lock `tasks`/`listeners` and snapshot on iteration (M3, M4).
11. Bound `tasks` and the AI cache (M1, M5).
12. Delete the dead code and dead UI (D2–D5).
13. Add the missing `render.yaml`/`railway.json`/`koyeb.yaml`/`vercel.json` or remove those
    sections from `DEPLOYMENT.md`; add a `README.md` (§7).
14. Add tests for the worker/cancellation paths.

---

## 10. Reproducing the findings

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
python -m pytest test_app.py -q          # 16 passed

sh ./start.sh                            # then open 4 browser tabs -> H1
```

The probes used here (`POST /api/enqueue` with an out-of-tree `save_dir`; N concurrent
`EventSource` connections followed by `GET /api/queue`; and a subprocess writing 200 KB to a
captured stderr pipe before `wait()`) are all reproducible from the descriptions in §3–§4 and
were run against `gunicorn 23.0.0` with the exact flags from `start.sh`.
