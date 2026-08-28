"""
Shared helpers for extracting video metadata with yt-dlp.

Used by both deployment targets:
- app.py               (self-hosted Flask server, subprocess yt-dlp queue)
- serverless_backend.py (Vercel serverless functions)
"""

import re
from datetime import datetime


def clean_filename(name):
    # Removes illegal characters for filenames
    return re.sub(r'[\\/*?:"<>|]', "", str(name or "")).strip()


def format_upload_date(date_str):
    if not date_str or len(date_str) != 8:
        return date_str
    try:
        return datetime.strptime(date_str, "%Y%m%d").strftime("%b %d, %Y")
    except ValueError:
        return date_str


def human_filesize(filesize):
    if not filesize:
        return "N/A"
    gb = filesize / (1024 * 1024 * 1024)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    return f"{filesize / (1024 * 1024):.2f} MB"


def extract_video_info(url, extra_opts=None):
    """Run yt-dlp metadata extraction (no download). Raises on failure."""
    import yt_dlp

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    if extra_opts:
        ydl_opts.update(extra_opts)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def build_info_payload(info, default_save_dir=None):
    """Turn a yt-dlp info dict into the JSON payload the frontend expects."""
    formats = []
    seen_resolutions = set()

    # Gather video resolutions
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
                    'note': f.get('format_note') or ''
                })

    # Add basic formats
    formats.sort(key=lambda x: int(re.sub(r'\D', '', x['resolution']) or 0), reverse=True)

    subtitles_available = bool(info.get('subtitles') or info.get('automatic_captions'))

    filesize = info.get('filesize') or info.get('filesize_approx')
    filesize_str = human_filesize(filesize)

    vcodec = info.get('vcodec') or 'N/A'
    acodec = info.get('acodec') or 'N/A'

    return {
        'title': info.get('title'),
        'duration': info.get('duration'),
        'thumbnail': info.get('thumbnail'),
        'formats': formats,
        'subtitles_available': subtitles_available,
        'default_save_dir': default_save_dir,
        'uploader': info.get('uploader') or info.get('channel') or 'N/A',
        'upload_date': format_upload_date(info.get('upload_date')),
        'resolution': f"{info.get('height')}p" if info.get('height') else "N/A",
        'filesize': filesize_str,
        'vcodec': vcodec,
        'acodec': acodec,
        'fps': info.get('fps') or 'N/A'
    }
