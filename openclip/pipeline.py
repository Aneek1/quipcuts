"""Orchestrates Modules A-E. Used by the Streamlit app and runnable as a CLI:

    python -m openclip.pipeline "https://www.youtube.com/watch?v=..." --max-clips 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .analyzer import DEFAULT_MODEL, DEFAULT_OLLAMA_MODEL, OLLAMA_URL, analyze_transcript, resolve_llm
from .cropper import scan_faces, single_keyframes_from_scan, split_keyframes_from_scan
from .downloader import download_youtube_video, ingest_local_video, video_id_from_url
from .renderer import render_final_clip
from .transcriber import transcribe_audio

LogFn = Callable[[str], None]
ProgressFn = Callable[[float], None]


@dataclass
class Settings:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    whisper_model: str = "small"
    max_clips: int = 5
    min_duration: float = 30.0
    max_duration: float = 60.0
    workspace: str = field(default_factory=lambda: os.path.abspath("workspace"))
    provider: str = "auto"            # auto | litellm | ollama
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_url: str = OLLAMA_URL
    layout: str = "auto"              # auto | single | split
    split_min_two_face_ratio: float = 0.6  # auto: split only if both faces visible this often


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:limit].strip("-") or "clip"


def run_pipeline(url: str, settings: Settings, log: LogFn = print,
                 progress: ProgressFn = lambda _: None) -> dict:
    t0 = time.time()

    log("Resolving video id...")
    video_id = video_id_from_url(url) or "video"
    job_dir = os.path.join(settings.workspace, _slug(video_id, 64))
    os.makedirs(job_dir, exist_ok=True)

    # A - ingestion
    log("Step 1/5 - Downloading video (<=1080p) and extracting 16 kHz mono WAV...")
    media = download_youtube_video(url, job_dir)
    log(f"Downloaded '{media['title']}' ({media['duration']:.0f}s, {media['width']}x{media['height']} "
        f"{media['vcodec']}, format {media['format_id']}).")
    if media.get("ai_upscaled"):
        log("Note: YouTube has no native 1080p for this video; using its AI-upscaled 1080p stream.")
    progress(0.15)
    return _process(media, job_dir, url, video_id, settings, log, progress, t0)


def run_pipeline_on_file(video_path: str, settings: Settings, title: str | None = None,
                         job_dir: str | None = None, log: LogFn = print,
                         progress: ProgressFn = lambda _: None) -> dict:
    """Same pipeline, starting from a local file (an upload) instead of a YouTube link."""
    t0 = time.time()
    job_dir = job_dir or os.path.join(settings.workspace, _slug(title or os.path.basename(video_path), 64))
    os.makedirs(job_dir, exist_ok=True)

    log("Step 1/5 - Reading the uploaded video and extracting 16 kHz mono WAV...")
    media = ingest_local_video(video_path, job_dir, title)
    log(f"Loaded '{media['title']}' ({media['duration']:.0f}s, {media['width']}x{media['height']} "
        f"{media['vcodec']}).")
    progress(0.15)
    return _process(media, job_dir, None, None, settings, log, progress, t0)


def _process(media: dict, job_dir: str, url: str | None, video_id: str | None, settings: Settings,
             log: LogFn, progress: ProgressFn, t0: float) -> dict:

    # B - transcription (cached per Whisper model, transcription is the slowest CPU step)
    transcript_path = os.path.join(job_dir, f"transcript_{settings.whisper_model}.json")
    if os.path.exists(transcript_path):
        log("Step 2/5 - Re-using cached transcript.")
        with open(transcript_path, encoding="utf-8") as f:
            transcript = json.load(f)
    else:
        log(f"Step 2/5 - Transcribing with faster-whisper '{settings.whisper_model}' (word timestamps)...")
        transcript = transcribe_audio(media["audio_path"], model_size=settings.whisper_model)
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
    log(f"Transcript: {len(transcript['words'])} words, language '{transcript['language']}'.")
    progress(0.40)

    # C - highlight detection
    backend, llm_name = resolve_llm(settings.provider, settings.api_key, settings.model, settings.ollama_model)
    where = f"local Ollama ({settings.ollama_url})" if backend == "ollama" else "LiteLLM"
    log(f"Step 3/5 - Asking {llm_name} via {where} for the strongest hooks...")
    clips = analyze_transcript(
        transcript["words"], settings.api_key, settings.model,
        max_clips=settings.max_clips,
        min_duration=settings.min_duration, max_duration=settings.max_duration,
        provider=settings.provider, ollama_model=settings.ollama_model, ollama_url=settings.ollama_url,
    )
    log(f"LLM selected {len(clips)} clip(s).")
    progress(0.50)

    # D + E - reframe and render each clip
    clips_dir = os.path.join(job_dir, "clips")
    results = []
    for n, clip in enumerate(clips, 1):
        label = f"[{n}/{len(clips)}] {clip['headline_title']}"
        log(f"Step 4/5 - {label}: tracking faces {clip['start_time']:.1f}s-{clip['end_time']:.1f}s...")
        scan = scan_faces(media["video_path"], clip["start_time"], clip["end_time"])
        layout, two_face_ratio = settings.layout, None
        split = None
        if layout in ("auto", "split"):
            split = split_keyframes_from_scan(scan)
            two_face_ratio = split["two_face_ratio"]
            if layout == "auto":
                layout = "split" if two_face_ratio >= settings.split_min_two_face_ratio else "single"
        keyframes = split if layout == "split" else single_keyframes_from_scan(scan)
        ratio_txt = f", two faces in {two_face_ratio:.0%} of frames" if two_face_ratio is not None else ""
        log(f"{label}: layout '{layout}'{ratio_txt}.")

        out_path = os.path.join(clips_dir, f"{n:02d}_{_slug(clip['headline_title'])}.mp4")
        log(f"Step 5/5 - {label}: rendering 1080x1920 with subtitles...")
        render_final_clip(media["video_path"], clip, keyframes, out_path, layout=layout)

        results.append({**{k: v for k, v in clip.items() if k != "words"}, "output_path": out_path,
                        "layout": layout, "two_face_ratio": two_face_ratio})
        progress(0.50 + 0.50 * n / len(clips))

    summary = {
        "url": url,
        "video_id": video_id,
        "title": media["title"],
        "job_dir": job_dir,
        "model": llm_name,
        "provider": backend,
        "source": {k: media.get(k) for k in ("width", "height", "vcodec", "fps", "format_id", "ai_upscaled")},
        "elapsed_s": round(time.time() - t0, 1),
        "clips": results,
    }
    with open(os.path.join(job_dir, "clips.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"Done in {summary['elapsed_s']}s. Clips saved to {clips_dir}")
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="OpenClip AI - turn a YouTube video into vertical shorts.")
    p.add_argument("url")
    p.add_argument("--model", default=os.getenv("OPENCLIP_MODEL", DEFAULT_MODEL), help="LiteLLM model string")
    p.add_argument("--provider", choices=["auto", "litellm", "ollama"], default="auto",
                   help="auto = local Ollama when no API key is available")
    p.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    p.add_argument("--ollama-url", default=OLLAMA_URL)
    p.add_argument("--layout", choices=["auto", "single", "split"], default="auto")
    p.add_argument("--api-key", default=os.getenv("OPENCLIP_API_KEY", ""),
                   help="Provider API key (or rely on ANTHROPIC_API_KEY / OPENAI_API_KEY env vars)")
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--max-clips", type=int, default=5)
    p.add_argument("--min-duration", type=float, default=30.0)
    p.add_argument("--max-duration", type=float, default=60.0)
    p.add_argument("--workspace", default="workspace")
    a = p.parse_args()
    s = Settings(api_key=a.api_key, model=a.model, whisper_model=a.whisper_model, max_clips=a.max_clips,
                 min_duration=a.min_duration, max_duration=a.max_duration,
                 workspace=os.path.abspath(a.workspace), provider=a.provider,
                 ollama_model=a.ollama_model, ollama_url=a.ollama_url, layout=a.layout)
    summary = run_pipeline(a.url, s)
    for c in summary["clips"]:
        print(f"{c['virality_score']:>3}  [{c['layout']}]  {c['headline_title']}  ->  {c['output_path']}")


if __name__ == "__main__":
    main()
