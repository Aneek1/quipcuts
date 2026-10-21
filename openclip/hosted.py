"""Hosted mode (OPENCLIP_HOSTED=1): the public demo on Modal.

The Streamlit UI runs on a small CPU container; each job runs on a GPU container
(deploy/modal_app.py, class Clipper). They share the "openclip-jobs" volume at /jobs:
the UI writes the upload, the worker writes the clips. Kept free of the pipeline's
heavy imports (whisper, OpenCV, yt-dlp) so the UI image stays small.

Usage caps live in a modal.Dict so they survive the UI container scaling to zero.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time

HOSTED = os.getenv("OPENCLIP_HOSTED") == "1"
APP_NAME = "openclip"
JOBS_DIR = "/jobs"
SAMPLE_DIR = os.getenv("OPENCLIP_SAMPLE_DIR", "/root/sample")
SAMPLE_ID = "sample"
MODEL = "qwen3:8b"

MAX_UPLOAD_MB = 500
MAX_VIDEO_S = 15 * 60
MAX_CLIPS = 5
PER_PASS_PER_DAY = 3      # jobs one visitor can run in a UTC day
PER_DAY = 25              # jobs across everyone in a UTC day (bounds the GPU bill)
KEEP_JOBS_S = 24 * 3600   # rendered clips are deleted after this

COOKIE = "openclip_pass"
EXTS = ["mp4", "mov", "m4v", "webm", "mkv"]


# ---------------------------------------------------------------- pass cookie (shared with deploy/web.py)
def pass_key(secret_key: str) -> bytes:
    return hashlib.sha256(b"openclip-pass:" + secret_key.encode()).digest()


def sign(key: bytes, sid: str, exp: int) -> str:
    return hmac.new(key, f"{sid}.{exp}".encode(), hashlib.sha256).hexdigest()


def check_pass(key: bytes, value: str | None, now: float | None = None) -> str | None:
    """The pass's session id if the cookie is genuine and unexpired, else None."""
    try:
        sid, exp_s, mac = (value or "").split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, sign(key, sid, exp)) or exp < (now or time.time()):
        return None
    return sid


def visitor_id(cookies) -> str | None:
    secret = os.getenv("TURNSTILE_SECRET_KEY")
    if not secret:
        return None
    return check_pass(pass_key(secret), cookies.get(COOKIE))


# ---------------------------------------------------------------- caps
def _usage():
    import modal
    return modal.Dict.from_name("openclip-usage", create_if_missing=True)


def take_job_slot(sid: str | None) -> str | None:
    """Reserve one job for this visitor. None if allowed, else the reason it isn't."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    usage = _usage()
    total = usage.get(f"day:{day}", 0)
    mine = usage.get(f"pass:{day}:{sid}", 0)
    if total >= PER_DAY:
        return "The demo has reached its daily limit. Please try again tomorrow."
    if mine >= PER_PASS_PER_DAY:
        return f"You've used today's {PER_PASS_PER_DAY} free runs. Please come back tomorrow."
    usage[f"day:{day}"] = total + 1
    usage[f"pass:{day}:{sid}"] = mine + 1
    return None


def runs_left(sid: str | None) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    usage = _usage()
    return max(0, min(PER_PASS_PER_DAY - usage.get(f"pass:{day}:{sid}", 0),
                      PER_DAY - usage.get(f"day:{day}", 0)))


# ---------------------------------------------------------------- jobs
def _volume():
    import modal
    return modal.Volume.from_name("openclip-jobs")


def new_job_id() -> str:
    return secrets.token_hex(6)


def probe_duration(path: str) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", path], capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def save_upload(job_id: str, name: str, data) -> str:
    """Write the upload into the shared volume. Returns its path. `data` is a file-like object."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "mp4"
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    path = os.path.join(job_dir, f"upload.{ext}")
    with open(path, "wb") as f:
        while chunk := data.read(8 << 20):
            f.write(chunk)
    return path


def commit() -> None:
    _volume().commit()


def submit(job_id: str, upload_name: str, title: str, opts: dict):
    """Run the job on the GPU worker; yields ("log", str) / ("progress", float) / ("done", dict) / ("error", str)."""
    import modal
    commit()
    clipper = modal.Cls.from_name(APP_NAME, "Clipper")()
    yield from clipper.run.remote_gen(job_id, upload_name, title, opts)
    _volume().reload()


def load_job(job_id: str) -> dict | None:
    if job_id == SAMPLE_ID:
        path = os.path.join(SAMPLE_DIR, "clips.json")
    elif job_id.isalnum() and len(job_id) <= 32:
        path = os.path.join(JOBS_DIR, job_id, "clips.json")
        if not os.path.exists(path):
            try:
                _volume().reload()
            except Exception:
                pass
    else:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    data["_mtime"] = os.path.getmtime(path)
    return data
