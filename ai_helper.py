"""Transcript extraction and AI cut detection for YT-DLP Studio.

The pipeline deliberately follows the order the UI presents it in:

    1. extract   download the subtitles and parse them into timed entries
    2. analyse   chunk the transcript (~60s) and score every chunk for importance
    3. sections  group the strongest chunks into clip ranges and label them

The clip ranges are plain ``{start, end, label}`` timestamps, which is exactly
what the timeline editor and the ffmpeg exporter consume -- the whole point of
the analysis is to turn a transcript into cut points, not to produce prose.

Removed from the previous implementation: per-chunk MiniLM embeddings and the
transcript semantic-search endpoint. Nothing in the UI ever called the search
endpoint, and the embeddings were stripped from the response before it was
serialised, so every chunk paid for a model load and a forward pass to compute
384 floats that were then thrown away.
"""

import glob
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request

from config import apply_runtime_environment, create_app_config, ensure_runtime_dirs

# Model handles, populated lazily and shared by every request thread.
_classifier = None
_summarizer = None
_models_lock = threading.RLock()
_last_model_use = 0.0
_idle_thread_started = False
_idle_thread_lock = threading.Lock()

# Serialises work per video so two browsers analysing the same URL do not both
# run the (expensive, memory hungry) pipeline and race to write the cache file.
_analysis_locks = {}
_analysis_locks_guard = threading.Lock()

APP_CONFIG = create_app_config()
apply_runtime_environment(APP_CONFIG)
ensure_runtime_dirs(APP_CONFIG)
CACHE_DIR = APP_CONFIG.cache_dir

CANDIDATE_LABELS = ["highlight insight", "filler chat", "tutorial process"]
HF_API_BASE = "https://api-inference.huggingface.co/models"


# --------------------------------------------------------------------------- #
# Hugging Face serverless fallback (used when the host cannot fit local models)
# --------------------------------------------------------------------------- #
def call_hf_api(url, payload, token=None, retries=5, delay=5):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
                err_data = json.loads(err_body)
                if "estimated_time" in err_data or "currently loading" in err_data.get("error", ""):
                    wait_time = min(float(err_data.get("estimated_time", 20.0)), 20.0)
                    print(f"[HF API] Model loading. Waiting {wait_time}s (attempt {attempt + 1}/{retries})...")
                    time.sleep(wait_time)
                    continue
                if e.code == 429:
                    print(f"[HF API] Rate limited. Waiting {delay}s...")
                    time.sleep(delay)
                    continue
            except Exception:
                pass
            if attempt == retries - 1:
                raise e
            time.sleep(delay)
        except urllib.error.URLError as e:
            if attempt == retries - 1:
                raise RuntimeError(
                    "Failed to connect to the Hugging Face Serverless API. "
                    "Check the server's internet connection, or set "
                    "USE_HF_INFERENCE_API=false to run the models locally."
                ) from e
            time.sleep(delay)
        except Exception as e:
            if attempt == retries - 1:
                raise e
            time.sleep(delay)
    raise Exception("API call failed after max retries")


# --------------------------------------------------------------------------- #
# Model lifecycle
# --------------------------------------------------------------------------- #
def _mark_used():
    global _last_model_use
    _last_model_use = time.time()


def _start_idle_watchdog():
    """Frees model memory after a configurable idle period (AI_IDLE_UNLOAD_SECONDS)."""
    global _idle_thread_started
    with _idle_thread_lock:
        if _idle_thread_started:
            return
        _idle_thread_started = True

    def _watchdog():
        while True:
            time.sleep(60)
            idle_for = APP_CONFIG.ai_idle_unload_seconds
            if idle_for <= 0:
                continue
            with _models_lock:
                if _last_model_use and (time.time() - _last_model_use) > idle_for:
                    print("[ai] models idle, freeing memory")
                    unload_models()

    threading.Thread(target=_watchdog, daemon=True).start()


def get_classifier():
    global _classifier
    with _models_lock:
        if _classifier is None:
            from transformers import pipeline
            print(f"[ai] loading classifier {APP_CONFIG.ai_nli_model}")
            _classifier = pipeline(
                "zero-shot-classification",
                model=APP_CONFIG.ai_nli_model,
                device=-1,
            )
            _start_idle_watchdog()
        _mark_used()
        return _classifier


def get_summarizer():
    global _summarizer
    with _models_lock:
        if _summarizer is None:
            from transformers import pipeline
            print(f"[ai] loading summarizer {APP_CONFIG.ai_summary_model}")
            _summarizer = pipeline(
                "summarization",
                model=APP_CONFIG.ai_summary_model,
                device=-1,
            )
            _start_idle_watchdog()
        _mark_used()
        return _summarizer


def unload_models():
    global _classifier, _summarizer, _last_model_use
    with _models_lock:
        if _classifier is not None or _summarizer is not None:
            _classifier = None
            _summarizer = None
            _last_model_use = 0.0
        try:
            import gc
            gc.collect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Subtitle parsing helpers
# --------------------------------------------------------------------------- #
def time_to_seconds(t_str):
    t_str = t_str.replace(',', '.')
    parts = t_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    else:
        return float(parts[0])


def clean_text_for_comparison(text):
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    return " ".join(text.split())


def deduplicate_entries(entries):
    """Collapses rolling/karaoke captions where each cue repeats the previous one."""
    if not entries:
        return []

    cleaned = []
    for entry in entries:
        text = entry['text'].strip()
        if not text:
            continue

        if not cleaned:
            cleaned.append(entry)
            continue

        prev = cleaned[-1]
        prev_text = prev['text'].strip()

        norm_text = clean_text_for_comparison(text)
        norm_prev = clean_text_for_comparison(prev_text)

        if norm_text.startswith(norm_prev):
            prev['text'] = text
            prev['end'] = entry['end']
        elif norm_prev.startswith(norm_text):
            prev['end'] = max(prev['end'], entry['end'])
        else:
            cleaned.append(entry)

    return cleaned


def parse_subtitles(file_path):
    """Parses VTT or SRT files into timestamped caption blocks.

    Returns: [{'start': float, 'end': float, 'text': str}]
    """
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    entries = []
    current_start = None
    current_end = None
    current_text = []

    time_pattern = re.compile(r'(\d{1,2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})\s+-->\s+(\d{1,2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})')

    for line in lines:
        line = line.strip()
        match = time_pattern.search(line)
        if match:
            if current_start is not None and current_text:
                full_text = " ".join(current_text).strip()
                full_text = re.sub(r'<[^>]+>', '', full_text)
                if full_text:
                    entries.append({
                        'start': current_start,
                        'end': current_end,
                        'text': full_text
                    })
            current_start = time_to_seconds(match.group(1))
            current_end = time_to_seconds(match.group(2))
            current_text = []
        elif line:
            if line.isdigit() and len(current_text) == 0:
                continue
            if line == "WEBVTT" or line.startswith("Kind:") or line.startswith("Language:"):
                continue
            current_text.append(line)

    if current_start is not None and current_text:
        full_text = " ".join(current_text).strip()
        full_text = re.sub(r'<[^>]+>', '', full_text)
        if full_text:
            entries.append({
                'start': current_start,
                'end': current_end,
                'text': full_text
            })

    return deduplicate_entries(entries)


# --------------------------------------------------------------------------- #
# Step 1: transcript extraction
# --------------------------------------------------------------------------- #
def download_subtitles(video_url, progress_cb=None):
    """Downloads subtitles and returns ``(entries, language)``.

    English is tried first; if the video has no English track we fall back to
    whatever language it does have, so non-English videos still get cuts.
    """
    import yt_dlp

    if progress_cb:
        progress_cb(4, "fetching subtitles")

    attempts = (
        {"subtitleslangs": ["en", "en-.*"], "label": "English"},
        {"subtitleslangs": ["all"], "label": "any language"},
    )

    for attempt_index, attempt in enumerate(attempts):
        temp_dir = tempfile.mkdtemp(dir=APP_CONFIG.temp_dir)
        out_tmpl = os.path.join(temp_dir, 'subtitle')
        try:
            ydl_opts = {
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': attempt["subtitleslangs"],
                'subtitlesformat': 'vtt/srt/best',
                'skip_download': True,
                'outtmpl': out_tmpl,
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])

            subtitle_files = glob.glob(out_tmpl + ".*")
            if not subtitle_files:
                continue

            # Prefer the richest track: manual captions are usually longer than
            # the auto-generated ones, and size is a good proxy for content.
            subtitle_files.sort(key=lambda p: os.path.getsize(p), reverse=True)
            sub_file = subtitle_files[0]

            language = os.path.splitext(sub_file)[0].rsplit('.', 1)[-1] if '.' in sub_file else "unknown"
            entries = parse_subtitles(sub_file)
            if entries:
                if progress_cb:
                    progress_cb(12, f"transcript ready ({len(entries)} cues, {attempt['label']})")
                return entries, language
        except Exception as e:
            print(f"Error downloading subtitles ({attempt['label']}): {e}")
        finally:
            _remove_temp_dir(temp_dir)

        if attempt_index == 0 and progress_cb:
            progress_cb(8, "no English captions, retrying any language")

    return [], None


def _remove_temp_dir(temp_dir):
    try:
        for f in glob.glob(os.path.join(temp_dir, "*")):
            os.remove(f)
        os.rmdir(temp_dir)
    except Exception:
        pass


def extract_video_id(url):
    match = re.search(r'(?:v=|\/embed\/|\/101\/|\/v\/|youtu\.be\/|\/shorts\/)([a-zA-Z0-9_-]{11})', url)
    if match:
        return match.group(1)
    import hashlib
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def transcript_duration(entries):
    """Approximate media length from the last caption cue."""
    if not entries:
        return None
    try:
        return float(max(e['end'] for e in entries))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Step 2: analysis
# --------------------------------------------------------------------------- #
def chunk_transcript(entries, chunk_duration=60):
    chunks = []
    current_chunk = []
    chunk_start = None

    for entry in entries:
        if chunk_start is None:
            chunk_start = entry['start']

        current_chunk.append(entry)

        if entry['end'] - chunk_start >= chunk_duration:
            text = " ".join([e['text'] for e in current_chunk]).strip()
            chunks.append({
                'start': chunk_start,
                'end': entry['end'],
                'text': text
            })
            current_chunk = []
            chunk_start = None

    if current_chunk:
        text = " ".join([e['text'] for e in current_chunk]).strip()
        chunks.append({
            'start': chunk_start,
            'end': current_chunk[-1]['end'],
            'text': text
        })

    return chunks


def score_chunk(text, classifier=None):
    """Zero-shot importance for one chunk; returns a float in [0, 2]."""
    if not text or not text.strip():
        return 0.0

    try:
        if APP_CONFIG.use_hf_inference_api:
            url = f"{HF_API_BASE}/{APP_CONFIG.ai_nli_model}"
            res = call_hf_api(
                url,
                {"inputs": text, "parameters": {"candidate_labels": CANDIDATE_LABELS}},
                APP_CONFIG.hf_token,
            )
            scores = dict(zip(res['labels'], res['scores']))
        else:
            res = classifier(text, candidate_labels=CANDIDATE_LABELS)
            scores = dict(zip(res['labels'], res['scores']))

        return scores.get("highlight insight", 0.0) + scores.get("tutorial process", 0.0)
    except Exception as e:
        print(f"Classifier error: {e}")
        return 0.0


def summarize_text(text, summarizer=None):
    """Short label for a clip range. Falls back to the transcript itself."""
    text = (text or "").strip()
    if not text:
        return "Clip"

    try:
        if APP_CONFIG.use_hf_inference_api:
            url = f"{HF_API_BASE}/{APP_CONFIG.ai_summary_model}"
            res = call_hf_api(
                url,
                {
                    "inputs": text,
                    "parameters": {"max_length": 40, "min_length": 10, "do_sample": False},
                },
                APP_CONFIG.hf_token,
            )
            summary = res[0]['summary_text']
        else:
            summary = summarizer(text, max_length=40, min_length=10, do_sample=False)[0]['summary_text']
        summary = " ".join(summary.split())
        return summary or text[:80]
    except Exception as e:
        print(f"Summarizer error: {e}")
        return text[:80] + ("..." if len(text) > 80 else "")


# --------------------------------------------------------------------------- #
# Step 3: sections -> clip ranges
# --------------------------------------------------------------------------- #
def build_sections(chunks, media_duration=None):
    """Groups the strongest chunks into clip ranges.

    Pure function over the scored chunks, so it is unit testable without models.
    """
    cfg = APP_CONFIG
    threshold = cfg.ai_importance_threshold
    max_sections = max(1, cfg.ai_max_sections)
    min_seconds = max(0.0, cfg.ai_min_section_seconds)
    max_seconds = max(min_seconds + 1.0, cfg.ai_max_section_seconds)
    pad = 1.0

    ranked = [c for c in chunks if c.get('importance', 0.0) >= threshold and c.get('text')]

    # Fallback: if nothing clears the bar, keep the best scoring chunks so the
    # user still gets something usable to edit.
    if not ranked and chunks:
        fallback_count = max(1, min(3, len(chunks) // 5 or 1))
        ranked = sorted(chunks, key=lambda c: c.get('importance', 0.0), reverse=True)[:fallback_count]
        ranked.sort(key=lambda c: c['id'])

    groups = []
    for chunk in ranked:
        if groups:
            last_group = groups[-1]
            gap_ok = chunk['id'] - last_group[-1]['id'] <= 2
            length_ok = (chunk['end'] - last_group[0]['start']) <= max_seconds
            if gap_ok and length_ok:
                last_group.append(chunk)
                continue
        groups.append([chunk])

    sections = []
    for group in groups:
        start = max(0.0, group[0]['start'] - pad)
        end = group[-1]['end'] + pad
        if media_duration:
            end = min(end, media_duration)

        if end - start < min_seconds:
            middle = (start + end) / 2.0
            start = max(0.0, middle - min_seconds / 2.0)
            end = start + min_seconds
            if media_duration:
                end = min(end, media_duration)
                start = max(0.0, end - min_seconds)

        score = sum(c.get('importance', 0.0) for c in group) / len(group)
        sections.append({
            'start': round(start, 2),
            'end': round(end, 2),
            'score': round(score, 3),
            'chunk_ids': [c['id'] for c in group],
        })

    sections = _merge_overlaps(sections)

    if len(sections) > max_sections:
        sections = sorted(sections, key=lambda s: s['score'], reverse=True)[:max_sections]
        sections.sort(key=lambda s: s['start'])

    return sections


def _merge_overlaps(sections):
    merged = []
    for section in sorted(sections, key=lambda s: s['start']):
        if merged and section['start'] <= merged[-1]['end']:
            previous = merged[-1]
            previous['end'] = max(previous['end'], section['end'])
            previous['score'] = round(max(previous['score'], section['score']), 3)
            previous['chunk_ids'] = previous['chunk_ids'] + [
                cid for cid in section['chunk_ids'] if cid not in previous['chunk_ids']
            ]
        else:
            merged.append(dict(section))
    return merged


def section_text(chunks, section):
    wanted = set(section.get('chunk_ids', ()))
    parts = [c['text'] for c in chunks if c['id'] in wanted]
    return " ".join(parts).strip()


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _cache_path(video_id):
    return os.path.join(CACHE_DIR, f"{video_id}.json")


def _read_cache(video_id):
    path = _cache_path(video_id)
    if not os.path.exists(path):
        return None
    ttl = APP_CONFIG.ai_cache_ttl_seconds
    if ttl > 0 and (time.time() - os.path.getmtime(path)) > ttl:
        try:
            os.remove(path)
        except Exception:
            pass
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(video_id, result):
    path = _cache_path(video_id)
    tmp_path = f"{path}.tmp"
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"Could not write analysis cache: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _lock_for(video_id):
    with _analysis_locks_guard:
        lock = _analysis_locks.get(video_id)
        if lock is None:
            lock = threading.RLock()
            _analysis_locks[video_id] = lock
        return lock


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def analyze_video(video_url, progress_cb=None):
    """Runs extract -> analyse -> sections and returns the result dict.

    ``progress_cb(percent, message)`` is optional and is used to drive the queue
    UI while the (slow) analysis runs.
    """
    def report(percent, message):
        if progress_cb:
            try:
                progress_cb(round(float(percent), 1), message)
            except Exception:
                pass

    video_id = extract_video_id(video_url)

    cached = _read_cache(video_id)
    if cached is not None:
        report(100, "loaded from cache")
        return cached

    # Two browsers analysing the same video must not run the models twice.
    with _lock_for(video_id):
        cached = _read_cache(video_id)
        if cached is not None:
            report(100, "loaded from cache")
            return cached

        # Step 1 -- transcript
        entries, language = download_subtitles(video_url, progress_cb=report)
        if not entries:
            return {
                "video_url": video_url,
                "video_id": video_id,
                "chunks": [],
                "sections": [],
                "error": "No subtitles or captions were available for this video.",
            }

        report(15, f"read {len(entries)} caption cues")

        # Step 2 -- chunk and score
        chunks = chunk_transcript(entries, chunk_duration=APP_CONFIG.ai_chunk_seconds)
        media_duration = transcript_duration(entries)

        classifier = None
        summarizer = None
        if not APP_CONFIG.use_hf_inference_api:
            classifier = get_classifier()

        total = len(chunks) or 1
        for index, chunk in enumerate(chunks):
            chunk['id'] = index
            chunk['importance'] = round(score_chunk(chunk['text'], classifier), 4)
            report(18 + (62.0 * (index + 1) / total), f"scoring chunk {index + 1}/{total}")

        # Step 3 -- sections
        report(82, "building clip ranges")
        sections = build_sections(chunks, media_duration=media_duration)

        if not APP_CONFIG.use_hf_inference_api and sections:
            summarizer = get_summarizer()

        total_sections = len(sections) or 1
        for index, section in enumerate(sections):
            section['label'] = summarize_text(section_text(chunks, section), summarizer)
            report(84 + (16.0 * (index + 1) / total_sections), f"naming clip {index + 1}/{total_sections}")

        result = {
            "video_url": video_url,
            "video_id": video_id,
            "language": language,
            "duration": media_duration,
            "transcript_entries": len(entries),
            "chunks": [
                {
                    "id": c["id"],
                    "start": c["start"],
                    "end": c["end"],
                    "text": c["text"],
                    "importance": c["importance"],
                }
                for c in chunks
            ],
            "sections": [
                {
                    "start": s["start"],
                    "end": s["end"],
                    "score": s["score"],
                    "label": s["label"],
                }
                for s in sections
            ],
        }

        _write_cache(video_id, result)
        report(100, "analysis complete")
        return result
