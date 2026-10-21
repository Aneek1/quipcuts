"""Module E - Animated .ass subtitles and the final FFmpeg render.

One FFmpeg invocation per clip, in one of two layouts:
    single: -ss/-t trim -> sendcmd (moves the crop window over time) -> crop -> scale 1080x1920
            -> ass (burns the karaoke-style subtitles) -> H.264 + AAC MP4
    split:  -ss/-t trim -> sendcmd -> split into two streams -> crop@face1 / crop@face2
            -> scale each to 1080x960 -> vstack -> ass -> H.264 + AAC MP4

The dynamic crop is driven by the `sendcmd` filter: keyframes from cropper.py are interpolated at
30 Hz into a command file ("12.033 crop x 418;"), which FFmpeg applies frame-accurately. This
avoids building a giant nested if() expression that breaks on long clips.

FFmpeg runs with its working directory set to a scratch folder holding the .ass and command
files, so filter arguments are plain relative names and Windows drive-letter colons never need
filtergraph escaping.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile

OUT_W, OUT_H = 1080, 1920
PROJECT_FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")

WHITE = "&H00FFFFFF&"
YELLOW = "&H0000FFFF&"  # ASS colours are &HAABBGGRR -> pure yellow (RGB FFFF00)


class RenderError(RuntimeError):
    pass


# ---------------------------------------------------------------- subtitles

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def default_font() -> str:
    if glob.glob(os.path.join(PROJECT_FONTS_DIR, "Montserrat*Black*.ttf")):
        return "Montserrat Black"
    if platform.system() == "Windows":
        return "Impact"
    if platform.system() == "Darwin":
        return "Impact"
    return "DejaVu Sans"


def _group_words(words: list[dict], max_words: int, max_chars: int, max_gap: float) -> list[list[dict]]:
    """Split words into short on-screen 'pages' (2-3 words is the classic shorts look)."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            chars = sum(len(x["text"]) + 1 for x in current) + len(w["text"])
            gap = w["start"] - current[-1]["end"]
            ends_sentence = re.search(r"[.!?…,;:]$", current[-1]["raw"]) is not None
            if len(current) >= max_words or chars > max_chars or gap > max_gap or ends_sentence:
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def build_ass_subtitles(
    words: list[dict],
    clip_start: float,
    clip_end: float,
    font_name: str | None = None,
    font_size: int = 96,
    max_words_per_line: int = 3,
    max_chars_per_line: int = 18,
    vertical_position: float = 0.62,
) -> str:
    """Build an .ass script with the active word in yellow and the rest of the line in white.

    `words` use absolute source times; they are shifted so 0 is the clip start.
    `vertical_position` is the subtitle centre as a fraction of frame height (0.62 sits in the
    lower part of the middle third, clear of faces and of platform UI at the bottom).
    """
    duration = clip_end - clip_start
    font_name = font_name or default_font()
    prepared = []
    for w in words:
        start, end = w["start"] - clip_start, w["end"] - clip_start
        if end <= 0 or start >= duration:
            continue
        text = _ass_escape(w["word"].strip().upper())
        if not text:
            continue
        # strip trailing punctuation for display, keep the raw token for line breaking decisions
        display = re.sub(r"[,.;:]+$", "", text) or text
        prepared.append({"text": display, "raw": text, "start": max(0.0, start), "end": min(duration, end)})

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,7,4,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    x, y = OUT_W // 2, int(OUT_H * vertical_position)
    events: list[str] = []
    groups = _group_words(prepared, max_words_per_line, max_chars_per_line, max_gap=0.7)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration
        group_end = min(group[-1]["end"] + 0.35, next_start, duration)
        for k, word in enumerate(group):
            ev_start = word["start"]
            ev_end = group[k + 1]["start"] if k + 1 < len(group) else group_end
            if ev_end - ev_start < 0.02:
                continue
            parts = []
            for m, other in enumerate(group):
                if m == k:
                    # active word: yellow, slightly larger, with a quick pop-in
                    parts.append(
                        f"{{\\c{YELLOW}\\fscx118\\fscy118\\t(0,90,\\fscx108\\fscy108)}}{other['text']}{{\\r}}"
                    )
                else:
                    parts.append(f"{{\\c{WHITE}}}{other['text']}")
            text = f"{{\\an5\\pos({x},{y})}}" + " ".join(parts)
            events.append(f"Dialogue: 0,{_ass_time(ev_start)},{_ass_time(ev_end)},Caption,,0,0,0,,{text}")

    return header + "\n".join(events) + "\n"


# ---------------------------------------------------------------- crop commands

def build_crop_commands(crop_keyframes: list, duration: float, rate: float = 30.0,
                        target: str = "crop") -> tuple[str, int, int]:
    """Interpolate keyframes into sendcmd lines for the crop filter named `target`.

    Emits "T target x X;" / "T target y Y;" only when a value changes.
    Returns (script, initial_x, initial_y).
    """
    kfs = sorted(crop_keyframes, key=lambda k: k["time"])
    x0, y0 = int(kfs[0]["x"]), int(kfs[0]["y"])
    if len(kfs) == 1:
        return "", x0, y0

    lines = []
    last = {"x": x0, "y": y0}
    idx = 0
    steps = int(duration * rate) + 1
    for s in range(1, steps):
        t = s / rate
        while idx + 1 < len(kfs) and kfs[idx + 1]["time"] <= t:
            idx += 1
        cur = kfs[idx]
        interpolate = idx + 1 < len(kfs) and not kfs[idx + 1].get("cut")
        for axis in ("x", "y"):
            if interpolate:
                nxt = kfs[idx + 1]
                span = nxt["time"] - cur["time"]
                alpha = (t - cur["time"]) / span if span > 0 else 0.0
                v = cur[axis] + (nxt[axis] - cur[axis]) * alpha
            else:
                v = cur[axis]  # hold until the next shot starts (hard cut, no pan across the edit)
            v = int(round(v)) // 2 * 2
            if v != last[axis]:
                lines.append(f"{t:.3f} {target} {axis} {v};")
                last[axis] = v
    return "\n".join(lines) + ("\n" if lines else ""), x0, y0


# ---------------------------------------------------------------- render

LAYOUTS = ("single", "split")
PANEL_H = OUT_H // 2  # 960: each split-screen panel is 1080x960


def _relative_for_filter(path: str, cwd: str) -> str | None:
    """Path usable inside a filtergraph argument, or None if it cannot be expressed safely."""
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:  # different drive on Windows
        return None
    rel = rel.replace("\\", "/")
    return None if re.search(r"[:',\[\];=\\]", rel) else rel


def _ass_filter(work_dir: str) -> str:
    ass_filter = "ass=subs.ass"
    if os.path.isdir(PROJECT_FONTS_DIR):
        fonts_rel = _relative_for_filter(PROJECT_FONTS_DIR, work_dir)
        if fonts_rel:
            ass_filter += f":fontsdir={fonts_rel}"
    return ass_filter


def _single_graph(crop_keyframes: list, duration: float, work_dir: str) -> str:
    """sendcmd -> crop -> scale 1080x1920 -> ass."""
    cmd_text, x0, y0 = build_crop_commands(crop_keyframes, duration)
    crop_w, crop_h = int(crop_keyframes[0]["w"]), int(crop_keyframes[0]["h"])
    filters = []
    if cmd_text:
        with open(os.path.join(work_dir, "crop.cmd"), "w", encoding="utf-8") as f:
            f.write(cmd_text)
        filters.append("sendcmd=f=crop.cmd")
    filters += [
        f"crop=w={crop_w}:h={crop_h}:x={x0}:y={y0}",
        f"scale={OUT_W}:{OUT_H}:flags=lanczos",
        "setsar=1",
        _ass_filter(work_dir),
    ]
    return "[0:v]" + ",".join(filters) + "[vout]"


def _split_graph(split_keyframes: dict, duration: float, work_dir: str) -> str:
    """Dual-speaker layout: one decode, split into two streams, each cropped around its face.

        [0:v] sendcmd ─ split ─┬─ crop@face1 (Face_1 path) ─ scale 1080x960 ─┐
                               └─ crop@face2 (Face_2 path) ─ scale 1080x960 ─┴─ vstack ─ ass ─ 1080x1920

    A single sendcmd drives both crops: commands are addressed to the named instances
    crop@face1 / crop@face2, so each panel follows its own speaker.
    """
    cmd_parts, inits = [], {}
    for name, key in (("crop@face1", "face_1"), ("crop@face2", "face_2")):
        kfs = split_keyframes.get(key)
        if not kfs:
            raise RenderError(f"Split layout needs keyframes for {key}.")
        text, x0, y0 = build_crop_commands(kfs, duration, target=name)
        cmd_parts.append(text)
        inits[key] = (x0, y0, int(kfs[0]["w"]), int(kfs[0]["h"]))
    cmd_text = "".join(cmd_parts)

    head = "[0:v]"
    if cmd_text:
        with open(os.path.join(work_dir, "crop.cmd"), "w", encoding="utf-8") as f:
            f.write(cmd_text)
        head += "sendcmd=f=crop.cmd,"
    x1, y1, w1, h1 = inits["face_1"]
    x2, y2, w2, h2 = inits["face_2"]
    panel = f"scale={OUT_W}:{PANEL_H}:flags=lanczos,setsar=1"
    return (
        f"{head}split=2[in1][in2];"
        f"[in1]crop@face1=w={w1}:h={h1}:x={x1}:y={y1},{panel}[top];"
        f"[in2]crop@face2=w={w2}:h={h2}:x={x2}:y={y2},{panel}[bottom];"
        f"[top][bottom]vstack=inputs=2,{_ass_filter(work_dir)}[vout]"
    )


def build_ffmpeg_command(ffmpeg: str, video_path: str, start: float, duration: float, graph: str,
                         output_path: str, crf: int = 20, preset: str = "medium") -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", os.path.abspath(video_path), "-t", f"{duration:.3f}",
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]


def render_final_clip(
    video_path: str,
    clip_metadata: dict,
    crop_keyframes,
    output_path: str,
    font_name: str | None = None,
    font_size: int = 96,
    crf: int = 20,
    preset: str = "medium",
    keep_work_files: bool = False,
    layout: str = "single",
) -> str:
    """Render one 1080x1920 MP4 with dynamic reframing and burned-in animated subtitles.

    layout="single": crop_keyframes is the list from cropper.calculate_crop_keyframes.
    layout="split":  crop_keyframes is the dict from cropper.calculate_split_keyframes
                     ({"face_1": [...], "face_2": [...]}); Face_1 on top, Face_2 below,
                     captions on the seam between the panels.
    clip_metadata needs: start_time, end_time and words ([{"word","start","end"}], absolute times),
    as returned by analyzer.analyze_transcript. Returns the absolute output path.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("ffmpeg was not found on PATH.")
    if layout not in LAYOUTS:
        raise RenderError(f"layout must be one of {LAYOUTS}")
    if not crop_keyframes:
        raise RenderError("No crop keyframes supplied.")
    if not os.path.exists(video_path):
        raise RenderError(f"Source video not found: {video_path}")

    start = float(clip_metadata["start_time"])
    end = float(clip_metadata["end_time"])
    duration = end - start
    if duration <= 0:
        raise RenderError("Clip end_time must be after start_time.")

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="render_", dir=os.path.dirname(output_path))

    try:
        ass_text = build_ass_subtitles(clip_metadata.get("words", []), start, end,
                                       font_name=font_name, font_size=font_size,
                                       vertical_position=0.5 if layout == "split" else 0.62)
        with open(os.path.join(work_dir, "subs.ass"), "w", encoding="utf-8") as f:
            f.write(ass_text)

        if layout == "split":
            graph = _split_graph(crop_keyframes, duration, work_dir)
        else:
            graph = _single_graph(crop_keyframes, duration, work_dir)

        cmd = build_ffmpeg_command(ffmpeg, video_path, start, duration, graph, output_path, crf, preset)
        proc = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not os.path.exists(output_path):
            tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
            raise RenderError(f"FFmpeg render failed (exit {proc.returncode}):\n{tail}")

        if keep_work_files:
            with open(os.path.join(work_dir, "ffmpeg_cmd.json"), "w", encoding="utf-8") as f:
                json.dump(cmd, f, indent=2)
        return output_path
    finally:
        if not keep_work_files:
            shutil.rmtree(work_dir, ignore_errors=True)
