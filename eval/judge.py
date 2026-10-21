r"""Pick-quality evaluation, step 2: random baselines and a blind pairwise judge.

    .venv\Scripts\python eval\judge.py          # asks for a Claude API key (hidden), or set ANTHROPIC_API_KEY

Reads eval/results/picks.json (from eval/modal_eval.py). For every clip a model picked, it cuts a
random clip of the same length from the same video, starting on a sentence boundary so the
baseline reads cleanly (a fairer, harder baseline than a cut that starts mid-sentence), and not
overlapping the model's own picks by more than half. Then:

  * model pick vs its random cut        -> does picking beat chance?
  * qwen3:8b pick vs Mistral Small pick -> head to head, same rank in each list

A different model (Claude) judges each pair from the transcript text alone, without knowing which
side is which, once in each order. A side wins only if it wins both orders; otherwise it's a tie.
Writes eval/results/judgments.json and eval/RESULTS.md.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
JUDGE = "claude-sonnet-5-5"
PICKERS = ["qwen3:8b", "mistral-small3.2"]
NAMES = {"qwen3:8b": "qwen3:8b", "mistral-small3.2": "Mistral Small 3.2", "random": "random cut"}

PROMPT = """You are an experienced short-form video editor. Below are two excerpts from the transcript of the same
video ("{title}"). Each would be cut out and posted on its own as a vertical short (TikTok, Reels, Shorts).

Which one works better as a standalone short? Judge only:
1. the opening: does the first sentence make someone want to keep watching?
2. standalone sense: can a viewer follow it without the rest of the video?
3. the ending: does it land, rather than stop mid-thought?

Excerpt A:
\"\"\"{a}\"\"\"

Excerpt B:
\"\"\"{b}\"\"\"

Reply with JSON only: {{"better": "A" | "B" | "tie", "why": "<one sentence>"}}"""


def words_between(words, start, end):
    return " ".join(w["word"].strip() for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05)


def random_cut(video, length, avoid, seed):
    words = video["words"]
    dur = video["duration"]
    starts = [w["start"] for i, w in enumerate(words)
              if i == 0 or re.search(r"[.!?]$", words[i - 1]["word"].strip())]
    def overlap(s, e, a, b):
        return max(0.0, min(e, b) - max(s, a))
    ok = [s for s in starts if s + length <= dur
          and all(overlap(s, s + length, a, b) <= 0.5 * length for a, b in avoid)]
    if not ok:
        ok = [s for s in starts if s + length <= dur] or [0.0]
    s = random.Random(seed).choice(ok)
    ends = [w["end"] for w in words if w["end"] <= s + length + 0.5]
    return s, (ends[-1] if ends else s + length)


def seed_of(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


CACHE = ROOT / "results" / "judge_cache.json"
_cache: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
_params = {"temperature": 0}   # dropped if the judge model doesn't accept it


def ask(client, key, title, a, b):
    ck = hashlib.sha256(f"{JUDGE}|{title}|{a}|{b}".encode()).hexdigest()
    if ck in _cache:  # re-runs don't pay for the same call twice
        return tuple(_cache[ck])
    for attempt in range(5):
        r = client.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": JUDGE, "max_tokens": 200, **_params,
                  "messages": [{"role": "user", "content": PROMPT.format(title=title, a=a, b=b)}]}, timeout=120)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code == 400 and "temperature" in r.text and _params:
            print("  (the judge model doesn't take a temperature setting; continuing without it)")
            _params.clear()
            continue
        if r.status_code >= 400:
            sys.exit(f"The Claude API refused the request ({r.status_code}): {r.text[:500]}")
        text = "".join(c.get("text", "") for c in r.json()["content"])
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        better = str(data.get("better", "tie")).strip().upper()
        result = ((better if better in ("A", "B") else "tie"), str(data.get("why", ""))[:300])
        _cache[ck] = result
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
        return result
    raise RuntimeError("judge kept failing")


def judge_pair(client, key, title, x, y):
    """x and y are excerpt texts. Returns 'x', 'y' or 'tie' after judging both orders."""
    first, why1 = ask(client, key, title, x, y)       # x is A
    second, why2 = ask(client, key, title, y, x)      # x is B
    x_wins = (first == "A") + (second == "B")
    y_wins = (first == "B") + (second == "A")
    verdict = "x" if x_wins == 2 else "y" if y_wins == 2 else "tie"
    return verdict, [why1, why2]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def sign_test_p(k, n):
    """Two-sided exact binomial test against 50/50."""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def main():
    key = os.environ.get("ANTHROPIC_API_KEY") or getpass.getpass("Claude API key for the judge (input hidden): ").strip()
    if not key.startswith("sk-ant-"):
        sys.exit("That doesn't look like a Claude API key.")
    videos = json.loads((ROOT / "results" / "picks.json").read_text(encoding="utf-8"))
    videos = [v for v in videos if v.get("language") == "en" and v.get("words")]
    client = httpx.Client()
    pairs = []
    for v in videos:
        ok = {m: v["picks"][m] for m in PICKERS if isinstance(v["picks"].get(m), list)}
        for m, picks in ok.items():
            avoid = [(p["start_time"], p["end_time"]) for p in picks]
            for i, p in enumerate(picks):
                L = p["end_time"] - p["start_time"]
                rs, re_ = random_cut(v, L, avoid, seed_of(v["title"], m, i))
                pairs.append({"video": v["title"], "kind": f"{m} vs random", "x": m, "y": "random",
                              "x_span": [p["start_time"], p["end_time"]], "y_span": [rs, re_],
                              "x_text": words_between(v["words"], p["start_time"], p["end_time"]),
                              "y_text": words_between(v["words"], rs, re_)})
        if len(ok) == 2:
            a, b = (sorted(ok[m], key=lambda p: -p["virality_score"]) for m in PICKERS)
            for pa, pb in zip(a, b):
                pairs.append({"video": v["title"], "kind": "qwen3:8b vs mistral-small3.2", "x": PICKERS[0], "y": PICKERS[1],
                              "x_span": [pa["start_time"], pa["end_time"]], "y_span": [pb["start_time"], pb["end_time"]],
                              "x_text": words_between(v["words"], pa["start_time"], pa["end_time"]),
                              "y_text": words_between(v["words"], pb["start_time"], pb["end_time"])})
    print(f"{len(videos)} videos, {len(pairs)} pairs, {2 * len(pairs)} judge calls with {JUDGE}")
    for i, p in enumerate(pairs, 1):
        (xs, xe), (ys, ye) = p["x_span"], p["y_span"]
        shared = max(0.0, min(xe, ye) - max(xs, ys)) / max(xe - xs, ye - ys)
        if shared >= 0.8:  # both models picked essentially the same moment: nothing to judge
            p["verdict"], p["why"] = "same", []
            print(f"[{i}/{len(pairs)}] {p['kind']:<32} -> same moment, not judged")
            continue
        p["verdict"], p["why"] = judge_pair(client, key, p["video"], p["x_text"], p["y_text"])
        print(f"[{i}/{len(pairs)}] {p['kind']:<32} -> {p['verdict']}")
    (ROOT / "results" / "judgments.json").write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# Clip-picking evaluation", "",
             f"{len(videos)} openly licensed English videos (eval/videos.json), 3 clips of 30-60 s per model per video.",
             f"Judge: {JUDGE}, blind, transcript text only, each pair judged in both orders; a side wins only if it",
             "wins both orders, otherwise the pair is a tie. Random cuts are the same length as the pick they're",
             "compared with and start on a sentence boundary.", "",
             "| Comparison | Pairs | Wins | Losses | Ties | Win rate excl. ties (95% CI) | p (sign test) |",
             "|---|---|---|---|---|---|---|"]
    for kind in sorted({p["kind"] for p in pairs}):
        ps = [p for p in pairs if p["kind"] == kind]
        w = sum(p["verdict"] == "x" for p in ps)
        l = sum(p["verdict"] == "y" for p in ps)
        same = sum(p["verdict"] == "same" for p in ps)
        t = len(ps) - w - l - same
        lo, hi = wilson(w, w + l)
        rate = f"{w / (w + l):.0%} ({lo:.0%}-{hi:.0%})" if w + l else "n/a"
        x, y = ps[0]["x"], ps[0]["y"]
        lines.append(f"| {NAMES[x]} vs {NAMES[y]} | {len(ps)}{f' ({same} same moment)' if same else ''} | {w} | {l} | {t} | {rate} | {sign_test_p(w, w + l):.3f} |")
    timings = {m: [v["seconds"].get(m) for v in videos if v["seconds"].get(m)] for m in PICKERS}
    lines += ["", "Picking time per video on one L4 GPU (median): " +
              ", ".join(f"{NAMES[m]} {sorted(t)[len(t) // 2]:.0f} s" for m, t in timings.items() if t), ""]
    (ROOT / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
