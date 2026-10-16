FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DEFAULT_SAVE_DIR=/data/downloads \
    APP_CACHE_DIR=/data/cache \
    APP_TEMP_DIR=/data/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

# Persistent state lives under /data, which should be a mounted volume.
# Creating and chowning it here means a fresh named volume inherits these
# permissions, so the app can keep running as a non-root user.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/downloads /data/cache /data/tmp \
    && chown -R app:app /data /app

USER app

EXPOSE 8000

CMD ["sh", "./start.sh"]
