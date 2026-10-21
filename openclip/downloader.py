"""Module A - Ingestion: fetch a YouTube video with yt-dlp and extract a 16 kHz mono WAV."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor


class DownloadError(RuntimeError):
    pass


class FFmpegMissingError(DownloadError):
    """FFmpeg/ffprobe cannot be found - shown verbatim in the Streamlit UI."""


# Separate DASH video (<=1080p, MP4 container) + M4A audio, merged by FFmpeg into one MP4.
# Progressive <=1080p is only the fallback. When a video has no native 1080p, YouTube may offer
# an AI-upscaled 1080p AV1 stream (format ids ending in "-sr"); this selector picks it as the
# best mp4, and the result reports it via `ai_upscaled`.
_FORMAT = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]"

# Optional explicit FFmpeg location (folder or binary) for machines where it is not on PATH.
FFMPEG_LOCATION = os.getenv("OPENCLIP_FFMPEG_LOCATION") or None

FFMPEG_SETUP_HELP = (
    "FFmpeg is required to merge the 1080p video and audio streams, extract audio and render clips.\n"
    "Install it and make sure `ffmpeg` and `ffprobe` are on PATH, then restart the app:\n"
    "  - Windows: winget install Gyan.FFmpeg\n"
    "  - macOS:   brew install ffmpeg\n"
    "  - Linux:   sudo apt install ffmpeg\n"
    "Or set OPENCLIP_FFMPEG_LOCATION to the folder that contains the binaries."
)


class _YdlLogger:
    """Collects yt-dlp warnings so 'ffmpeg is not installed' cannot be silently swallowed."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.warnings.append(msg)


def _base_opts() -> dict:
    opts: dict = {}
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    # YouTube hides formats from clients that cannot solve its JS challenges. yt-dlp only enables
    # Deno by default, so also enable Node/Bun when those are what the machine has.
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    return opts


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path and FFMPEG_LOCATION:
        loc = Path(FFMPEG_LOCATION)
        for candidate in (loc / name, loc / f"{name}.exe", loc):
            if candidate.is_file() and candidate.stem == name:
                path = str(candidate)
                break
    if not path:
        raise FFmpegMissingError(f"'{name}' was not found.\n\n{FFMPEG_SETUP_HELP}")
    return path


def check_ffmpeg() -> dict:
    """Verify FFmpeg is discoverable on PATH *and* by yt-dlp's own merger.

    Returns {"ffmpeg": path, "ffprobe": path, "version": str}; raises FFmpegMissingError otherwise.
    """
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **_base_opts()}) as ydl:
        pp = FFmpegPostProcessor(ydl)
        if not pp.available or not pp.probe_available:
            missing = [n for n, ok in (("ffmpeg", pp.available), ("ffprobe", pp.probe_available)) if not ok]
            raise FFmpegMissingError(f"yt-dlp cannot find {' and '.join(missing)}.\n\n{FFMPEG_SETUP_HELP}")
        version = FFmpegPostProcessor.get_versions(ydl).get("ffmpeg") or "unknown"
    # The renderer and audio extraction call the binaries directly, so they must resolve too.
    return {"ffmpeg": require_binary("ffmpeg"), "ffprobe": require_binary("ffprobe"), "version": version}


def probe_video(path: str) -> dict:
    """Codec / resolution / fps of the first video stream, via ffprobe."""
    proc = subprocess.run(
        [require_binary("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,avg_frame_rate", "-of", "json", path],
        capture_output=True, text=True,
    )
    try:
        stream = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
    except json.JSONDecodeError:
        stream = {}
    num, _, den = (stream.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den) if float(den) else 0.0
    except ValueError:
        fps = 0.0
    return {"vcodec": stream.get("codec_name"), "width": stream.get("width"),
            "height": stream.get("height"), "fps": round(fps, 3)}


def extract_wav(video_path: str, wav_path: str) -> str:
    """Extract a 16-bit PCM, mono, 16 kHz WAV (the sample rate Whisper expects)."""
    cmd = [
        require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(wav_path):
        raise DownloadError(f"FFmpeg audio extraction failed:\n{proc.stderr.strip()}")
    return wav_path


def _resolve_downloaded_path(info: dict, ydl: yt_dlp.YoutubeDL) -> str:
    # After a merge the final extension can differ from the template, so trust
    # requested_downloads first and only fall back to prepare_filename.
    for item in info.get("requested_downloads") or []:
        fp = item.get("filepath")
        if fp and os.path.exists(fp):
            return fp
    guess = Path(ydl.prepare_filename(info))
    for candidate in (guess, guess.with_suffix(".mp4"), guess.with_suffix(".mkv"), guess.with_suffix(".webm")):
        if candidate.exists():
            return str(candidate)
    raise DownloadError("yt-dlp finished but the output file could not be located.")


def download_youtube_video(url: str, output_dir: str) -> dict:
    """Download the best <=1080p video for `url` and extract a WAV for transcription.

    Returns a dict with: video_path, audio_path, video_id, title, duration, width, height,
    vcodec, fps, format_id, ai_upscaled, warnings.
    Re-uses files already present in `output_dir` (handy when re-running a job).
    """
    if not url or not url.strip():
        raise DownloadError("No URL provided.")
    check_ffmpeg()  # fail fast with setup instructions instead of leaving an unmerged video-only file
    os.makedirs(output_dir, exist_ok=True)

    logger = _YdlLogger()
    opts = {
        **_base_opts(),
        "logger": logger,
        "format": _FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(output_dir, "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": False,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=True)
            if info is None:
                raise DownloadError("yt-dlp returned no metadata for this URL.")
            if info.get("_type") == "playlist":
                raise DownloadError("Playlist URLs are not supported; paste a single video URL.")
            video_path = _resolve_downloaded_path(info, ydl)
    except yt_dlp.utils.DownloadError as exc:
        if "ffmpeg" in str(exc).lower() and "not installed" in str(exc).lower():
            raise FFmpegMissingError(f"{exc}\n\n{FFMPEG_SETUP_HELP}") from exc
        raise DownloadError(f"yt-dlp could not download the video: {exc}") from exc

    ffmpeg_warnings = [w for w in logger.warnings if "ffmpeg" in w.lower() and "not installed" in w.lower()]
    if ffmpeg_warnings:
        raise FFmpegMissingError(f"{ffmpeg_warnings[0]}\n\n{FFMPEG_SETUP_HELP}")

    stream = probe_video(video_path)
    if not stream.get("vcodec"):
        raise DownloadError(f"Downloaded file has no video stream: {video_path}")

    audio_path = os.path.join(output_dir, "audio.wav")
    if not os.path.exists(audio_path) or os.path.getmtime(audio_path) < os.path.getmtime(video_path):
        extract_wav(video_path, audio_path)

    return {
        "video_path": os.path.abspath(video_path),
        "audio_path": os.path.abspath(audio_path),
        "video_id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "duration": float(info.get("duration") or 0.0),
        "width": stream.get("width") or info.get("width"),
        "height": stream.get("height") or info.get("height"),
        "vcodec": stream.get("vcodec"),
        "fps": stream.get("fps"),
        "format_id": info.get("format_id"),
        "ai_upscaled": "-sr" in str(info.get("format_id") or ""),
        "warnings": logger.warnings,
    }


def ingest_local_video(video_path: str, output_dir: str, title: str | None = None) -> dict:
    """Same result shape as download_youtube_video, for a file that is already on disk
    (an upload). YouTube blocks data-centre IPs, so the hosted app takes files instead."""
    check_ffmpeg()
    if not os.path.exists(video_path):
        raise DownloadError(f"Video file not found: {video_path}")
    os.makedirs(output_dir, exist_ok=True)
    stream = probe_video(video_path)
    if not stream.get("vcodec"):
        raise DownloadError("This file has no video stream (or FFmpeg can't read it).")
    audio_path = os.path.join(output_dir, "audio.wav")
    if not os.path.exists(audio_path) or os.path.getmtime(audio_path) < os.path.getmtime(video_path):
        extract_wav(video_path, audio_path)
    return {
        "video_path": os.path.abspath(video_path),
        "audio_path": os.path.abspath(audio_path),
        "video_id": None,
        "title": title or Path(video_path).stem,
        "duration": probe_duration(video_path),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "vcodec": stream.get("vcodec"),
        "fps": stream.get("fps"),
        "format_id": "upload",
        "ai_upscaled": False,
        "warnings": [],
    }


def probe_duration(path: str) -> float:
    """Container duration in seconds via ffprobe (0.0 if unknown)."""
    proc = subprocess.run(
        [require_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def video_id_from_url(url: str) -> str | None:
    """Resolve the YouTube video id without downloading (used to name the job folder)."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, **_base_opts()}) as ydl:
        info = ydl.extract_info(url.strip(), download=False, process=False)
    return (info or {}).get("id")
