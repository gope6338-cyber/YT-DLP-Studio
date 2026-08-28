"""
Vercel serverless entrypoint.

All code this function needs lives inside api/ (the only directory tree the
Vercel Python runtime bundles). Shared modules sit in api/_shared/ — the
underscore prefix keeps Vercel from treating them as separate functions.

Before importing the backend we:
1. point all writable directories at /tmp (the only writable path on Vercel),
2. put api/_shared on sys.path so `serverless_backend`, `config`,
   `media_info` and `ai_helper` resolve exactly as they do when self-hosted.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(_HERE, "_shared")

_SERVERLESS_DEFAULTS = {
    "DEFAULT_SAVE_DIR": "/tmp/downloads",
    "APP_CACHE_DIR": "/tmp/cache",
    "APP_TEMP_DIR": "/tmp/tmp",
    "HF_HOME": "/tmp/cache/huggingface",
}
for _key, _value in _SERVERLESS_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from serverless_backend import app  # noqa: E402,F401
