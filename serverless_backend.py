"""
Serverless backend for Vercel (Flask WSGI app).

Vercel runs every request in an isolated, short-lived function, so the
self-hosted architecture (background queue thread + SSE + yt-dlp/ffmpeg
subprocesses writing to a server folder) cannot work here. This backend
re-shapes the app for serverless:

- Metadata lookup (/api/info) works exactly like the self-hosted app.
- Downloads resolve a direct media URL with yt-dlp, then either stream the
  bytes through the function (progress-friendly) or, for formats that need
  processing (merged best quality, MP3 audio, clip sections), download to
  /tmp with yt-dlp + the imageio-ffmpeg static binary and return the file.
  The browser saves the file locally; the queue/progress UI runs client-side.
- AI endpoints run only when USE_HF_INFERENCE_API=true (no torch/transformers
  in the serverless bundle; they would blow the function size/memory limits).

The queue/SSE endpoints intentionally respond with 501 + guidance: the
frontend detects `serverless: true` in the boot config injected into the
index page and runs its own client-side queue instead of calling them.
"""

import os
import re
import glob
import json
import shutil
import tempfile
import urllib.request
from urllib.parse import urlencode, quote

from flask import Flask, request, jsonify, Response, send_file, send_from_directory
from config import apply_runtime_environment, create_app_config, ensure_runtime_dirs

from media_info import (
    build_info_payload,
    clean_filename,
    extract_video_info,
    human_filesize,
)

app = Flask(__name__)

APP_CONFIG = create_app_config()
apply_runtime_environment(APP_CONFIG)
ensure_runtime_dirs(APP_CONFIG)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Serverless guard rails (override via env vars in Vercel project settings).
MAX_MERGE_BYTES = int(os.environ.get("MAX_MERGE_BYTES", 400 * 1024 * 1024))   # processed (merged/mp3) downloads
MAX_CLIP_SECONDS = float(os.environ.get("MAX_CLIP_SECONDS", 600))             # per clip segment
MAX_CLIP_BYTES = int(os.environ.get("MAX_CLIP_BYTES", 400 * 1024 * 1024))     # per clip output
MAX_VIDEO_HEIGHT = int(os.environ.get("MAX_VIDEO_HEIGHT", 1080))              # cap resolution server-side

CHUNK_SIZE = 64 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_NOT_SERVERLESS_MSG = (
    "This is the serverless (Vercel) deployment: downloads run in your browser. "
    "The self-hosted server queue is not available here."
)


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg (static binary shipped via imageio-ffmpeg; Vercel has no system ffmpeg)
# ─────────────────────────────────────────────────────────────────────────────

def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return os.environ.get("FFMPEG_BIN", "ffmpeg")


# ─────────────────────────────────────────────────────────────────────────────
# Format resolution
# ─────────────────────────────────────────────────────────────────────────────

def _is_http(f):
    return (f.get('protocol') or 'https') in ('https', 'http')


def _pick_audio(formats):
    candidates = [
        f for f in formats
        if f.get('acodec') not in (None, 'none')
        and f.get('vcodec') == 'none'
        and f.get('url')
        and _is_http(f)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.get('tbr') or 0, f.get('filesize') or 0))


def _pick_progressive(formats):
    candidates = [
        f for f in formats
        if f.get('vcodec') not in (None, 'none')
        and f.get('acodec') not in (None, 'none')
        and f.get('url')
        and _is_http(f)
        and (f.get('height') or 0) <= MAX_VIDEO_HEIGHT
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))


def _pick_video_only(formats):
    candidates = [
        f for f in formats
        if f.get('vcodec') not in (None, 'none')
        and f.get('acodec') in (None, 'none')
        and f.get('url')
        and _is_http(f)
        and (f.get('height') or 0) <= MAX_VIDEO_HEIGHT
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))


def _stream_result(url, title, info, f):
    return {
        'mode': 'stream',
        'title': title,
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'filesize': f.get('filesize') or f.get('filesize_approx'),
        'filename': f"{title}.{f.get('ext') or 'mp4'}",
        'media_url': f['url'],
        'content_type': 'video/mp4',
        'url': url,
        'fmt': 'best',
    }


def resolve_media(url, fmt):
    """
    Select the best delivery strategy for a format request. Returns a dict:
      mode: 'stream'      -> direct progressive single file, proxy bytes through
      mode: 'disk_audio'  -> download bestaudio to /tmp, convert to mp3
      mode: 'disk_merge'  -> download DASH video+audio to /tmp, merge to mp4
    """
    fmt = fmt or 'best'

    # Fail fast on malformed format selectors (before any network extraction).
    # Allows yt-dlp selector syntax (ids, expressions); rejects junk like "!!".
    if fmt not in ('audio', 'best') and not re.fullmatch(r'[\w.=\-+/*\[\](),]+', str(fmt)):
        raise ValueError("Invalid format selector.")

    info = extract_video_info(url)
    formats = info.get('formats') or []
    title = clean_filename(info.get('title') or 'video')

    if fmt == 'audio':
        best = _pick_audio(formats)
        if best is None:
            raise LookupError("No downloadable audio stream found for this video.")
        return {
            'mode': 'disk_audio',
            'title': title,
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'filesize': best.get('filesize') or best.get('filesize_approx'),
            'filename': f"{title}.mp3",
            'url': url,
            'fmt': fmt,
            'ydl_format': 'ba/bestaudio/b',
        }

    if fmt in (None, '', 'best'):
        progressive = _pick_progressive(formats)
        if progressive is not None and (progressive.get('height') or 0) >= 720:
            return _stream_result(url, title, info, progressive)
        video = _pick_video_only(formats)
        audio = _pick_audio(formats)
        if video is not None and audio is not None:
            size = (video.get('filesize') or video.get('filesize_approx') or 0) + \
                   (audio.get('filesize') or audio.get('filesize_approx') or 0)
            if size <= MAX_MERGE_BYTES:
                return {
                    'mode': 'disk_merge',
                    'title': title,
                    'thumbnail': info.get('thumbnail'),
                    'duration': info.get('duration'),
                    'filesize': size,
                    'filename': f"{title}.mp4",
                    'url': url,
                    'fmt': 'best',
                    'ydl_format': 'bv*[height<={0}]+ba/b[ext=mp4]/b'.format(MAX_VIDEO_HEIGHT),
                }
            # Merged streams would exceed the cap: fall back to progressive if possible.
            if progressive is not None:
                return _stream_result(url, title, info, progressive)
            raise ValueError(
                f"Selected streams total {human_filesize(size)}, above the serverless limit of "
                f"{human_filesize(MAX_MERGE_BYTES)}. Pick a lower resolution."
            )
        if progressive is not None:
            return _stream_result(url, title, info, progressive)
        raise LookupError("No downloadable video stream found for this video.")

    # Custom yt-dlp format id(s), e.g. "137" or "137+140"
    ids = [part.strip() for part in str(fmt).split('+') if part.strip()]
    selected = []
    for fid in ids:
        match = next((f for f in formats if f.get('format_id') == fid and f.get('url')), None)
        if match is None:
            raise LookupError(f"Format '{fid}' is not available for this video.")
        selected.append(match)
    if len(selected) == 1 and selected[0].get('acodec') not in (None, 'none'):
        f = selected[0]
        return {
            'mode': 'stream',
            'title': title,
            'thumbnail': info.get('thumbnail'),
            'duration': info.get('duration'),
            'filesize': f.get('filesize') or f.get('filesize_approx'),
            'filename': f"{title}.{f.get('ext') or 'mp4'}",
            'media_url': f['url'],
            'content_type': 'video/mp4',
            'url': url,
            'fmt': fmt,
        }
    size = sum((f.get('filesize') or f.get('filesize_approx') or 0) for f in selected)
    if size > MAX_MERGE_BYTES:
        raise ValueError(
            f"Selected streams total {human_filesize(size)}, above the serverless limit of "
            f"{human_filesize(MAX_MERGE_BYTES)}."
        )
    return {
        'mode': 'disk_merge',
        'title': title,
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'filesize': size,
        'filename': f"{title}.mp4",
        'url': url,
        'fmt': fmt,
        'ydl_format': '+'.join(ids),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Media delivery helpers
# ─────────────────────────────────────────────────────────────────────────────

def open_upstream(media_url, range_header=None):
    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.youtube.com/',
    }
    if range_header:
        headers['Range'] = range_header
    req = urllib.request.Request(media_url, headers=headers)
    return urllib.request.urlopen(req, timeout=30)


def iter_upstream(upstream):
    try:
        while True:
            chunk = upstream.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            upstream.close()
        except Exception:
            pass


def _ydl_base_opts():
    return {
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'ffmpeg_location': ffmpeg_exe(),
        'retries': 3,
    }


def process_to_tmp(url, fmt_spec, audio_mp3=False, download_ranges=None):
    """Download (and merge/convert) media into a temp dir; return the file path."""
    workdir = tempfile.mkdtemp(dir=APP_CONFIG.temp_dir)
    opts = _ydl_base_opts()
    opts.update({
        'format': fmt_spec,
        'outtmpl': os.path.join(workdir, 'media.%(ext)s'),
        'merge_output_format': 'mp4',
    })
    if audio_mp3:
        opts['format'] = fmt_spec
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }]
    if download_ranges:
        opts['download_ranges'] = download_ranges

    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    if audio_mp3:
        preferred = os.path.join(workdir, 'media.mp3')
        candidates = [p for p in glob.glob(os.path.join(workdir, 'media.*')) if p == preferred] or \
                     sorted(glob.glob(os.path.join(workdir, 'media.*')), key=os.path.getsize, reverse=True)
    else:
        candidates = sorted(glob.glob(os.path.join(workdir, 'media.*')), key=os.path.getsize, reverse=True)
    if not candidates:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError("yt-dlp finished but produced no output file.")
    return candidates[0], workdir


def file_response(path, filename, mimetype, workdir=None):
    resp = send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
        conditional=True,
    )
    if workdir:
        def _cleanup(response):
            shutil.rmtree(workdir, ignore_errors=True)
        resp.call_on_close(_cleanup)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Routes: UI + static
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    boot_config = json.dumps({
        "serverless": True,
        "aiEnabled": bool(APP_CONFIG.use_hf_inference_api),
        "defaultSaveDir": None,
    })
    return Response(html.replace('"__APP_BOOT_CONFIG__"', boot_config, 1), mimetype="text/html")


@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)


@app.route('/api/health')
def health():
    return jsonify({
        "ok": True,
        "serverless": True,
        "ai_enabled": bool(APP_CONFIG.use_hf_inference_api),
        "ffmpeg": bool(ffmpeg_exe()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Routes: metadata + resolve + download + clip
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        info = extract_video_info(url)
        return jsonify(build_info_payload(info, default_save_dir=None))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/resolve', methods=['POST'])
def resolve():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    fmt = data.get('format') or 'best'
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        media = resolve_media(url, fmt)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    payload = {
        "title": media['title'],
        "filename": media['filename'],
        "filesize": media.get('filesize'),
        "filesize_str": human_filesize(media.get('filesize')),
        "thumbnail": media.get('thumbnail'),
        "duration": media.get('duration'),
        "mode": media['mode'],
        "proxy_url": "/api/download?" + urlencode({"url": url, "format": media['fmt']}),
        "needs_processing": media['mode'] != 'stream',
    }
    if media['mode'] == 'stream':
        payload["direct_url"] = media['media_url']
    return jsonify(payload)


@app.route('/api/download')
def download():
    url = request.args.get('url')
    fmt = request.args.get('format') or 'best'
    filename = request.args.get('filename')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        media = resolve_media(url, fmt)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    filename = clean_filename(filename or media['filename'])

    if media['mode'] == 'stream':
        try:
            upstream = open_upstream(media['media_url'], request.headers.get('Range'))
        except Exception as e:
            return jsonify({"error": f"Could not open media stream: {e}"}), 502

        headers = {
            'Content-Type': upstream.headers.get('Content-Type') or media.get('content_type') or 'application/octet-stream',
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}",
            'Accept-Ranges': 'bytes',
        }
        content_length = upstream.headers.get('Content-Length')
        if content_length:
            headers['Content-Length'] = content_length
        status = 206 if getattr(upstream, 'status', 200) == 206 else 200
        if status == 206 and upstream.headers.get('Content-Range'):
            headers['Content-Range'] = upstream.headers.get('Content-Range')
        return Response(iter_upstream(upstream), status=status, headers=headers)

    # Disk modes: merged best-quality mp4, or mp3 conversion
    try:
        if media['mode'] == 'disk_audio':
            path, workdir = process_to_tmp(url, media['ydl_format'], audio_mp3=True)
            mimetype = 'audio/mpeg'
            filename = filename.rsplit('.', 1)[0] + '.mp3'
        else:
            path, workdir = process_to_tmp(url, media['ydl_format'])
            mimetype = 'video/mp4'
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Server-side processing failed: {e}"}), 500

    if os.path.getsize(path) > MAX_MERGE_BYTES:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"error": f"Processed file exceeds the serverless limit ({human_filesize(MAX_MERGE_BYTES)})."}), 413

    return file_response(path, filename, mimetype, workdir)


@app.route('/api/clip')
def clip():
    url = request.args.get('url')
    title = request.args.get('title') or 'video'
    label = request.args.get('label') or 'clip'
    try:
        start = float(request.args.get('start', '0'))
        end = float(request.args.get('end', '0'))
    except ValueError:
        return jsonify({"error": "start/end must be seconds"}), 400

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if start < 0 or end <= start:
        return jsonify({"error": "Invalid clip range"}), 400
    if end - start > MAX_CLIP_SECONDS:
        return jsonify({"error": f"Clips are limited to {int(MAX_CLIP_SECONDS)}s on the serverless deployment."}), 400

    filename = f"{clean_filename(title)}_{clean_filename(label)}.mp4"
    ranges = lambda info, ydl: [{'start_time': start, 'end_time': end}]

    try:
        info = extract_video_info(url)
        total_size = info.get('filesize') or info.get('filesize_approx') or 0
        duration = info.get('duration') or 0
        if total_size and duration:
            expected = total_size * (end - start) / duration
            if expected > MAX_CLIP_BYTES:
                return jsonify({"error": f"This clip would exceed the serverless limit ({human_filesize(MAX_CLIP_BYTES)})."}), 413
        path, workdir = process_to_tmp(
            url,
            'bv*[height<={0}]+ba/b[ext=mp4]/b'.format(MAX_VIDEO_HEIGHT),
            download_ranges=ranges,
        )
    except Exception as e:
        return jsonify({"error": f"Clip export failed: {e}"}), 500

    return file_response(path, filename, 'video/mp4', workdir)


# ─────────────────────────────────────────────────────────────────────────────
# Routes: AI (Hugging Face Inference API only — no torch in serverless bundle)
# ─────────────────────────────────────────────────────────────────────────────

def _ai_available():
    return bool(APP_CONFIG.use_hf_inference_api)


def _ai_disabled_response():
    return jsonify({
        "error": "AI features are disabled on this deployment. Set USE_HF_INFERENCE_API=true "
                 "(and optionally HF_TOKEN) in your Vercel project environment variables to enable "
                 "them via the Hugging Face Inference API."
    }), 501


@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not _ai_available():
        return _ai_disabled_response()
    try:
        import ai_helper
        result = ai_helper.analyze_video(url)
        if "chunks" in result:
            for c in result["chunks"]:
                c.pop("embedding", None)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ai/search', methods=['POST'])
def ai_search():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    query = data.get('query')
    if not url or not query:
        return jsonify({"error": "URL and Query are required"}), 400
    if not _ai_available():
        return _ai_disabled_response()
    try:
        import ai_helper
        results = ai_helper.query_transcript(url, query)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes: server-only APIs → explicit 501 with guidance
# ─────────────────────────────────────────────────────────────────────────────

def _not_serverless():
    return jsonify({"error": _NOT_SERVERLESS_MSG, "serverless": True}), 501


@app.route('/api/enqueue', methods=['POST'])
def enqueue_download():
    return _not_serverless()


@app.route('/api/cancel', methods=['POST'])
def cancel_task():
    return _not_serverless()


@app.route('/api/remove', methods=['POST'])
def remove_task():
    return _not_serverless()


@app.route('/api/queue', methods=['GET'])
def get_queue():
    return _not_serverless()


@app.route('/api/stream')
def get_stream():
    return _not_serverless()
