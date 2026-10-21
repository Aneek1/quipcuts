"""Quipcuts link downloader: runs on a home PC because YouTube blocks cloud servers.

It only makes outgoing requests, so nothing on the PC is exposed: every few seconds it asks the
API for a waiting YouTube link, downloads it with yt-dlp (<=1080p MP4), uploads the file to the
signed R2 URL it was given and reports back. The GPU pipeline on Modal takes it from there.

    API_URL=https://aneek1--quipcuts-api.us-east.modal.run FETCHER_TOKEN=... python fetcher/agent.py

Normally run with Docker: see fetcher/compose.yaml.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
import traceback

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openclip.downloader import download_youtube_video  # noqa: E402

API_URL = os.environ.get("API_URL", "https://aneek1--quipcuts-api.us-east.modal.run").rstrip("/")
TOKEN = os.environ["FETCHER_TOKEN"]
FETCHER_ID = os.environ.get("FETCHER_ID") or socket.gethostname()[:60]
POLL_S = float(os.environ.get("POLL_S", "5"))

http = requests.Session()
http.headers["X-Fetcher-Token"] = TOKEN


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def api(method: str, path: str, **kw) -> requests.Response:
    r = http.request(method, API_URL + path, timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
    return r


def report(pid: str, line: str) -> None:
    try:
        api("POST", f"/api/fetcher/{pid}/log", json={"line": line})
    except Exception as exc:  # progress lines are nice to have, never fatal
        log(f"  (couldn't send progress: {exc})")


def friendly(exc: Exception) -> str:
    text = str(exc)
    if "Sign in to confirm" in text:
        return "YouTube asked the downloader to sign in, so this video can't be fetched right now."
    if "Private video" in text or "members-only" in text.lower():
        return "This video is private or members-only."
    if "age" in text.lower() and "confirm" in text.lower():
        return "This video is age-restricted, so it can't be downloaded."
    if "is not available" in text or "Video unavailable" in text:
        return "YouTube says this video isn't available."
    return "The download from YouTube failed. Try again, or upload the file instead."


def handle(job: dict) -> None:
    pid, url = job["project_id"], job["url"]
    log(f"job {pid}: {url}")
    work = tempfile.mkdtemp(prefix="quipcuts_fetch_")
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False, process=False) or {}
        duration = float(info.get("duration") or 0)
        title = info.get("title") or "YouTube video"
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise ValueError("Live streams can't be clipped until they've finished.")
        if duration > job["max_video_s"] + 1:
            raise ValueError(f"That video is {duration / 60:.0f} minutes long; the free beta takes up to "
                             f"{job['max_video_s'] // 60} minutes.")
        report(pid, f"Found '{title}' ({duration / 60:.1f} min). Downloading at up to 1080p…")

        t0 = time.time()
        # YouTube now and then answers a format request with 403; a fresh attempt usually works
        for attempt in range(1, 4):
            try:
                media = download_youtube_video(url, work)
                break
            except Exception as exc:
                if "403" not in str(exc) or attempt == 3:
                    raise
                report(pid, f"YouTube refused the download (403), retrying ({attempt}/2)…")
                for name in os.listdir(work):
                    os.remove(os.path.join(work, name))
                time.sleep(3 * attempt)
        path = media["video_path"]
        size = os.path.getsize(path)
        if size > job["max_upload_mb"] * 1024 * 1024:
            raise ValueError(f"The downloaded file is {size / 1048576:.0f} MB, over the {job['max_upload_mb']} MB limit.")
        report(pid, f"Downloaded {media['width']}x{media['height']}, {size / 1048576:.0f} MB in "
                    f"{time.time() - t0:.0f}s. Uploading…")

        with open(path, "rb") as f:
            r = requests.put(job["upload_url"], data=f, headers={"Content-Type": job["content_type"]}, timeout=1800)
        r.raise_for_status()
        api("POST", f"/api/fetcher/{pid}/done", json={"ok": True, "title": title})
        log(f"job {pid}: done ({size / 1048576:.0f} MB)")
    except ValueError as exc:
        api("POST", f"/api/fetcher/{pid}/done", json={"ok": False, "error": str(exc)})
        log(f"job {pid}: rejected: {exc}")
    except Exception as exc:
        traceback.print_exc()
        try:
            api("POST", f"/api/fetcher/{pid}/done", json={"ok": False, "error": friendly(exc)})
        except Exception:
            traceback.print_exc()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    log(f"Quipcuts link downloader '{FETCHER_ID}' polling {API_URL} every {POLL_S:.0f}s")
    errors = 0
    while True:
        try:
            r = api("POST", "/api/fetcher/claim", json={"fetcher_id": FETCHER_ID})
            errors = 0
            if r.status_code == 200:
                handle(r.json())
                continue  # check for the next link straight away
        except Exception as exc:
            errors += 1
            if errors in (1, 10) or errors % 100 == 0:
                log(f"can't reach the API ({errors}x): {exc}")
        time.sleep(POLL_S if errors == 0 else min(60, POLL_S * (1 + errors)))


if __name__ == "__main__":
    main()
