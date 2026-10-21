"""Punch-in zooms: find the loudest words in a clip and zoom in on them briefly.

Loudness is measured per word (RMS in dB over the word's span), then turned into a z-score against
the rest of the clip, so a quiet podcast and a shouty stage talk both get zooms on their own peaks.
`pick_zooms` is deterministic; web/src/pages/Editor.tsx has a copy so the preview matches the render.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np

RATE = 16000

# A zoom eases in fast, holds, then eases out (seconds)
EASE_IN = 0.12
EASE_OUT = 0.3
HOLD = 1.1
MIN_GAP = 4.0          # seconds between the starts of two zooms
EVERY = 6.0            # at most one zoom per this many seconds of clip
MIN_Z = 0.8            # only words at least this many standard deviations louder than the clip


def _audio(media_path: str, start: float, end: float) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH.")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", media_path,
         "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def word_loudness(media_path: str, start: float, end: float, words: list[dict]) -> list[float | None]:
    """z-score of each word's loudness within [start, end] (absolute source times). None if unmeasurable."""
    samples = _audio(media_path, start, end)
    db: list[float | None] = []
    for w in words:
        a = int(max(0.0, w["start"] - start) * RATE)
        b = int(max(0.0, w["end"] - start) * RATE)
        seg = samples[a:b]
        if len(seg) < RATE * 0.05:
            db.append(None)
            continue
        rms = float(np.sqrt(np.mean(seg ** 2)))
        db.append(float(20 * np.log10(rms + 1e-6)))
    vals = [v for v in db if v is not None]
    if len(vals) < 5:
        return [None] * len(words)
    mean, std = float(np.mean(vals)), float(np.std(vals)) or 1.0
    return [None if v is None else round(float((v - mean) / std), 2) for v in db]


def pick_zooms(words: list[dict], clip_start: float, clip_end: float) -> list[float]:
    """Clip-relative start times of punch-ins, from words carrying a `loud` z-score."""
    dur = clip_end - clip_start
    cands = []
    for w in words:
        z = w.get("loud")
        t = w["start"] - clip_start
        if z is None or z < MIN_Z or w["end"] - w["start"] < 0.12 or t < 0.5 or t > dur - EASE_OUT - 0.3:
            continue
        if not str(w.get("word", "")).strip():
            continue
        cands.append((z, round(t, 3)))
    cands.sort(key=lambda c: (-c[0], c[1]))
    picked: list[float] = []
    budget = max(1, int(dur // EVERY))
    for _, t in cands:
        if len(picked) >= budget:
            break
        if all(abs(t - p) >= MIN_GAP for p in picked):
            picked.append(t)
    return sorted(picked)


def zoom_at(t: float, zooms: list[float], strength: float) -> float:
    """Zoom factor (1.0 = none) at clip-relative time t."""
    z = 1.0
    for s in zooms:
        if t < s or t > s + EASE_IN + HOLD + EASE_OUT:
            continue
        if t < s + EASE_IN:
            k = (t - s) / EASE_IN
            k = 1 - (1 - k) ** 3            # ease out cubic: a quick punch
        elif t < s + EASE_IN + HOLD:
            k = 1.0
        else:
            k = 1 - (t - s - EASE_IN - HOLD) / EASE_OUT
            k = k * k * (3 - 2 * k)          # smoothstep back out
        z = max(z, 1.0 + strength * k)
    return z
