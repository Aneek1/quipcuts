r"""Pick-quality evaluation, step 1: run the clip pickers on a fixed set of openly licensed videos.

    .venv\Scripts\python -m modal run eval/modal_eval.py            # all of eval/videos.json
    .venv\Scripts\python -m modal run eval/modal_eval.py --limit 2  # quick check

For every video (on one L4 GPU, like production): download from Wikimedia Commons, transcribe with
faster-whisper 'small', then ask each model in MODELS for 3 clips of 30-60 s through
openclip.analyzer.analyze_transcript, the same call the pipeline makes. Writes
eval/results/picks.json. Step 2 (random baselines + blind judging) is eval/judge.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = "/root/app"
MODELS = ["qwen3:8b", "mistral-small3.2"]

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "curl", "zstd", "ca-certificates")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .run_commands("(ollama serve > /tmp/ollama.log 2>&1 &) && sleep 8 && "
                  + " && ".join(f"ollama pull {m}" for m in MODELS))
    .pip_install("faster-whisper>=1.1.0", "litellm>=1.60.0", "numpy>=1.24", "pydantic>=2.7", "httpx>=0.27",
                 "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
    .env({"LD_LIBRARY_PATH": "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
                             "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib",
          "OLLAMA_HOST": "http://127.0.0.1:11434", "OLLAMA_KEEP_ALIVE": "-1", "PYTHONPATH": APP_DIR})
    .add_local_dir(ROOT / "openclip", f"{APP_DIR}/openclip",
                   ignore=lambda p: "__pycache__" in str(p))
)
app = modal.App("quipcuts-eval")
UA = {"User-Agent": "QuipcutsEval/1.0 (https://quipcuts.app; hello@quipulm.dev)"}


def _download_url(page_url: str) -> str:
    import httpx
    from urllib.parse import unquote

    title = unquote(page_url.split("/wiki/", 1)[1])
    r = httpx.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=60, params={
        "action": "query", "format": "json", "titles": title, "prop": "imageinfo", "iiprop": "url"}).json()
    page = next(iter(r["query"]["pages"].values()))
    return page["imageinfo"][0]["url"]


@app.function(image=image, gpu="L4", cpu=8.0, memory=16384, timeout=3600, max_containers=1)
def pick(video: dict) -> dict:
    import subprocess
    import tempfile

    import httpx

    from openclip.analyzer import analyze_transcript
    from openclip.transcriber import transcribe_audio

    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    work = Path(tempfile.mkdtemp())
    src, wav = work / "source", work / "audio.wav"
    t0 = time.time()
    with httpx.stream("GET", _download_url(video["url"]), headers=UA, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(src, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                   check=True)
    transcript = transcribe_audio(str(wav), model_size="small")
    out = {**video, "language": transcript["language"], "duration": transcript["words"][-1]["end"] if transcript["words"] else 0,
           "words": transcript["words"], "picks": {}, "seconds": {"download+transcribe": round(time.time() - t0, 1)}}
    for _ in range(60):  # wait for the Ollama server
        try:
            httpx.get("http://127.0.0.1:11434/api/tags", timeout=2)
            break
        except Exception:
            time.sleep(1)
    for model in MODELS:
        t = time.time()
        try:
            clips = analyze_transcript(transcript["words"], api_key="", max_clips=3, min_duration=30, max_duration=60,
                                       provider="ollama", ollama_model=model)
            out["picks"][model] = [{k: c[k] for k in ("start_time", "end_time", "headline_title", "virality_score",
                                                       "virality_score_rationale")} for c in clips]
        except Exception as exc:  # a model failing on a video is a result too
            out["picks"][model] = {"error": str(exc)[:500]}
        out["seconds"][model] = round(time.time() - t, 1)
    return out


@app.local_entrypoint()
def main(limit: int = 0):
    videos = json.loads((ROOT / "eval" / "videos.json").read_text(encoding="utf-8"))
    if limit:
        videos = videos[:limit]
    results = []
    for res in pick.map(videos, return_exceptions=True):
        if isinstance(res, Exception):
            print("FAILED:", repr(res)[:300])
            continue
        n = {m: (len(p) if isinstance(p, list) else p.get("error", "?")[:60]) for m, p in res["picks"].items()}
        print(f"{res['title'][:60]:<60} lang={res['language']} picks={n} t={res['seconds']}")
        results.append(res)
    out = ROOT / "eval" / "results" / "picks.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out} ({len(results)} videos)")
