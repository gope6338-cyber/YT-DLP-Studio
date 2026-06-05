import os
import re
import json
import glob
import tempfile
import gc
import urllib.request
import urllib.error
from config import apply_runtime_environment, create_app_config, ensure_runtime_dirs

# Global caching for models
_embeddings_tokenizer = None
_embeddings_model = None
_classifier = None
_summarizer = None

APP_CONFIG = create_app_config()
apply_runtime_environment(APP_CONFIG)
ensure_runtime_dirs(APP_CONFIG)
CACHE_DIR = APP_CONFIG.cache_dir


def call_hf_api(url, payload, token=None, retries=5, delay=5):
    import time
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
        except Exception as e:
            if attempt == retries - 1:
                raise e
            time.sleep(delay)
    raise Exception("API call failed after max retries")


def get_embeddings_model():
    global _embeddings_tokenizer, _embeddings_model
    if _embeddings_model is None:
        from transformers import AutoTokenizer, AutoModel
        model_name = "sentence-transformers/all-MiniLM-L6-v2"
        _embeddings_tokenizer = AutoTokenizer.from_pretrained(model_name)
        _embeddings_model = AutoModel.from_pretrained(model_name)
    return _embeddings_tokenizer, _embeddings_model

def get_classifier():
    global _classifier
    if _classifier is None:
        from transformers import pipeline
        model_name = "facebook/bart-large-mnli"
        _classifier = pipeline("zero-shot-classification", model=model_name, device=-1)
    return _classifier

def get_summarizer():
    global _summarizer
    if _summarizer is None:
        from transformers import pipeline
        model_name = "facebook/bart-large-cnn"
        _summarizer = pipeline("summarization", model=model_name, device=-1)
    return _summarizer

def unload_models():
    global _embeddings_tokenizer, _embeddings_model, _classifier, _summarizer
    if _embeddings_model is not None or _classifier is not None or _summarizer is not None:
        _embeddings_tokenizer = None
        _embeddings_model = None
        _classifier = None
        _summarizer = None
        try:
            gc.collect()
        except Exception:
            pass

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

def parse_subtitles(file_path):
    """
    Parses VTT or SRT files into timestamped caption blocks.
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
            
    return entries

def download_subtitles(video_url):
    """
    Downloads manual/auto subtitles for a video and returns parsed entries.
    """
    import yt_dlp
    
    # We will download in a temporary directory
    temp_dir = tempfile.mkdtemp(dir=APP_CONFIG.temp_dir)
    out_tmpl = os.path.join(temp_dir, 'subtitle')
    
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['en'],
        'skip_download': True,
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        # Search for subtitles files in the temp directory
        subtitle_files = glob.glob(out_tmpl + ".*")
        if not subtitle_files:
            return []
            
        # Select the best subtitle file (prefer manual over auto, srt/vtt)
        sub_file = subtitle_files[0]
        entries = parse_subtitles(sub_file)
        
        # Clean up files
        for f in subtitle_files:
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass
            
        return entries
    except Exception as e:
        print(f"Error downloading subtitles: {e}")
        return []

def extract_video_id(url):
    """
    Extracts video ID from standard YouTube URL.
    """
    # Handles v=ID, embed/ID, share links, etc.
    patterns = [
        r'(?:v=|\/embed\/|\/101\/|\/v\/|youtu\.be\/|\/shorts\/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Default fallback: hash of the url
    import hashlib
    return hashlib.md_str(url.encode('utf-8')).hexdigest() if hasattr(hashlib, 'md_str') else hashlib.md5(url.encode('utf-8')).hexdigest()

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

def get_embedding(text, tokenizer=None, model=None):
    if APP_CONFIG.use_hf_inference_api:
        url = "https://api-inference.huggingface.co/pipeline/feature-extraction/sentence-transformers/all-MiniLM-L6-v2"
        res = call_hf_api(url, {"inputs": text}, APP_CONFIG.hf_token)
        if isinstance(res, list):
            if len(res) > 0 and isinstance(res[0], list):
                return res[0]
            return res
        raise ValueError(f"Unexpected response format from HF API: {res}")
    
    import torch
    inputs = tokenizer(text, padding=True, truncation=True, return_tensors="pt")
    with torch.no_grad():
        outputs = model(**inputs)
    attention_mask = inputs['attention_mask']
    token_embeddings = outputs[0]
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    sum_embeddings = torch.sum(token_embeddings * input_mask_expanded, 1)
    sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
    embedding = sum_embeddings / sum_mask
    embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
    return embedding[0].tolist()

def analyze_video(video_url):
    """
    Downloads subtitles, chunks them, generates embeddings, scores importance,
    groups important segments into sections, and caches the result.
    """
    video_id = extract_video_id(video_url)
    cache_file = os.path.join(CACHE_DIR, f"{video_id}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
            
    # Step 1: Download subtitles
    entries = download_subtitles(video_url)
    if not entries:
        return {"chunks": [], "sections": [], "error": "No subtitles found for this video"}
        
    # Step 2: Create chunks of ~60 seconds
    chunks = chunk_transcript(entries, chunk_duration=60)
    
    # Step 3: Load models and analyze chunks
    if APP_CONFIG.use_hf_inference_api:
        tok, model = None, None
        classifier = None
        summarizer = None
    else:
        tok, model = get_embeddings_model()
        classifier = get_classifier()
        summarizer = get_summarizer()
    
    # Generate embeddings and scores for each chunk
    candidate_labels = ["highlight insight", "filler chat", "tutorial process"]
    
    important_chunks = []
    for i, chunk in enumerate(chunks):
        # Generate embedding
        chunk['embedding'] = get_embedding(chunk['text'], tok, model)
        
        # Run classifier
        try:
            if APP_CONFIG.use_hf_inference_api:
                url = "https://api-inference.huggingface.co/models/facebook/bart-large-mnli"
                res = call_hf_api(url, {"inputs": chunk['text'], "parameters": {"candidate_labels": candidate_labels}}, APP_CONFIG.hf_token)
            else:
                res = classifier(chunk['text'], candidate_labels=candidate_labels)
            scores = dict(zip(res['labels'], res['scores']))
            importance = scores.get("highlight insight", 0) + scores.get("tutorial process", 0)
        except Exception as e:
            print(f"Classifier error: {e}")
            importance = 0.5
            
        chunk['importance'] = importance
        chunk['id'] = i
        
        # High importance threshold
        if importance >= 0.4:
            important_chunks.append(chunk)
            
    # Step 4: Create sections by merging adjacent important chunks
    sections = []
    if important_chunks:
        current_section_chunks = [important_chunks[0]]
        
        for c in important_chunks[1:]:
            last_c = current_section_chunks[-1]
            # Merge if adjacent (gap of at most 1 chunk in indices)
            if c['id'] - last_c['id'] <= 2 and (c['end'] - current_section_chunks[0]['start'] <= 300):
                current_section_chunks.append(c)
            else:
                # Close section
                sec_text = " ".join([ch['text'] for ch in current_section_chunks]).strip()
                sec_start = current_section_chunks[0]['start']
                sec_end = current_section_chunks[-1]['end']
                
                # Summarize section
                try:
                    if APP_CONFIG.use_hf_inference_api:
                        url = "https://api-inference.huggingface.co/models/facebook/bart-large-cnn"
                        res = call_hf_api(url, {"inputs": sec_text, "parameters": {"max_length": 60, "min_length": 20, "do_sample": False}}, APP_CONFIG.hf_token)
                        summary = res[0]['summary_text']
                    else:
                        summary = summarizer(sec_text, max_length=60, min_length=20, do_sample=False)[0]['summary_text']
                except Exception as e:
                    print(f"Summarizer error: {e}")
                    summary = sec_text[:100] + "..."
                    
                sections.append({
                    'start': sec_start,
                    'end': sec_end,
                    'label': summary
                })
                current_section_chunks = [c]
                
        # Handle last section
        if current_section_chunks:
            sec_text = " ".join([ch['text'] for ch in current_section_chunks]).strip()
            sec_start = current_section_chunks[0]['start']
            sec_end = current_section_chunks[-1]['end']
            try:
                if APP_CONFIG.use_hf_inference_api:
                    url = "https://api-inference.huggingface.co/models/facebook/bart-large-cnn"
                    res = call_hf_api(url, {"inputs": sec_text, "parameters": {"max_length": 60, "min_length": 20, "do_sample": False}}, APP_CONFIG.hf_token)
                    summary = res[0]['summary_text']
                else:
                    summary = summarizer(sec_text, max_length=60, min_length=20, do_sample=False)[0]['summary_text']
            except Exception as e:
                print(f"Summarizer error: {e}")
                summary = sec_text[:100] + "..."
            sections.append({
                'start': sec_start,
                'end': sec_end,
                'label': summary
            })
            
    # Unload models to free memory on the host machine
    unload_models()
    
    result = {
        "video_url": video_url,
        "video_id": video_id,
        "chunks": chunks,
        "sections": sections
    }
    
    # Save cache
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
        
    return result

def query_transcript(video_url, query_text):
    """
    Computes embedding for query, and ranks chunks by cosine similarity.
    Returns ranked list of chunks.
    """
    video_id = extract_video_id(video_url)
    cache_file = os.path.join(CACHE_DIR, f"{video_id}.json")
    
    if not os.path.exists(cache_file):
        # Trigger full analysis if cache doesn't exist
        data = analyze_video(video_url)
    else:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
    chunks = data.get("chunks", [])
    if not chunks:
        return []
        
    tok, model = None, None
    if not APP_CONFIG.use_hf_inference_api:
        tok, model = get_embeddings_model()
    query_emb = get_embedding(query_text, tok, model)
    unload_models()
    
    results = []
    for chunk in chunks:
        emb = chunk.get("embedding")
        if not emb:
            continue
        # Cosine similarity is dot product of normalized vectors
        sim = sum(q * e for q, e in zip(query_emb, emb))
        results.append({
            'start': chunk['start'],
            'end': chunk['end'],
            'text': chunk['text'],
            'similarity': sim
        })
        
    # Sort by similarity descending
    results.sort(key=lambda x: x['similarity'], reverse=True)
    return results
