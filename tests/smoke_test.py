"""Offline smoke test: mocked LLM, real face tracking + FFmpeg render.

Needs a downloaded job folder with source.mp4 + transcript_*.json, e.g. after:
    python -m openclip.pipeline <url>   (or downloader + transcriber by hand)
Run:  python -m tests.smoke_test workspace/<video_id>
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from openclip import analyzer, publisher
from openclip.cropper import calculate_crop_keyframes
from openclip.renderer import build_ass_subtitles, build_crop_commands, render_final_clip


def fake_response(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def main(job_dir: str) -> None:
    transcript = json.load(open(glob.glob(os.path.join(job_dir, "transcript_*.json"))[0], encoding="utf-8"))
    words = transcript["words"]
    video = os.path.join(job_dir, "source.mp4")

    # --- analyzer: first reply is invalid (end < start) -> must be retried; second is fenced JSON
    bad = '{"clips": [{"start_time": 50, "end_time": 10, "headline_title": "x", "virality_score_rationale": "y", "virality_score": 50}]}'
    good = "```json\n" + json.dumps({"clips": [
        {"start_time": 61.0, "end_time": 140.0, "headline_title": "The Instant Gratification Monkey",
         "virality_score_rationale": "Funny, relatable hook.", "virality_score": 88},
        {"start_time": 65.0, "end_time": 100.0, "headline_title": "Overlapping duplicate",
         "virality_score_rationale": "dup", "virality_score": 40},
        {"start_time": 300.0, "end_time": 312.0, "headline_title": "Too short, must be extended",
         "virality_score_rationale": "short", "virality_score": 70},
    ]}) + "\n```"
    replies = iter([fake_response(bad), fake_response(good)])
    with mock.patch.object(analyzer.litellm, "completion", side_effect=lambda **kw: next(replies)), \
         mock.patch.object(analyzer, "_supports_schema", return_value=False):
        clips = analyzer.analyze_transcript(words, api_key="test", model="anthropic/test", max_clips=5)
    for c in clips:
        print(f"clip {c['virality_score']} {c['start_time']:.2f}-{c['end_time']:.2f} ({c['duration']:.1f}s) "
              f"'{c['words'][0]['word']} ... {c['words'][-1]['word']}'")
    assert len(clips) == 2, "overlapping duplicate should be dropped"
    assert all(28 <= c["duration"] <= 61 for c in clips), "durations must be clamped to ~30-60s"
    assert clips[0]["virality_score"] == 88

    # --- subtitles + crop command builders
    ass = build_ass_subtitles(clips[0]["words"], clips[0]["start_time"], clips[0]["end_time"])
    assert "&H0000FFFF&" in ass and ass.count("Dialogue:") >= len(clips[0]["words"]) - 2
    cmds, x0, _ = build_crop_commands([
        {"time": 0, "x": 100, "y": 0, "w": 404, "h": 720},
        {"time": 1, "x": 200, "y": 0, "w": 404, "h": 720},
        {"time": 2, "x": 600, "y": 0, "w": 404, "h": 720, "cut": True},
    ], duration=3)
    times = [float(line.split()[0]) for line in cmds.splitlines()]
    assert x0 == 100 and "2.000 crop x 600;" in cmds, cmds[-200:]
    # 0-1s pans 100->200; 1-2s holds at 200 because the next keyframe is a hard cut (no pan across it)
    assert any(0 < tt < 1 for tt in times) and not any(1.0 < tt < 2.0 for tt in times), cmds

    # --- publisher helpers (no network)
    t = publisher.format_shorts_title("A" * 30 + " " + "word " * 30 + "#shorts")
    assert len(t) <= 100 and t.endswith("#Shorts") and t.lower().count("#shorts") == 1, t
    slots = publisher.build_drip_schedule(5, datetime(2026, 10, 21, 9, 0))
    assert slots[-1] - slots[0] == timedelta(days=7) * 4 / 5

    # --- real face tracking + render of the top clip
    clip = clips[0]
    kfs = calculate_crop_keyframes(video, clip["start_time"], clip["end_time"])
    xs = [k["x"] for k in kfs]
    print(f"keyframes: {len(kfs)}, crop {kfs[0]['w']}x{kfs[0]['h']}, x range {min(xs)}-{max(xs)}, "
          f"cuts {sum(k['cut'] for k in kfs)}")
    out = render_final_clip(video, clip, kfs, os.path.join(job_dir, "smoke", "clip.mp4"))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                            "-of", "json", out], capture_output=True, text=True)
    info = json.loads(probe.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - clip["duration"]) < 0.5
    print(f"rendered {out}: 1080x1920, {float(info['format']['duration']):.2f}s")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "workspace/test")
