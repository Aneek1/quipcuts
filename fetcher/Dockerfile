# Quipcuts link downloader. Build from the repo root (compose.yaml does this).
FROM python:3.12-slim

# yt-dlp needs FFmpeg to merge video+audio and a JS runtime to solve YouTube's challenges
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "yt-dlp[default]" requests

WORKDIR /app
COPY openclip/__init__.py openclip/downloader.py openclip/
COPY fetcher/agent.py fetcher/
# YouTube changes often; get the newest yt-dlp at each container start, not just at build time
CMD ["sh", "-c", "pip install -q -U --no-cache-dir 'yt-dlp[default]' || true; exec python -u fetcher/agent.py"]
