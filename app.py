import os
import sys
import json
import time
import re
import queue
import threading
import subprocess
import glob
import tempfile
import signal
from flask import Flask, request, jsonify, Response, send_from_directory
from config import apply_runtime_environment, create_app_config, ensure_runtime_dirs

app = Flask(__name__)

# Constants
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
APP_CONFIG = create_app_config()
DEFAULT_SAVE_DIR = APP_CONFIG.default_save_dir
apply_runtime_environment(APP_CONFIG)
ensure_runtime_dirs(APP_CONFIG)

# State
task_queue = queue.Queue()
tasks = {}      # task_id -> task_dict
listeners = []  # List of queue.Queue objects for SSE
active_task = None
active_task_lock = threading.Lock()

# Import local AI helper after runtime env is configured
import ai_helper


def _subprocess_creation_kwargs():
    kwargs = {}
    if sys.platform == 'win32':
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _run_subprocess(args, **kwargs):
    return subprocess.Popen(args, **kwargs, **_subprocess_creation_kwargs())


def _normalize_save_dir(save_dir):
    return os.path.abspath(os.path.expanduser(save_dir or DEFAULT_SAVE_DIR))


def _clip_temp_dir(task_id):
    return os.path.join(APP_CONFIG.temp_dir, f"temp_clips_{task_id}")


def _render_index_with_config():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as handle:
        html = handle.read()
    boot_config = json.dumps({"defaultSaveDir": DEFAULT_SAVE_DIR})
    return html.replace('"__APP_BOOT_CONFIG__"', boot_config, 1)

def notify_listeners(event_type, data):
    payload = {"type": event_type, "data": data}
    for listener in listeners:
        try:
            listener.put(payload)
        except Exception:
            pass

def update_task(task_id, **kwargs):
    if task_id in tasks:
        tasks[task_id].update(kwargs)
        # Create a copy without the process object for serialization
        task_copy = {k: v for k, v in tasks[task_id].items() if k != 'process'}
        notify_listeners("task_update", task_copy)

def parse_ytdlp_progress(line):
    # Parses line: [download]  12.3% of ~45.20MiB at  3.12MiB/s ETA 00:15
    # Or: [download]  45.0% of 10.20MiB at 10.00MiB/s ETA 00:01
    percent_match = re.search(r'(\d+(?:\.\d+)?)%', line)
    speed_match = re.search(r'at\s+(\S+)', line)
    eta_match = re.search(r'ETA\s+(\S+)', line)
    
    percent = float(percent_match.group(1)) if percent_match else None
    speed = speed_match.group(1) if speed_match else None
    eta = eta_match.group(1) if eta_match else None
    
    return percent, speed, eta

def kill_process_tree(pid):
    """
    Forcefully terminates a subprocess tree on current platform.
    """
    try:
        if sys.platform == 'win32':
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=True)
        else:
            process_group_id = os.getpgid(pid)
            os.killpg(process_group_id, signal.SIGTERM)
    except Exception as e:
        print(f"Error killing process {pid}: {e}")

def run_download_process(task):
    task_id = task['id']
    url = task['url']
    save_dir = task['save_dir']
    fmt = task['format']
    
    os.makedirs(save_dir, exist_ok=True)
    
    # Base yt-dlp arguments
    # Output template: title.ext
    out_tmpl = os.path.join(save_dir, '%(title)s.%(ext)s')
    
    args = [APP_CONFIG.yt_dlp_bin, "--no-playlist"]
    
    if fmt == 'audio':
        args += ["-f", "ba", "-x", "--audio-format", "mp3"]
    elif fmt == 'best':
        args += ["-f", "bv*+ba/b"] # downloads best video and audio merged
    else:
        args += ["-f", fmt]
        
    args += ["-o", out_tmpl, url]
    
    update_task(task_id, status='downloading', progress=0.0)
    
    try:
        # Spawn yt-dlp on Windows with flags to allow group killing if needed
        # We redirect stderr to stdout so we can capture all messages
        p = _run_subprocess(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        update_task(task_id, process=p)
        
        title_extracted = False
        
        while True:
            line = p.stdout.readline()
            if not line:
                break
            
            line = line.strip()
            print(f"[yt-dlp LOG] {line}") # Debug logging on console
            
            # Extract title if not already done
            if not title_extracted:
                title_match = re.search(r'\[download\] Destination: (.+)', line)
                if not title_match:
                    title_match = re.search(r'\[info\] (\S+): Downloading webpage', line)
                if title_match:
                    # Try to get cleaner title via --get-title later or just use current match
                    pass
            
            if "[download]" in line:
                pct, spd, eta = parse_ytdlp_progress(line)
                update_args = {}
                if pct is not None:
                    update_args['progress'] = pct
                if spd is not None:
                    update_args['speed'] = spd
                if eta is not None:
                    update_args['eta'] = eta
                if update_args:
                    update_task(task_id, **update_args)
                    
        p.wait()
        
        if p.returncode == 0:
            return True, None
        else:
            if tasks.get(task_id, {}).get('status') == 'cancelled':
                return False, "Cancelled"
            return False, f"yt-dlp exited with code {p.returncode}"
            
    except Exception as e:
        return False, str(e)

def run_clip_export(task):
    task_id = task['id']
    url = task['url']
    save_dir = task['save_dir']
    regions = task['regions'] # list of {start, end, label, enabled}
    
    update_task(task_id, status='downloading', progress=0.0)
    
    # To clip a video, we first download the full best quality video to a temp file,
    # then clip it into multiple parts, and then delete the temp full file.
    temp_dir = _clip_temp_dir(task_id)
    os.makedirs(temp_dir, exist_ok=True)
    temp_full_path = os.path.join(temp_dir, f"full_{task_id}.mp4")
    
    # Download full video temporarily
    dl_args = [APP_CONFIG.yt_dlp_bin, "--no-playlist", "-f", "mp4/best", "-o", temp_full_path, url]
    
    try:
        p_dl = _run_subprocess(
            dl_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        update_task(task_id, process=p_dl)
        
        while True:
            line = p_dl.stdout.readline()
            if not line:
                break
            line = line.strip()
            if "[download]" in line:
                pct, spd, eta = parse_ytdlp_progress(line)
                update_args = {}
                if pct is not None:
                    update_args['progress'] = pct * 0.9 # Save 10% for clipping
                if spd is not None:
                    update_args['speed'] = spd
                if eta is not None:
                    update_args['eta'] = eta
                if update_args:
                    update_task(task_id, **update_args)
                    
        p_dl.wait()
        
        if p_dl.returncode != 0:
            if tasks.get(task_id, {}).get('status') == 'cancelled':
                cleanup_temp_files(temp_full_path, temp_dir)
                return False, "Cancelled"
            cleanup_temp_files(temp_full_path, temp_dir)
            return False, f"Failed to download video for clipping (exit code {p_dl.returncode})"
            
        # Get actual video title (we can extract from metadata or use default)
        video_title = "clip"
        try:
            meta_process = subprocess.run(
                [APP_CONFIG.yt_dlp_bin, "--get-title", url],
                capture_output=True,
                text=True,
                check=True,
                **_subprocess_creation_kwargs()
            )
            video_title = clean_filename(meta_process.stdout.strip())
        except Exception:
            pass
            
        # Perform clipping using ffmpeg
        update_task(task_id, status='clipping', progress=90.0, speed="N/A", eta="clipping...")
        
        enabled_regions = [r for r in regions if r.get('enabled', True)]
        for i, r in enumerate(enabled_regions):
            start = r['start']
            end = r['end']
            label = clean_filename(r.get('label') or f"segment_{i+1}")
            
            output_filename = f"{video_title}_{label}.mp4"
            output_path = os.path.join(save_dir, output_filename)
            
            # Clip command using ffmpeg (stream copy -ss before -i is extremely fast and accurate)
            ffmpeg_args = [
                APP_CONFIG.ffmpeg_bin, "-y",
                "-ss", str(start),
                "-to", str(end),
                "-i", temp_full_path,
                "-c", "copy",
                output_path
            ]
            
            p_ff = _run_subprocess(
                ffmpeg_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            update_task(task_id, process=p_ff)
            p_ff.wait()
            
            if p_ff.returncode != 0:
                # If one clip fails or gets cancelled
                if tasks.get(task_id, {}).get('status') == 'cancelled':
                    cleanup_temp_files(temp_full_path, temp_dir)
                    return False, "Cancelled"
                cleanup_temp_files(temp_full_path, temp_dir)
                return False, f"FFmpeg error clipping segment '{label}' (exit code {p_ff.returncode})"
                
            # Progress calculation
            progress = 90.0 + (10.0 * (i + 1) / len(enabled_regions))
            update_task(task_id, progress=progress)
            
        cleanup_temp_files(temp_full_path, temp_dir)
        return True, None
        
    except Exception as e:
        cleanup_temp_files(temp_full_path, temp_dir)
        return False, str(e)

def tempfile_temp_dir():
    return APP_CONFIG.temp_dir

def cleanup_temp_files(full_path, temp_dir):
    try:
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception:
        pass
    try:
        # Clean up any residual .part files
        for f in glob.glob(os.path.join(temp_dir, "*")):
            os.remove(f)
        os.rmdir(temp_dir)
    except Exception:
        pass

def clean_filename(name):
    # Removes illegal characters for filenames
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def queue_worker():
    global active_task
    while True:
        task = task_queue.get()
        if task is None:
            break
            
        task_id = task['id']
        
        with active_task_lock:
            active_task = task
            
        if tasks.get(task_id, {}).get('status') == 'cancelled':
            task_queue.task_done()
            with active_task_lock:
                active_task = None
            continue
            
        update_task(task_id, status='downloading', progress=0.0)
        
        success = False
        err_msg = None
        
        if task['type'] == 'download':
            success, err_msg = run_download_process(task)
        elif task['type'] == 'clip':
            success, err_msg = run_clip_export(task)
            
        with active_task_lock:
            active_task = None
            
        if success:
            update_task(task_id, status='completed', progress=100.0, speed="N/A", eta="00:00")
        else:
            current_status = tasks.get(task_id, {}).get('status')
            if current_status != 'cancelled':
                update_task(task_id, status='failed', error=err_msg or "Unknown error")
                
        # Clean up any partial files if task was cancelled or failed
        if not success:
            cleanup_partial_downloads(task)
            
        task_queue.task_done()

def cleanup_partial_downloads(task):
    save_dir = task['save_dir']
    # Scan save_dir for .part or .ytdl files that match title
    try:
        for f in os.listdir(save_dir):
            if f.endswith('.part') or f.endswith('.ytdl'):
                # We can remove it
                os.remove(os.path.join(save_dir, f))
    except Exception:
        pass

# Start Background Worker
threading.Thread(target=queue_worker, daemon=True).start()

# Flask Routes
@app.route('/')
def index():
    return Response(_render_index_with_config(), mimetype="text/html")

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)

def format_upload_date(date_str):
    if not date_str or len(date_str) != 8:
        return date_str
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        yr = date_str[:4]
        m_idx = int(date_str[4:6]) - 1
        day = str(int(date_str[6:]))
        if 0 <= m_idx < 12:
            return f"{months[m_idx]} {day}, {yr}"
    except Exception:
        pass
    return date_str

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
        
    try:
        # Extract metadata
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        # Parse formats
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
        
        # Check if it has subtitles
        subtitles_available = False
        if info.get('subtitles') or info.get('automatic_captions'):
            subtitles_available = True
            
        # Try to get filesize
        filesize = info.get('filesize') or info.get('filesize_approx')
        filesize_str = "N/A"
        if filesize:
            gb = filesize / (1024 * 1024 * 1024)
            if gb >= 1.0:
                filesize_str = f"{gb:.2f} GB"
            else:
                filesize_str = f"{filesize / (1024 * 1024):.2f} MB"
                
        # Get formats codecs
        vcodec = info.get('vcodec') or 'N/A'
        acodec = info.get('acodec') or 'N/A'
        
        result = {
            'title': info.get('title'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
            'formats': formats,
            'subtitles_available': subtitles_available,
            'default_save_dir': DEFAULT_SAVE_DIR,
            'uploader': info.get('uploader') or info.get('channel') or 'N/A',
            'upload_date': format_upload_date(info.get('upload_date')),
            'resolution': f"{info.get('height')}p" if info.get('height') else "N/A",
            'filesize': filesize_str,
            'vcodec': vcodec,
            'acodec': acodec,
            'fps': info.get('fps') or 'N/A'
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/enqueue', methods=['POST'])
def enqueue_download():
    data = request.json
    url = data.get('url')
    save_dir = _normalize_save_dir(data.get('save_dir'))
    fmt = data.get('format') or 'best'
    title = data.get('title') or "YouTube Video"
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
        
    task_id = f"dl_{int(time.time() * 1000)}"
    task = {
        'id': task_id,
        'title': title,
        'url': url,
        'type': 'download',
        'format': fmt,
        'save_dir': save_dir,
        'status': 'pending',
        'progress': 0.0,
        'speed': 'N/A',
        'eta': 'pending...',
        'error': None,
        'process': None
    }
    
    tasks[task_id] = task
    task_queue.put(task)
    
    # Notify initial task creation
    task_copy = {k: v for k, v in task.items() if k != 'process'}
    notify_listeners("task_update", task_copy)
    
    return jsonify({"success": True, "task_id": task_id})

@app.route('/api/clip', methods=['POST'])
def enqueue_clip():
    data = request.json
    url = data.get('url')
    save_dir = _normalize_save_dir(data.get('save_dir'))
    regions = data.get('regions') # list of {start, end, label, enabled}
    title = data.get('title') or "YouTube Clip"
    
    if not url or not regions:
        return jsonify({"error": "URL and regions are required"}), 400
        
    task_id = f"clip_{int(time.time() * 1000)}"
    task = {
        'id': task_id,
        'title': title,
        'url': url,
        'type': 'clip',
        'regions': regions,
        'save_dir': save_dir,
        'status': 'pending',
        'progress': 0.0,
        'speed': 'N/A',
        'eta': 'pending...',
        'error': None,
        'process': None
    }
    
    tasks[task_id] = task
    task_queue.put(task)
    
    task_copy = {k: v for k, v in task.items() if k != 'process'}
    notify_listeners("task_update", task_copy)
    
    return jsonify({"success": True, "task_id": task_id})

@app.route('/api/cancel', methods=['POST'])
def cancel_task():
    data = request.json
    task_id = data.get('task_id')
    
    if not task_id or task_id not in tasks:
        return jsonify({"error": "Invalid task ID"}), 400
        
    task = tasks[task_id]
    
    if task['status'] in ['completed', 'failed', 'cancelled']:
        return jsonify({"success": True, "message": "Task already finished"})
        
    update_task(task_id, status='cancelled', speed="N/A", eta="cancelled")
    
    # Check if task is actively running and kill its process
    with active_task_lock:
        if active_task and active_task['id'] == task_id:
            p = active_task.get('process')
            if p:
                print(f"Cancelling active process for task {task_id} (PID: {p.pid})")
                kill_process_tree(p.pid)
                
    return jsonify({"success": True})

@app.route('/api/queue', methods=['GET'])
def get_queue():
    # Return all tasks, removing 'process' handle
    task_list = []
    for t_id, task in tasks.items():
        t_copy = {k: v for k, v in task.items() if k != 'process'}
        task_list.append(t_copy)
    return jsonify(task_list)

@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400
        
    try:
        result = ai_helper.analyze_video(url)
        # Convert embedding arrays to none for frontend transmission to save bandwidth
        if "chunks" in result:
            for c in result["chunks"]:
                if "embedding" in c:
                    del c["embedding"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/ai/search', methods=['POST'])
def ai_search():
    data = request.json
    url = data.get('url')
    query = data.get('query')
    if not url or not query:
        return jsonify({"error": "URL and Query are required"}), 400
        
    try:
        results = ai_helper.query_transcript(url, query)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# SSE Streaming Setup
def sse_stream():
    q = queue.Queue()
    listeners.append(q)
    try:
        # Send initial full state
        initial_tasks = []
        for t_id, task in tasks.items():
            t_copy = {k: v for k, v in task.items() if k != 'process'}
            initial_tasks.append(t_copy)
        yield f"data: {json.dumps({'type': 'init', 'data': initial_tasks})}\n\n"
        
        while True:
            try:
                msg = q.get(timeout=15.0)
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield "data: {\"type\": \"ping\"}\n\n"
    finally:
        listeners.remove(q)

@app.route('/api/stream')
def get_stream():
    return Response(sse_stream(), mimetype="text/event-stream")

@app.route('/api/remove', methods=['POST'])
def remove_task():
    data = request.json
    task_id = data.get('task_id')
    if not task_id:
        return jsonify({"error": "Task ID is required"}), 400
    if task_id in tasks:
        if tasks[task_id]['status'] in ['completed', 'cancelled', 'failed']:
            del tasks[task_id]
            # Notify listeners of removal
            notify_listeners("task_update", {"id": task_id, "status": "removed"})
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Cannot remove active task"}), 400
    return jsonify({"error": "Task not found"}), 404

if __name__ == '__main__':
    # Make sure static directory exists
    os.makedirs(STATIC_DIR, exist_ok=True)
    # Start flask server
    app.run(host=APP_CONFIG.host, port=APP_CONFIG.port, debug=False)
