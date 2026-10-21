"""OpenClip on Modal: the public demo.

    modal deploy deploy/modal_app.py      # from the repo root

Two parts sharing the "openclip-jobs" volume at /jobs:
  * ui      - CPU container: Turnstile gate + reverse proxy (deploy/web.py) in front of the
              Streamlit app in hosted mode (OPENCLIP_HOSTED=1). One container: Streamlit
              sessions are sticky.
  * Clipper - L4 GPU container that runs one job at a time: faster-whisper on CUDA,
              qwen3:8b served by Ollama inside the container, OpenCV tracking, FFmpeg render.
              Scales to zero two minutes after the last job.

YouTube blocks downloads from data-centre IPs, so the hosted app takes uploaded files.
Turnstile keys come from the Modal secret "openclip-turnstile" (TURNSTILE_SITE_KEY,
TURNSTILE_SECRET_KEY) for the "openclip" widget, whose hostname list has this app's host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = "/root/openclip-ai"
OLLAMA_MODEL = "qwen3:8b"
WHISPER_MODEL = "small"
FONT_URL = "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Black.ttf"

jobs = modal.Volume.from_name("openclip-jobs", create_if_missing=True)
app = modal.App("openclip")


def _ignore(p: Path) -> bool:
    return any(part in {"__pycache__", ".venv", "workspace", ".streamlit", "tests"} for part in p.parts)


def _fetch_whisper() -> None:
    from faster_whisper import WhisperModel
    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


worker_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "curl", "zstd", "ca-certificates", "fonts-dejavu-core")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .run_commands(f"(ollama serve > /tmp/ollama.log 2>&1 &) && sleep 8 && ollama pull {OLLAMA_MODEL}")
    .pip_install("faster-whisper>=1.1.0", "litellm>=1.60.0", "pydantic>=2.7", "numpy>=1.24",
                 "opencv-contrib-python-headless>=4.9", "yt-dlp>=2025.10.22",
                 "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
    .env({"LD_LIBRARY_PATH": "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
                             "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib",
          "OLLAMA_HOST": "http://127.0.0.1:11434", "OLLAMA_KEEP_ALIVE": "-1"})
    .run_function(_fetch_whisper)
    .run_commands(f"mkdir -p {APP_DIR}/fonts && curl -fsSL -o {APP_DIR}/fonts/Montserrat-Black.ttf {FONT_URL}")
    .add_local_dir(ROOT / "openclip", f"{APP_DIR}/openclip", ignore=_ignore)
    .add_local_dir(ROOT / "models", f"{APP_DIR}/models")
)

ui_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install("streamlit>=1.50", "fastapi[standard]", "httpx", "websockets>=13")
    .env({"OPENCLIP_HOSTED": "1"})
    .add_local_file(ROOT / "app.py", f"{APP_DIR}/app.py")
    .add_local_file(ROOT / "deploy" / "streamlit_config.toml", f"{APP_DIR}/.streamlit/config.toml")
    .add_local_file(ROOT / "openclip" / "hosted.py", f"{APP_DIR}/openclip/hosted.py")
    .add_local_file(ROOT / "deploy" / "web.py", f"{APP_DIR}/web.py")
    .add_local_dir(ROOT / "deploy" / "sample", "/root/sample")
)


@app.cls(image=worker_image, gpu="L4", cpu=8.0, memory=16384, timeout=1800, max_containers=1,
         scaledown_window=120, volumes={"/jobs": jobs})
class Clipper:
    @modal.enter()
    def start(self):
        import sys
        import urllib.request
        sys.path.insert(0, APP_DIR)
        os.chdir(APP_DIR)
        self.ollama = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        # load the model onto the GPU now rather than during the first job
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", method="POST",
                                     data=f'{{"model":"{OLLAMA_MODEL}","prompt":"","keep_alive":-1}}'.encode())
        urllib.request.urlopen(req, timeout=300).read()

    @modal.exit()
    def stop(self):
        self.ollama.terminate()

    @modal.method()
    def run(self, job_id: str, upload_name: str, title: str, opts: dict):
        import queue
        import threading

        from openclip.pipeline import Settings, run_pipeline_on_file

        jobs.reload()
        _sweep_old_jobs()
        job_dir = f"/jobs/{job_id}"
        upload = os.path.join(job_dir, upload_name)
        if not os.path.exists(upload):
            yield ("error", "The upload didn't reach the worker. Please try again.")
            return

        settings = Settings(whisper_model=WHISPER_MODEL, provider="ollama", ollama_model=OLLAMA_MODEL,
                            max_clips=int(opts.get("max_clips", 3)), min_duration=float(opts.get("min_d", 30)),
                            max_duration=float(opts.get("max_d", 60)), layout=opts.get("layout", "auto"),
                            workspace="/jobs")
        events: queue.Queue = queue.Queue()

        def work():
            try:
                summary = run_pipeline_on_file(upload, settings, title=title, job_dir=job_dir,
                                               log=lambda m: events.put(("log", m)),
                                               progress=lambda p: events.put(("progress", p)))
                events.put(("done", summary))
            except Exception as exc:
                events.put(("error", str(exc) or exc.__class__.__name__))

        threading.Thread(target=work, daemon=True).start()
        while True:
            kind, payload = events.get()
            if kind in ("done", "error"):
                # keep only the rendered clips: drop the visitor's source video and its audio
                for name in (upload_name, "audio.wav"):
                    try:
                        os.remove(os.path.join(job_dir, name))
                    except OSError:
                        pass
                jobs.commit()
                yield (kind, payload)
                return
            yield (kind, payload)


def _sweep_old_jobs(max_age_s: float = 24 * 3600) -> None:
    now = time.time()
    for d in Path("/jobs").iterdir():
        if d.is_dir() and now - d.stat().st_mtime > max_age_s:
            shutil.rmtree(d, ignore_errors=True)


@app.function(image=ui_image, cpu=2.0, memory=4096, max_containers=1, scaledown_window=600, timeout=3600,
              volumes={"/jobs": jobs}, secrets=[modal.Secret.from_name("openclip-turnstile")])
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def ui():
    import sys
    import urllib.request
    sys.path.insert(0, APP_DIR)
    subprocess.Popen(["streamlit", "run", "app.py", "--server.address", "127.0.0.1", "--server.port", "8501",
                      "--server.headless", "true"], cwd=APP_DIR)
    for _ in range(120):
        try:
            urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    import web
    return web.build_app(os.environ["TURNSTILE_SITE_KEY"], os.environ["TURNSTILE_SECRET_KEY"])
