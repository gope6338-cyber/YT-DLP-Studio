"""YT-DLP Studio -- Flask application.

Layout
------
* one background worker consumes a single FIFO queue (downloads, clip exports
  and AI analyses), so heavy jobs never overlap and compete for RAM;
* task state is pushed to browsers over server sent events;
* every job is a subprocess whose process group can be killed on cancel.

Security model
--------------
Running with no ``APP_TOKEN`` keeps the original behaviour: open, and by default
bound to loopback -- fine for a personal tool on a laptop. Set ``APP_TOKEN`` and
the app requires a login session before serving anything, and every download is
constrained to ``DEFAULT_SAVE_DIR`` (plus anything in ``EXTRA_SAVE_DIRS``).
"""

import glob
import hmac
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)
from config import (
    FORMAT_AUDIO,
    FORMAT_BEST,
    FORMAT_SAFE_CHARS,
    apply_runtime_environment,
    create_app_config,
    ensure_runtime_dirs,
    resolve_save_dir,
)

app = Flask(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
APP_CONFIG = create_app_config()
DEFAULT_SAVE_DIR = APP_CONFIG.default_save_dir
apply_runtime_environment(APP_CONFIG)
ensure_runtime_dirs(APP_CONFIG)

app.secret_key = APP_CONFIG.secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)

# State (guarded by STATE_LOCK: workers and request threads both touch these)
STATE_LOCK = threading.RLock()
task_queue = queue.Queue()
tasks = {}          # task_id -> task dict
listeners = []      # queue.Queue objects fed by the SSE generator
active_task = None
active_task_lock = threading.Lock()

TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# Never serialised to clients: a live handle or a bulky payload.
PRIVATE_TASK_KEYS = ("process", "result", "files")

# Import local AI helper after runtime env is configured
import ai_helper


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _subprocess_creation_kwargs():
    kwargs = {"stdin": subprocess.DEVNULL}
    if sys.platform == 'win32':
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _run_subprocess(args, **kwargs):
    return subprocess.Popen(args, **kwargs, **_subprocess_creation_kwargs())


def _public_task(task):
    """Copy of a task safe to send to a browser."""
    return {k: v for k, v in task.items() if k not in PRIVATE_TASK_KEYS}


def _utc_now():
    return time.time()


def format_upload_date(date_str):
    if not date_str or len(str(date_str)) != 8:
        return date_str
    try:
        return datetime.strptime(str(date_str), "%Y%m%d").strftime("%b %d, %Y")
    except ValueError:
        return date_str


def clean_filename(name):
    """Strips characters that are illegal in filenames on Windows/macOS/Linux."""
    name = re.sub(r'[\\/*?:"<>|\r\n\t]', "", str(name))
    return name.strip().rstrip('.').strip() or "clip"


def _is_safe_format(fmt):
    return 0 < len(fmt) <= 64 and all(ch in FORMAT_SAFE_CHARS for ch in fmt)


def _format_expression(fmt):
    """Maps a UI selection onto a yt-dlp format spec, keeping the audio track.

    ``/api/info`` only lists video-only streams, so a bare id such as "137"
    would download a silent file. Anything that is not already an expression is
    merged with the best audio stream instead.
    """
    fmt = (fmt or "best").strip()
    if fmt in ("", "best"):
        return FORMAT_BEST
    if fmt == "audio":
        return FORMAT_AUDIO
    if not _is_safe_format(fmt):
        raise ValueError("Unsupported format selector")
    if any(op in fmt for op in ("+", "/", "(", ")", "[", ",")):
        return fmt
    return f"{fmt}+ba/b"


def _clip_temp_dir(task_id):
    return os.path.join(APP_CONFIG.temp_dir, f"temp_clips_{task_id}")


# --------------------------------------------------------------------------- #
# Task registry
# --------------------------------------------------------------------------- #
def notify_listeners(event_type, data):
    with STATE_LOCK:
        snapshot = list(listeners)
    payload = {"type": event_type, "data": data}
    for listener in snapshot:
        try:
            listener.put(payload)
        except Exception:
            pass


def update_task(task_id, **kwargs):
    with STATE_LOCK:
        task = tasks.get(task_id)
        if task is None:
            return
        task.update(kwargs)
        copy = _public_task(task)
    notify_listeners("task_update", copy)


def add_task(task):
    with STATE_LOCK:
        tasks[task["id"]] = task
        copy = _public_task(task)
    notify_listeners("task_update", copy)


def snapshot_tasks():
    with STATE_LOCK:
        return [_public_task(t) for t in tasks.values()]


def trim_history():
    """Keeps the in-memory task list bounded (MAX_TASK_HISTORY)."""
    limit = APP_CONFIG.max_task_history
    with STATE_LOCK:
        if limit <= 0 or len(tasks) <= limit:
            return
        finished = [t for t in tasks.values() if t.get("status") in TERMINAL_STATUSES]
        finished.sort(key=lambda t: t.get("created_at", 0))
        for task in finished[: len(tasks) - limit]:
            tasks.pop(task["id"], None)
            notify_listeners("task_update", {"id": task["id"], "status": "removed"})


def kill_process_tree(pid):
    """Terminates a subprocess tree on the current platform."""
    try:
        if sys.platform == 'win32':
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception as e:
        print(f"Error killing process {pid}: {e}")


# --------------------------------------------------------------------------- #
# yt-dlp output parsing
# --------------------------------------------------------------------------- #
_DESTINATION_RE = re.compile(r'Destination:\s+"?(.+?)"?\s*$')
_MERGE_RE = re.compile(r'Merging formats into\s+"(.+)"')


def parse_ytdlp_progress(line):
    """Pulls (percent, speed, eta) out of a yt-dlp progress line."""
    percent_match = re.search(r'(\d+(?:\.\d+)?)%', line)
    speed_match = re.search(r'\bat\s+([^\s/]+/s)\b', line)
    eta_match = re.search(r'ETA\s+(\S+)', line)

    percent = float(percent_match.group(1)) if percent_match else None
    speed = speed_match.group(1) if speed_match else None
    eta = eta_match.group(1) if eta_match else None
    return percent, speed, eta


def note_output_file(line, files):
    """Records files yt-dlp says it created, so cleanup can be scoped to them."""
    match = _MERGE_RE.search(line) or _DESTINATION_RE.search(line)
    if match:
        path = match.group(1).strip()
        if path and path not in files:
            files.append(path)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it."""


def _check_cancelled(task_id):
    with STATE_LOCK:
        status = tasks.get(task_id, {}).get("status")
    if status == "cancelled":
        raise JobCancelled()


def _drain_progress(process, task_id, files, scale=1.0, offset=0.0, phase=None):
    """Reads yt-dlp output, forwarding progress and recording created files."""
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        note_output_file(line, files)
        if "[download]" not in line:
            continue
        pct, spd, eta = parse_ytdlp_progress(line)
        updates = {}
        if pct is not None:
            updates["progress"] = round(offset + pct * scale, 1)
        if spd is not None:
            updates["speed"] = spd
        if eta is not None:
            updates["eta"] = eta
        if updates:
            update_task(task_id, **updates)
        _check_cancelled(task_id)


def run_download_process(task):
    task_id = task['id']
    url = task['url']
    save_dir = task['save_dir']
    fmt = task.get('format') or 'best'

    os.makedirs(save_dir, exist_ok=True)

    expression = _format_expression(fmt)

    out_tmpl = os.path.join(save_dir, '%(title)s.%(ext)s')
    args = [
        APP_CONFIG.yt_dlp_bin,
        "--no-playlist",
        "--newline",          # progress lines are \r-joined when piped without this
        "-f", expression,
        "-o", out_tmpl,
    ]
    if fmt == 'audio' or expression == FORMAT_AUDIO:
        args += ["-x", "--audio-format", "mp3"]
    args.append(url)

    update_task(task_id, status='downloading', progress=0.0)

    files = []
    try:
        process = _run_subprocess(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        update_task(task_id, process=process, files=files)

        _drain_progress(process, task_id, files)
        process.wait()

        if process.returncode == 0:
            return True, None
        _check_cancelled(task_id)
        return False, f"yt-dlp exited with code {process.returncode}"
    except JobCancelled:
        return False, "Cancelled"
    except Exception as e:
        return False, str(e)


def _ffmpeg_clip_args(source, output, start, end, mode):
    """Cuts [start, end) out of source.

    "precise" re-encodes so the first frame lands exactly on the requested
    timestamp -- important when the cuts came from transcript timings.
    "fast" stream-copies, which snaps the in-point back to the nearest keyframe.
    """
    base = [
        APP_CONFIG.ffmpeg_bin,
        "-nostdin",
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", source,
    ]
    if mode == "fast":
        return base + ["-c", "copy", "-avoid_negative_ts", "make_zero",
                       "-movflags", "+faststart", output]
    return base + [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", output,
    ]


def _run_ffmpeg(args):
    """Runs ffmpeg to completion, draining its pipes (never wait() on pipes)."""
    process = _run_subprocess(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    _, stderr = process.communicate()
    return process.returncode, (stderr or "")


def run_clip_export(task):
    task_id = task['id']
    url = task['url']
    save_dir = task['save_dir']
    regions = task['regions']
    mode = task.get('cut_mode') or APP_CONFIG.clip_cut_mode
    if mode not in ("precise", "fast"):
        mode = "precise"

    enabled = [r for r in regions if r.get('enabled', True)]
    if not enabled:
        return False, "No enabled clip regions to export"

    os.makedirs(save_dir, exist_ok=True)

    update_task(task_id, status='downloading', progress=0.0, eta='downloading source')

    # Download the best available source into a scratch directory. The template
    # keeps %(ext)s so yt-dlp can merge streams without a forced container.
    temp_dir = _clip_temp_dir(task_id)
    os.makedirs(temp_dir, exist_ok=True)
    template = os.path.join(temp_dir, f"source_{task_id}.%(ext)s")

    try:
        dl_args = [
            APP_CONFIG.yt_dlp_bin,
            "--no-playlist",
            "--newline",
            "-f", FORMAT_BEST,
            "--merge-output-format", "mp4",
            "-o", template,
            url,
        ]
        files = []
        process = _run_subprocess(
            dl_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        update_task(task_id, process=process, files=files)

        # Reserve the last 15% for cutting.
        _drain_progress(process, task_id, files, scale=0.85)
        process.wait()

        if process.returncode != 0:
            _check_cancelled(task_id)
            return False, f"Failed to download the source video (exit code {process.returncode})"

        sources = [p for p in glob.glob(os.path.join(temp_dir, f"source_{task_id}.*"))
                   if not p.endswith(".part")]
        if not sources:
            return False, "The downloaded source file was not found"

        source_path = sources[0]

        video_title = task.get('title') or "clip"
        if not video_title or video_title.startswith("Export Clips"):
            video_title = _probe_title(url) or "clip"
        video_title = clean_filename(video_title)

        update_task(task_id, status='clipping', progress=85.0, speed="N/A", eta='cutting clips')

        total = len(enabled)
        for index, region in enumerate(enabled):
            _check_cancelled(task_id)

            start = float(region['start'])
            end = float(region['end'])
            if end <= start:
                return False, f"Clip '{region.get('label')}' ends before it starts"

            label = clean_filename(region.get('label') or f"segment_{index + 1}")
            output_path = os.path.join(save_dir, f"{video_title} - {label}.mp4")

            returncode, stderr = _run_ffmpeg(
                _ffmpeg_clip_args(source_path, output_path, start, end, mode)
            )
            if returncode != 0:
                _check_cancelled(task_id)
                detail = " ".join(stderr.strip().split()[-40:]) or "no ffmpeg output"
                return False, f"FFmpeg failed on '{label}': {detail}"

            update_task(
                task_id,
                progress=round(85.0 + (15.0 * (index + 1) / total), 1),
                eta=f"clip {index + 1}/{total}",
            )

        return True, None
    except JobCancelled:
        return False, "Cancelled"
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _probe_title(url):
    """Fallback title lookup; the UI normally supplies one."""
    try:
        completed = subprocess.run(
            [APP_CONFIG.yt_dlp_bin, "--get-title", url],
            capture_output=True, text=True, timeout=45,
            **_subprocess_creation_kwargs(),
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except Exception:
        pass
    return None


def run_analysis(task):
    """Transcript -> analyse -> clip ranges (see ai_helper)."""
    task_id = task['id']

    update_task(task_id, status='analyzing', progress=0.0, speed='N/A', eta='queued...')

    def progress_cb(percent, message):
        _check_cancelled(task_id)
        update_task(task_id, progress=percent, eta=message)

    try:
        result = ai_helper.analyze_video(task['url'], progress_cb=progress_cb)
    except JobCancelled:
        return False, "Cancelled"
    except Exception as e:
        return False, str(e)

    if result.get("error"):
        return False, result["error"]
    if not result.get("sections"):
        return False, "No clip-worthy segments were found in this transcript."

    update_task(
        task_id,
        result=result,
        sections=result["sections"],
        eta=f"{len(result['sections'])} clips suggested",
    )
    return True, None


def cleanup_partial_downloads(task):
    """Removes only the files this task created (never the whole directory)."""
    files = list(task.get('files') or [])
    for path in files:
        for candidate in (path, path + ".part", path + ".ytdl"):
            try:
                if os.path.isfile(candidate):
                    os.remove(candidate)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def queue_worker():
    global active_task
    while True:
        task = task_queue.get()
        if task is None:
            break

        task_id = task['id']

        with active_task_lock:
            active_task = task

        try:
            if tasks.get(task_id, {}).get('status') == 'cancelled':
                continue

            update_task(task_id, status=_status_for(task), progress=0.0)

            success = False
            err_msg = None
            job_type = task['type']

            if job_type == 'download':
                success, err_msg = run_download_process(task)
            elif job_type == 'clip':
                success, err_msg = run_clip_export(task)
            elif job_type == 'analysis':
                success, err_msg = run_analysis(task)

            with STATE_LOCK:
                current_status = tasks.get(task_id, {}).get('status')

            if success:
                update_task(task_id, status='completed', progress=100.0,
                            speed="N/A", eta='done')
            elif current_status != 'cancelled':
                update_task(task_id, status='failed', error=err_msg or "Unknown error",
                            speed="N/A", eta='failed')
                if job_type != 'analysis':
                    cleanup_partial_downloads(task)
        finally:
            with active_task_lock:
                active_task = None
            try:
                trim_history()
            except Exception:
                pass
            try:
                task_queue.task_done()
            except Exception:
                pass


def _status_for(task):
    return {'download': 'downloading', 'clip': 'downloading', 'analysis': 'analyzing'}.get(
        task['type'], 'downloading')


# Start background worker
threading.Thread(target=queue_worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _auth_enabled():
    return bool(APP_CONFIG.app_token)


def _token_matches(supplied):
    return bool(supplied) and hmac.compare_digest(str(supplied), APP_CONFIG.app_token)


# Health probes usually cannot authenticate, and the endpoint only exposes
# aggregate task counts.
UNAUTHENTICATED_PATHS = ("/api/health",)


def _authorized():
    if not _auth_enabled():
        return True
    if request.path in UNAUTHENTICATED_PATHS:
        return True
    if session.get("authenticated") is True:
        return True
    supplied = (request.headers.get("X-App-Token")
                or request.args.get("token")
                or request.form.get("token"))
    return _token_matches(supplied)


@app.before_request
def _enforce_auth():
    if _authorized():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required"}), 401
    if request.path not in ("/login", "/logout"):
        return redirect(url_for("login"))
    return None


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT-DLP Studio &mdash; Sign in</title>
<style>
  :root { --bg:#0d1017; --panel:#151a24; --border:#232a3a; --text:#e6edf7;
          --muted:#8b97ad; --accent:#4fc3f7; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--text);
         font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  .card { width:340px; padding:28px; background:var(--panel);
          border:1px solid var(--border); border-radius:12px; }
  h1 { margin:0 0 4px; font-size:17px; font-weight:600; }
  p { margin:0 0 20px; font-size:13px; color:var(--muted); line-height:1.5; }
  input { width:100%; padding:10px 12px; margin-bottom:12px; border-radius:8px;
          border:1px solid var(--border); background:#0f131c; color:var(--text);
          font-size:14px; }
  input:focus { outline:none; border-color:var(--accent); }
  button { width:100%; padding:10px; border:0; border-radius:8px; cursor:pointer;
           background:var(--accent); color:#06121c; font-size:14px; font-weight:600; }
  .err { color:#ef5350; font-size:12px; margin:-6px 0 12px; }
</style></head><body>
<form class="card" method="post" action="/login">
  <h1>YT-DLP Studio</h1>
  <p>This instance is protected. Enter the <code>APP_TOKEN</code> the server was
     started with.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="token" placeholder="Access token" autofocus required>
  <button type="submit">Sign in</button>
</form></body></html>
"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _auth_enabled():
        return redirect(url_for('index'))
    if request.method == 'POST':
        if _token_matches(request.form.get('token')):
            session['authenticated'] = True
            session.permanent = True
            return redirect(url_for('index'))
        from flask import render_template_string
        return render_template_string(LOGIN_PAGE, error="Incorrect token."), 401

    from flask import render_template_string
    return render_template_string(LOGIN_PAGE, error=None)


@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def _render_index_with_config():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    boot_config = json.dumps({
        "defaultSaveDir": DEFAULT_SAVE_DIR,
        "authEnabled": _auth_enabled(),
        "clipCutMode": APP_CONFIG.clip_cut_mode,
        "aiMaxSections": APP_CONFIG.ai_max_sections,
    })
    return html.replace('"__APP_BOOT_CONFIG__"', boot_config, 1)


@app.route('/')
def index():
    return Response(_render_index_with_config(), mimetype="text/html")


@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def _json_body():
    return request.get_json(silent=True) or {}


@app.route('/api/info', methods=['POST'])
def get_info():
    data = _json_body()
    url = (data.get('url') or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen_resolutions = set()
        for f in info.get('formats', []):
            if f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                res = f.get('resolution') or f"{f.get('height')}p"
                if res not in seen_resolutions and f.get('height'):
                    seen_resolutions.add(res)
                    formats.append({
                        'id': f['format_id'],
                        'resolution': res,
                        'ext': f.get('ext'),
                        'fps': f.get('fps'),
                        'note': f.get('format_note') or '',
                    })

        formats.sort(key=lambda x: int(re.sub(r'\D', '', x['resolution']) or 0), reverse=True)

        filesize = info.get('filesize') or info.get('filesize_approx')
        if filesize:
            gb = filesize / (1024 * 1024 * 1024)
            filesize_str = f"{gb:.2f} GB" if gb >= 1.0 else f"{filesize / (1024 * 1024):.2f} MB"
        else:
            filesize_str = "N/A"

        duration = info.get('duration')

        return jsonify({
            'title': info.get('title'),
            'duration': duration,
            'duration_string': _format_duration(duration),
            'thumbnail': info.get('thumbnail'),
            'formats': formats,
            'default_save_dir': DEFAULT_SAVE_DIR,
            'uploader': info.get('uploader') or info.get('channel') or 'N/A',
            'upload_date': format_upload_date(info.get('upload_date')),
            'resolution': f"{info.get('height')}p" if info.get('height') else "N/A",
            'filesize': filesize_str,
            'vcodec': info.get('vcodec') or 'N/A',
            'acodec': info.get('acodec') or 'N/A',
            'fps': info.get('fps') or 'N/A',
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _format_duration(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "N/A"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@app.route('/api/enqueue', methods=['POST'])
def enqueue_download():
    data = _json_body()
    url = (data.get('url') or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    save_dir = resolve_save_dir(data.get('save_dir'), APP_CONFIG)
    if save_dir is None:
        return jsonify({
            "error": "save_dir must be inside the configured download directory",
            "allowed": list(APP_CONFIG.allowed_save_dirs),
        }), 400

    try:
        fmt = _format_expression(data.get('format'))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    task = _new_task(
        prefix="dl",
        title=clean_filename(data.get('title') or "YouTube Video"),
        url=url,
        type='download',
        save_dir=save_dir,
        format=fmt,
    )
    add_task(task)
    task_queue.put(task)
    return jsonify({"success": True, "task_id": task["id"]})


@app.route('/api/clip', methods=['POST'])
def enqueue_clip():
    data = _json_body()
    url = (data.get('url') or "").strip()
    regions = data.get('regions')
    if not url or not regions:
        return jsonify({"error": "URL and regions are required"}), 400

    save_dir = resolve_save_dir(data.get('save_dir'), APP_CONFIG)
    if save_dir is None:
        return jsonify({
            "error": "save_dir must be inside the configured download directory",
            "allowed": list(APP_CONFIG.allowed_save_dirs),
        }), 400

    clean_regions = []
    for index, region in enumerate(regions):
        try:
            start = float(region['start'])
            end = float(region['end'])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": f"Region {index + 1} has invalid times"}), 400
        if end <= start or start < 0:
            return jsonify({"error": f"Region {index + 1} has an invalid range"}), 400
        clean_regions.append({
            'start': round(start, 3),
            'end': round(end, 3),
            'label': clean_filename(region.get('label') or f"segment_{index + 1}"),
            'enabled': bool(region.get('enabled', True)),
        })

    cut_mode = (data.get('cut_mode') or APP_CONFIG.clip_cut_mode).lower()
    if cut_mode not in ("precise", "fast"):
        cut_mode = APP_CONFIG.clip_cut_mode

    task = _new_task(
        prefix="clip",
        title=data.get('title') or "YouTube Clip",
        url=url,
        type='clip',
        save_dir=save_dir,
        regions=clean_regions,
        cut_mode=cut_mode,
        format=FORMAT_BEST,
    )
    add_task(task)
    task_queue.put(task)
    return jsonify({"success": True, "task_id": task["id"]})


def _new_task(prefix, **fields):
    task = {
        'id': f"{prefix}_{int(time.time() * 1000)}_{os.urandom(2).hex()}",
        'title': "YouTube Video",
        'url': "",
        'type': prefix,
        'status': 'pending',
        'progress': 0.0,
        'speed': 'N/A',
        'eta': 'pending...',
        'error': None,
        'created_at': _utc_now(),
        'process': None,
        'files': [],
    }
    task.update(fields)
    return task


@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    """Queues the transcript -> analyse -> clip-ranges pipeline."""
    data = _json_body()
    url = (data.get('url') or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    task = _new_task(
        prefix="ai",
        title=f"AI cuts: {clean_filename(data.get('title') or 'video')}",
        url=url,
        type='analysis',
        save_dir=DEFAULT_SAVE_DIR,
    )
    add_task(task)
    task_queue.put(task)
    return jsonify({"success": True, "task_id": task["id"]})


@app.route('/api/ai/result/<task_id>', methods=['GET'])
def ai_result(task_id):
    with STATE_LOCK:
        task = tasks.get(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        result = task.get('result')
        status = task.get('status')
        error = task.get('error')
    if result is None:
        return jsonify({"error": error or "Analysis is not finished yet", "status": status}), 202
    return jsonify(result)


@app.route('/api/cancel', methods=['POST'])
def cancel_task():
    data = _json_body()
    task_id = data.get('task_id')

    with STATE_LOCK:
        task = tasks.get(task_id)
    if task is None:
        return jsonify({"error": "Invalid task ID"}), 400

    if task['status'] in TERMINAL_STATUSES:
        return jsonify({"success": True, "message": "Task already finished"})

    update_task(task_id, status='cancelled', speed="N/A", eta='cancelled')

    with active_task_lock:
        if active_task and active_task['id'] == task_id:
            process = active_task.get('process')
            if process is not None and process.poll() is None:
                print(f"Cancelling active process for task {task_id} (PID: {process.pid})")
                kill_process_tree(process.pid)

    return jsonify({"success": True})


@app.route('/api/queue', methods=['GET'])
def get_queue():
    return jsonify(snapshot_tasks())


@app.route('/api/remove', methods=['POST'])
def remove_task():
    data = _json_body()
    task_id = data.get('task_id')
    if not task_id:
        return jsonify({"error": "Task ID is required"}), 400

    with STATE_LOCK:
        task = tasks.get(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        if task['status'] not in TERMINAL_STATUSES:
            return jsonify({"error": "Cannot remove a task that is still running"}), 400
        tasks.pop(task_id, None)

    notify_listeners("task_update", {"id": task_id, "status": "removed"})
    return jsonify({"success": True})


@app.route('/api/health', methods=['GET'])
def health():
    with STATE_LOCK:
        counts = {}
        for task in tasks.values():
            counts[task['status']] = counts.get(task['status'], 0) + 1
    return jsonify({"status": "ok", "tasks": counts})


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def sse_stream():
    listener = queue.Queue()
    with STATE_LOCK:
        listeners.append(listener)
    try:
        yield f"data: {json.dumps({'type': 'init', 'data': snapshot_tasks()})}\n\n"
        while True:
            try:
                message = listener.get(timeout=15.0)
                yield f"data: {json.dumps(message)}\n\n"
            except queue.Empty:
                yield 'data: {"type": "ping"}\n\n'
    finally:
        with STATE_LOCK:
            if listener in listeners:
                listeners.remove(listener)


@app.route('/api/stream')
def get_stream():
    return Response(
        sse_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == '__main__':
    os.makedirs(STATIC_DIR, exist_ok=True)
    app.run(host=APP_CONFIG.host, port=APP_CONFIG.port, debug=False)
