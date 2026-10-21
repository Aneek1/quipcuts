"""Module B - Transcription: faster-whisper with word-level timestamps."""

from __future__ import annotations

import os
import re
import threading

from faster_whisper import WhisperModel

_MODELS: dict[tuple[str, str, str], WhisperModel] = {}
_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    pass


def _get_model(model_size: str, device: str, compute_type: str) -> WhisperModel:
    key = (model_size, device, compute_type)
    with _LOCK:
        if key not in _MODELS:
            try:
                _MODELS[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
            except Exception as exc:  # CUDA libs missing, bad model name, offline, ...
                if device != "cpu":
                    # GPU init is the usual failure; CPU int8 always works.
                    _MODELS[key] = WhisperModel(model_size, device="cpu", compute_type="int8")
                else:
                    raise TranscriptionError(f"Could not load Whisper model '{model_size}': {exc}") from exc
        return _MODELS[key]


def _load_audio(audio_path: str):
    """Load audio as float32 mono 16 kHz.

    The WAV written by downloader.extract_wav is already 16-bit/16 kHz/mono, so it is read
    directly; this bypasses faster-whisper's PyAV decoder, which breaks across PyAV releases.
    Anything else is resampled through FFmpeg.
    """
    import shutil
    import subprocess
    import wave

    import numpy as np

    try:
        with wave.open(audio_path, "rb") as wf:
            if wf.getsampwidth() == 2 and wf.getnchannels() == 1 and wf.getframerate() == 16000:
                pcm = wf.readframes(wf.getnframes())
                return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    except (wave.Error, EOFError):
        pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TranscriptionError("ffmpeg is required to decode this audio file.")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-i", audio_path,
         "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise TranscriptionError(f"Could not decode audio: {proc.stderr.decode(errors='replace')}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _run(model: WhisperModel, audio, language: str | None):
    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        vad_filter=True,  # skips long silences/music, which also stops Whisper hallucinating there
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
        condition_on_previous_text=False,  # reduces repetition loops on long videos
    )
    words: list[dict] = []
    for segment in segments:  # generator: transcription happens while iterating
        for w in segment.words or []:
            token = w.word.strip()
            if not token:
                continue
            start, end = float(w.start), float(w.end)
            words.append({"word": token, "start": round(start, 3), "end": round(max(start, end), 3)})
    return words, info


def transcribe_audio(
    audio_path: str,
    model_size: str = "small",
    device: str = "auto",
    compute_type: str = "default",
    language: str | None = None,
) -> dict:
    """Transcribe `audio_path` and return word-level timestamps.

    Returns:
        {
          "language": "en",
          "duration": 123.4,
          "text": "full transcript ...",
          "words": [{"word": "hello", "start": 0.12, "end": 0.45}, ...]
        }
    """
    if not os.path.exists(audio_path):
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    audio = _load_audio(audio_path)
    model = _get_model(model_size, device, compute_type)
    try:
        words, info = _run(model, audio, language)
    except Exception as exc:
        # device="auto" picks CUDA whenever a GPU exists, but missing cuBLAS/cuDNN DLLs only
        # surface on the first decode. Retry on CPU instead of failing the whole job.
        if device != "cpu" and re.search(r"cuda|cublas|cudnn", str(exc), re.I):
            try:
                words, info = _run(_get_model(model_size, "cpu", "int8"), audio, language)
            except Exception as cpu_exc:
                raise TranscriptionError(f"Transcription failed: {cpu_exc}") from cpu_exc
        else:
            raise TranscriptionError(f"Transcription failed: {exc}") from exc

    if not words:
        raise TranscriptionError("No speech was detected in the audio.")

    return {
        "language": info.language,
        "duration": float(info.duration),
        "text": " ".join(w["word"] for w in words),
        "words": words,
    }
