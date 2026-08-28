import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


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


def _bool_from_env(env, name, default):
    val = env.get(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


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


def create_app_config(env=None):
    env = env or os.environ
    default_cache_dir = str(BASE_DIR / ".cache")
    default_temp_dir = str(BASE_DIR / ".tmp")
    hf_home = _path_from_env(env, "HF_HOME", os.path.join(default_cache_dir, "huggingface"))

    return AppConfig(
        host=env.get("HOST", "127.0.0.1"),
        port=_int_from_env(env, "PORT", 5000),
        default_save_dir=_path_from_env(
            env,
            "DEFAULT_SAVE_DIR",
            os.path.join(os.path.expanduser("~"), "Downloads"),
        ),
        cache_dir=_path_from_env(env, "APP_CACHE_DIR", default_cache_dir),
        temp_dir=_path_from_env(env, "APP_TEMP_DIR", default_temp_dir),
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
