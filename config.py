"""Runtime configuration for YT-DLP Studio.

Every value is environment driven so the same image can run locally (open,
loopback bound) or on a host (token protected, 0.0.0.0 bound) without code changes.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

# yt-dlp format spec used when the caller does not ask for anything specific.
# "bestvideo*+bestaudio/best" merges the best separate streams, which is the only
# way to get >360p out of YouTube. A bare "best" resolves to a pre-merged stream.
FORMAT_BEST = "bv*+ba/b"
FORMAT_AUDIO = "ba"

# Characters permitted in a caller supplied yt-dlp format expression.
FORMAT_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.*+/-[]()<>=:,")


def _expand_path(value):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value)))


def _path_from_env(env, name, default):
    return _expand_path(env.get(name, default))


def _int_from_env(env, name, default):
    raw_value = env.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _float_from_env(env, name, default):
    raw_value = env.get(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def _bool_from_env(env, name, default):
    val = env.get(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _path_list_from_env(env, name):
    raw = env.get(name) or ""
    items = []
    for chunk in raw.replace(";", os.pathsep).split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            items.append(_expand_path(chunk))
    return items


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    default_save_dir: str
    cache_dir: str
    temp_dir: str
    yt_dlp_bin: str
    ffmpeg_bin: str
    hf_home: str
    hf_hub_cache: str
    transformers_cache: str
    use_hf_inference_api: bool
    hf_token: str
    allowed_save_dirs: tuple = ()
    app_token: str = ""
    secret_key: str = ""
    max_task_history: int = 200
    clip_cut_mode: str = "precise"
    ai_chunk_seconds: int = 60
    ai_importance_threshold: float = 0.4
    ai_max_sections: int = 8
    ai_min_section_seconds: float = 15.0
    ai_max_section_seconds: float = 240.0
    ai_cache_ttl_seconds: int = 7 * 24 * 3600
    ai_idle_unload_seconds: int = 900
    ai_nli_model: str = "facebook/bart-large-mnli"
    ai_summary_model: str = "facebook/bart-large-cnn"


def create_app_config(env=None):
    env = env or os.environ
    default_cache_dir = str(BASE_DIR / ".cache")
    default_temp_dir = str(BASE_DIR / ".tmp")
    hf_home = _path_from_env(env, "HF_HOME", os.path.join(default_cache_dir, "huggingface"))
    default_save_dir = _path_from_env(
        env,
        "DEFAULT_SAVE_DIR",
        os.path.join(os.path.expanduser("~"), "Downloads"),
    )

    # Downloads may only ever be written inside these roots (see C1 in ANALYSIS.md).
    allowed = [default_save_dir]
    for extra in _path_list_from_env(env, "EXTRA_SAVE_DIRS"):
        if extra not in allowed:
            allowed.append(extra)

    app_token = env.get("APP_TOKEN", "") or ""
    # Deriving the session key from the token keeps sessions valid across
    # restarts without asking the operator to manage a second secret.
    secret_key = env.get("SECRET_KEY") or hashlib.sha256(app_token.encode("utf-8")).hexdigest()

    return AppConfig(
        host=env.get("HOST", "127.0.0.1"),
        port=_int_from_env(env, "PORT", 5000),
        default_save_dir=default_save_dir,
        cache_dir=_path_from_env(env, "APP_CACHE_DIR", default_cache_dir),
        temp_dir=_path_from_env(env, "APP_TEMP_DIR", default_temp_dir),
        allowed_save_dirs=tuple(allowed),
        yt_dlp_bin=env.get("YT_DLP_BIN", "yt-dlp"),
        ffmpeg_bin=env.get("FFMPEG_BIN", "ffmpeg"),
        hf_home=hf_home,
        hf_hub_cache=_path_from_env(env, "HF_HUB_CACHE", os.path.join(hf_home, "hub")),
        transformers_cache=_path_from_env(
            env,
            "TRANSFORMERS_CACHE",
            os.path.join(hf_home, "transformers"),
        ),
        use_hf_inference_api=_bool_from_env(env, "USE_HF_INFERENCE_API", False),
        hf_token=env.get("HF_TOKEN") or env.get("HF_API_KEY") or "",
        app_token=app_token,
        secret_key=secret_key,
        max_task_history=_int_from_env(env, "MAX_TASK_HISTORY", 200),
        clip_cut_mode=(env.get("CLIP_CUT_MODE", "precise") or "precise").lower(),
        ai_chunk_seconds=_int_from_env(env, "AI_CHUNK_SECONDS", 60),
        ai_importance_threshold=_float_from_env(env, "AI_IMPORTANCE_THRESHOLD", 0.4),
        ai_max_sections=_int_from_env(env, "AI_MAX_SECTIONS", 8),
        ai_min_section_seconds=_float_from_env(env, "AI_MIN_SECTION_SECONDS", 15.0),
        ai_max_section_seconds=_float_from_env(env, "AI_MAX_SECTION_SECONDS", 240.0),
        ai_cache_ttl_seconds=_int_from_env(env, "AI_CACHE_TTL_SECONDS", 7 * 24 * 3600),
        ai_idle_unload_seconds=_int_from_env(env, "AI_IDLE_UNLOAD_SECONDS", 900),
        ai_nli_model=env.get("AI_NLI_MODEL", "facebook/bart-large-mnli"),
        ai_summary_model=env.get("AI_SUMMARY_MODEL", "facebook/bart-large-cnn"),
    )


def ensure_runtime_dirs(config):
    for directory in (
        config.default_save_dir,
        config.cache_dir,
        config.temp_dir,
        config.hf_home,
        config.hf_hub_cache,
        config.transformers_cache,
    ):
        os.makedirs(directory, exist_ok=True)


def apply_runtime_environment(config):
    os.environ.setdefault("HF_HOME", config.hf_home)
    os.environ.setdefault("HF_HUB_CACHE", config.hf_hub_cache)
    os.environ.setdefault("TRANSFORMERS_CACHE", config.transformers_cache)


def resolve_save_dir(save_dir, config):
    """Resolve a caller supplied output directory, or None if it is not allowed.

    Downloads must stay inside one of the configured roots: the API is
    unauthenticated by default, and without this check any caller could ask the
    server to create directories and fetch remote content anywhere on the disk.
    """
    candidate = _expand_path(save_dir or config.default_save_dir)
    for root in config.allowed_save_dirs:
        root = _expand_path(root)
        if candidate == root:
            return candidate
        if candidate.startswith(root + os.sep):
            return candidate
    return None
