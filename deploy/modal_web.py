"""Quipcuts on Modal: the API behind https://quipcuts.app plus the GPU worker.

    modal deploy deploy/modal_web.py      # from the repo root

  * api    - CPU container running server.main (FastAPI). Stateless: projects live in Neon
             Postgres, files in Cloudflare R2, so it can scale out.
  * Worker - L4 GPU container, one job at a time (max_containers=1; Modal queues the rest).
             Runs the full pipeline for new projects and re-renders edited clips.
  * sweep  - daily: deletes projects older than OPENCLIP_KEEP_DAYS.

Settings come from the Modal secret "quipcuts" (DATABASE_URL, R2_*, CLERK_PUBLISHABLE_KEY),
created with deploy/set_quipcuts_secrets.py. The old Streamlit demo is deploy/modal_app.py.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = "/root/app"
OLLAMA_MODEL = "qwen3:8b"
WHISPER_MODEL = "small"
FONT_URL = "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Black.ttf"
WEB_ORIGIN = "https://quipcuts.app"

ENV = {
    "STORAGE": "r2", "RUNNER": "modal", "MODAL_APP": "quipcuts", "OPENCLIP_DEV_AUTH": "0",
    "WEB_ORIGINS": f"{WEB_ORIGIN},https://www.quipcuts.app",
    "OPENCLIP_PROVIDER": "ollama", "OPENCLIP_OLLAMA_MODEL": OLLAMA_MODEL, "OPENCLIP_WHISPER_MODEL": WHISPER_MODEL,
    "OPENCLIP_DATA_DIR": "/tmp/quipcuts", "PYTHONPATH": APP_DIR,
}
SERVER_PIP = ["fastapi[standard]>=0.115", "sqlmodel>=0.0.22", "psycopg[binary]>=3.2", "pyjwt[crypto]>=2.9",
              "boto3>=1.35", "python-multipart>=0.0.9", "httpx>=0.27", "pydantic>=2.7"]

app = modal.App("quipcuts")
secret = modal.Secret.from_name("quipcuts")
fetcher_secret = modal.Secret.from_name("quipcuts-fetcher")  # deploy/set_fetcher_token.py


def _ignore(p: Path) -> bool:
    return any(part in {"__pycache__", ".venv", "workspace", "data", "tests"} for part in p.parts)


def _fetch_whisper() -> None:
    from faster_whisper import WhisperModel
    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


api_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(*SERVER_PIP)
    # Clerk production instance; overrides the issuer derived from CLERK_PUBLISHABLE_KEY in the secret
    .env({**ENV, "CLERK_ISSUER": "https://clerk.quipcuts.app"})
    # the API only needs the recipe model; the pipeline modules stay in the worker
    .add_local_file(ROOT / "openclip" / "__init__.py", f"{APP_DIR}/openclip/__init__.py")
    .add_local_file(ROOT / "openclip" / "recipe.py", f"{APP_DIR}/openclip/recipe.py")
    .add_local_file(ROOT / "openclip" / "renderer.py", f"{APP_DIR}/openclip/renderer.py")
    .add_local_dir(ROOT / "server", f"{APP_DIR}/server", ignore=_ignore)
)

worker_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "curl", "zstd", "ca-certificates", "fonts-dejavu-core")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .run_commands(f"(ollama serve > /tmp/ollama.log 2>&1 &) && sleep 8 && ollama pull {OLLAMA_MODEL}")
    .pip_install("faster-whisper>=1.1.0", "litellm>=1.60.0", "numpy>=1.24", "opencv-contrib-python-headless>=4.9",
                 "yt-dlp>=2025.10.22", "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*", *SERVER_PIP)
    .env({**ENV,
          "LD_LIBRARY_PATH": "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
                             "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib",
          "OLLAMA_HOST": "http://127.0.0.1:11434", "OLLAMA_KEEP_ALIVE": "-1"})
    .run_function(_fetch_whisper)
    .run_commands(f"mkdir -p {APP_DIR}/fonts && curl -fsSL -o {APP_DIR}/fonts/Montserrat-Black.ttf {FONT_URL}")
    .add_local_dir(ROOT / "openclip", f"{APP_DIR}/openclip", ignore=_ignore)
    .add_local_dir(ROOT / "models", f"{APP_DIR}/models")
    .add_local_dir(ROOT / "server", f"{APP_DIR}/server", ignore=_ignore)
)


@app.cls(image=worker_image, gpu="L4", cpu=8.0, memory=16384, timeout=1800, max_containers=1,
         scaledown_window=120, secrets=[secret])
class Worker:
    @modal.enter()
    def start(self):
        import urllib.request
        os.chdir(APP_DIR)
        self.ollama = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", method="POST",
                                     data=f'{{"model":"{OLLAMA_MODEL}","prompt":"","keep_alive":-1}}'.encode())
        urllib.request.urlopen(req, timeout=300).read()

    @modal.exit()
    def stop(self):
        self.ollama.terminate()

    @modal.method()
    def run(self, kind: str, item_id: str):
        from server.worker import run_job
        run_job(kind, item_id)


@app.function(image=api_image, secrets=[secret, fetcher_secret], cpu=1.0, memory=1024, scaledown_window=300)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def api():
    from server.db import init_db
    from server.main import app as fastapi_app
    init_db()
    return fastapi_app


@app.function(image=api_image, secrets=[secret], schedule=modal.Cron("17 3 * * *"))
def sweep():
    from server.worker import sweep_expired
    sweep_expired()
