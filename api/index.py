"""
Vercel serverless entrypoint.

Every request to the site is rewritten to /api/index (see vercel.json), which
maps to this file. Vercel's Python runtime mounts the exported Flask `app`.

Before importing the backend we:
1. point all writable directories at /tmp (the only writable path on Vercel),
2. add the project root to sys.path so shared root modules
   (serverless_backend, media_info, config, ai_helper) resolve.
"""

import os
import sys

_SERVERLESS_DEFAULTS = {
    "DEFAULT_SAVE_DIR": "/tmp/downloads",
    "APP_CACHE_DIR": "/tmp/cache",
    "APP_TEMP_DIR": "/tmp/tmp",
    "HF_HOME": "/tmp/cache/huggingface",
}
for _key, _value in _SERVERLESS_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from serverless_backend import app  # noqa: E402,F401
