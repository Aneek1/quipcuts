#!/bin/bash
# Build and (re)start the Quipcuts link downloader with plain Docker (no Compose plugin needed).
#   wsl -d Ubuntu -- bash /mnt/c/Users/aneek.chattopadhyay/openclip-ai/fetcher/run.sh [--follow]
# --follow keeps this command attached to the logs. On WSL that also keeps the distro (and so
# Docker and the container) from shutting down when no other WSL window is open.
set -e
cd "$(dirname "$0")/.."

DOCKER_BUILDKIT=0 docker build -f fetcher/Dockerfile -t quipcuts-fetcher .
docker rm -f quipcuts-fetcher >/dev/null 2>&1 || true
docker run -d --name quipcuts-fetcher --restart unless-stopped \
  --env-file fetcher/.env \
  -e API_URL=https://aneek1--quipcuts-api.us-east.modal.run \
  -e FETCHER_ID=home-pc \
  --log-opt max-size=5m --log-opt max-file=2 \
  quipcuts-fetcher >/dev/null
echo "quipcuts-fetcher started"

if [ "$1" = "--follow" ]; then
  exec docker logs -f quipcuts-fetcher
fi
