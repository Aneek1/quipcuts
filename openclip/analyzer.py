"""Module C - LLM highlight detection.

Two backends:
  - "litellm": any hosted provider (Anthropic, OpenAI, Gemini, ...) through LiteLLM.
  - "ollama":  a local Ollama server via its native /api/chat endpoint (no key, nothing leaves
               the machine). Chosen automatically when no API key is available.

Responses go through a tolerant JSON extractor (local models often wrap JSON in prose, add
trailing commas or get cut off), are normalised (key aliases, "1:23" timestamps, "85/100"
scores) and validated clip-by-clip against a Pydantic schema; bad output is fed back to the
model for a correction. Timestamps are then snapped to real word boundaries so a clip never
starts or ends mid-word, and clamped to the requested duration window.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import urllib.error
import urllib.request
from functools import lru_cache

import litellm
from pydantic import BaseModel, Field, ValidationError, model_validator

DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"
OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = f"http://{OLLAMA_URL}"
DEFAULT_OLLAMA_MODEL = os.getenv("OPENCLIP_OLLAMA_MODEL", "llama3.2")
PROVIDERS = ("auto", "litellm", "ollama")

SYSTEM_PROMPT = """You are a senior short-form video editor who has cut thousands of TikToks, Reels and YouTube Shorts for podcasts, interviews and talks. You know exactly which moments stop a viewer mid-scroll.

You will receive a timestamped transcript of a long-form video. Each line looks like:
[123.45] text spoken starting at 123.45 seconds

Your job: find the moments that would perform best as standalone vertical short clips.

WHAT MAKES A GREAT CLIP
1. The hook (first 3 seconds) is everything. The opening sentence must create tension, curiosity or emotion on its own: a bold or contrarian claim, a surprising number, a confession, a direct question, a conflict, a "nobody talks about this" moment. Never open on filler ("so", "um", "yeah, and"), on a greeting, or on a sentence that depends on earlier context.
2. It is self-contained. A viewer who has never seen the source must understand it. No unresolved "he", "that thing", "like I said" pointing at something outside the clip.
3. It has a payoff. Complete stories (setup -> tension -> resolution), high-value insights (a concrete, actionable takeaway), strong opinions with reasoning, funny or emotional peaks. End right after the punchline or conclusion lands, not in the middle of the next thought.
4. It respects sentence boundaries. Start at the beginning of a sentence and end at the end of one.

HARD CONSTRAINTS
- Each clip is between {min_duration:.0f} and {max_duration:.0f} seconds long (end_time - start_time).
- start_time and end_time are in seconds and must come from the timestamps in the transcript. start_time should be the timestamp of the line where the hook begins.
- Clips must not overlap each other.
- Return at most {max_clips} clips, best first. Quality beats quantity: return fewer if the material is weak.
- virality_score is an integer 1-100. Be calibrated: 90+ only for moments you would bet on going viral, 50-70 for solid but ordinary moments. Do not give every clip a high score.
- headline_title is a punchy on-screen title of at most 8 words, in the language of the transcript, no hashtags, no emoji.
- virality_score_rationale is 1-3 sentences naming the hook, the payoff and why it works as a short.

OUTPUT
Respond with ONLY a JSON object, no markdown fences and no commentary, matching exactly:
{{"clips": [{{"start_time": 12.3, "end_time": 58.9, "headline_title": "...", "virality_score_rationale": "...", "virality_score": 87}}]}}"""


class ClipSuggestion(BaseModel):
    start_time: float = Field(ge=0, description="Clip start in seconds")
    end_time: float = Field(gt=0, description="Clip end in seconds")
    headline_title: str = Field(min_length=1, max_length=120)
    virality_score_rationale: str = Field(min_length=1)
    virality_score: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _check_order(self) -> "ClipSuggestion":
        if self.end_time <= self.start_time:
            raise ValueError(f"end_time ({self.end_time}) must be greater than start_time ({self.start_time})")
        return self


class ClipSuggestions(BaseModel):
    clips: list[ClipSuggestion]


class AnalysisError(RuntimeError):
    pass


# ---------------------------------------------------------------- transcript formatting

_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")


def build_segments(words: list[dict], max_words: int = 28, max_gap: float = 1.0) -> list[dict]:
    """Group words into sentence-ish lines so the LLM sees readable, timestamped text."""
    segments: list[dict] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        if (
            nxt is None
            or len(current) >= max_words
            or gap >= max_gap
            or (_SENTENCE_END.search(w["word"]) and len(current) >= 4)
        ):
            segments.append({
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(x["word"] for x in current),
            })
            current = []
    return segments


def _format_transcript(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:.2f}] {s['text']}" for s in segments)


def _window_segments(segments: list[dict], max_chars: int, overlap_s: float = 90.0) -> list[list[dict]]:
    """Split very long transcripts into overlapping windows that fit the model context."""
    windows: list[list[dict]] = []
    i = 0
    while i < len(segments):
        size, j = 0, i
        while j < len(segments) and (size + len(segments[j]["text"]) + 12 <= max_chars or j == i):
            size += len(segments[j]["text"]) + 12
            j += 1
        windows.append(segments[i:j])
        if j >= len(segments):
            break
        # step back so a story spanning the boundary is fully visible in the next window
        back_to = segments[j - 1]["end"] - overlap_s
        k = j - 1
        while k > i + 1 and segments[k - 1]["start"] >= back_to:
            k -= 1
        i = max(k, i + 1)
    return windows


# ---------------------------------------------------------------- resilient JSON extraction

_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})
_PAIRS = {"{": "}", "[": "]"}


def _scan_outside_strings(text: str):
    """Yield (index, char, in_string) while tracking JSON double-quoted strings and escapes."""
    in_str = escaped = False
    for i, ch in enumerate(text):
        yield i, ch, in_str
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True


def _balanced_blocks(text: str) -> list[str]:
    """Every top-level {...} / [...] block, found by bracket matching that ignores string contents.

    An unterminated final block (model hit its token limit) is returned too, repaired by cutting it
    back to its last complete element and appending the missing closing brackets.
    """
    blocks, stack, start = [], [], None
    last_safe_cut = None  # (index, stack snapshot) just after the last closed nested element
    for i, ch, in_str in _scan_outside_strings(text):
        if in_str:
            continue
        if ch in _PAIRS:
            if not stack:
                start = i
            stack.append(_PAIRS[ch])
        elif ch in "}]" and stack:
            if ch != stack[-1]:  # mismatched bracket: abandon this block
                stack, start = [], None
                continue
            stack.pop()
            if not stack:
                blocks.append(text[start:i + 1])
                start, last_safe_cut = None, None
            else:
                last_safe_cut = (i + 1, list(stack))
    if stack and start is not None and last_safe_cut:
        cut, open_stack = last_safe_cut
        blocks.append(text[start:cut] + "".join(reversed(open_stack)))
    return blocks


def _repair_json(block: str) -> str:
    """Fix the usual local-model mistakes, touching only text outside string literals."""
    # Split into alternating code / "string" chunks so titles and rationales are never rewritten.
    chunks: list[tuple[bool, str]] = []
    buf, prev_in_str = [], False
    for _, ch, in_str in _scan_outside_strings(block):
        if in_str != prev_in_str:
            chunks.append((prev_in_str, "".join(buf)))
            buf, prev_in_str = [], in_str
        buf.append(ch)
    chunks.append((prev_in_str, "".join(buf)))

    def fix(code: str) -> str:
        code = re.sub(r",(\s*[}\]])", r"\1", code)  # trailing commas:  {"a": 1,}  [1, 2, ]
        code = re.sub(r"\bTrue\b", "true", code)     # Python literals
        code = re.sub(r"\bFalse\b", "false", code)
        code = re.sub(r"\bNone\b", "null", code)
        # unquoted keys:  {start_time: 1.0}
        return re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', code)

    # A trailing comma can sit in one code chunk and its bracket in the next only if a string
    # separates them, which is not a trailing comma - so per-chunk fixing is safe.
    return "".join(text if is_str else fix(text) for is_str, text in chunks)


def extract_json_payload(text: str):
    """Pull the most plausible JSON object/array out of a messy LLM response.

    Handles: <think> blocks, markdown fences, prose before/after, smart quotes, trailing commas,
    single quotes / Python literals, unquoted keys and output truncated mid-array.
    Returns the parsed object (dict or list); raises ValueError if nothing is recoverable.
    """
    if not text or not text.strip():
        raise ValueError("empty model response")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).translate(_SMART_QUOTES)
    fenced = re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, re.S)
    sources = fenced + [text] if fenced else [text]

    candidates: list[str] = []
    for src in sources:
        candidates.extend(_balanced_blocks(src))
    if not candidates:
        raise ValueError("no JSON object or array found in model response")

    # Prefer blocks that look like they carry clips, then the biggest one.
    candidates.sort(key=lambda b: ("start" in b, len(b)), reverse=True)
    errors = []
    for block in candidates:
        for attempt in (block, _repair_json(block)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
        try:  # single-quoted, Python-style dicts
            value = ast.literal_eval(_repair_json(block).replace("true", "True")
                                     .replace("false", "False").replace("null", "None"))
            if isinstance(value, (dict, list)):
                return value
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
    raise ValueError(f"could not parse JSON from model response ({errors[-1] if errors else 'unknown error'})")


_KEY_ALIASES = {
    "start_time": ("start_time", "start", "starttime", "start_sec", "start_seconds", "begin", "from"),
    "end_time": ("end_time", "end", "endtime", "end_sec", "end_seconds", "finish", "to"),
    "headline_title": ("headline_title", "title", "headline", "hook", "name"),
    "virality_score_rationale": ("virality_score_rationale", "rationale", "reason", "reasoning",
                                 "explanation", "why", "justification"),
    "virality_score": ("virality_score", "score", "virality", "viral_score", "rating"),
}


def _to_seconds(value):
    """Accept 12.5, "12.5", "12.5s", "1:23", "01:02:03.5"."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().rstrip("s").strip()
    if ":" in s:
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part or 0)
        return total
    return float(s)


def _to_score(value):
    if isinstance(value, str):
        m = re.search(r"\d+(?:\.\d+)?", value)
        if not m:
            return value
        num = float(m.group())
        if "/10" in value.replace(" ", "") and "/100" not in value.replace(" ", ""):
            num *= 10
        value = num
    if isinstance(value, float) and 0 < value <= 10 and value != int(value):
        value *= 10  # 8.5 on a 10-point scale
    return int(round(float(value)))


def _normalise_clip(raw: dict) -> dict:
    lowered = {re.sub(r"[\s\-]+", "_", str(k).strip().lower()): v for k, v in raw.items()}
    clip = {}
    for canonical, aliases in _KEY_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias] not in (None, ""):
                clip[canonical] = lowered[alias]
                break
    for key in ("start_time", "end_time"):
        if key in clip:
            try:
                clip[key] = _to_seconds(clip[key])
            except ValueError:
                pass
    if "virality_score" in clip:
        try:
            clip["virality_score"] = max(1, min(100, _to_score(clip["virality_score"])))
        except (ValueError, TypeError):
            pass
    clip.setdefault("headline_title", "Untitled clip")
    clip.setdefault("virality_score_rationale", "No rationale provided by the model.")
    clip["headline_title"] = str(clip["headline_title"]).strip()[:120] or "Untitled clip"
    return clip


def parse_clip_suggestions(text: str) -> tuple[list[ClipSuggestion], list[str]]:
    """Parse an LLM response into validated clips. Invalid clips are skipped, not fatal.

    Returns (valid_clips, per-clip error messages).
    """
    payload = extract_json_payload(text)
    if isinstance(payload, dict):
        items = payload.get("clips")
        if items is None:  # {"highlights": [...]} or a single clip object
            items = next((v for v in payload.values() if isinstance(v, list)), [payload])
    else:
        items = payload
    if isinstance(items, dict):
        items = [items]

    clips, errors = [], []
    for n, item in enumerate(items or [], 1):
        if not isinstance(item, dict):
            errors.append(f"clip {n}: not an object")
            continue
        try:
            clips.append(ClipSuggestion.model_validate(_normalise_clip(item)))
        except ValidationError as exc:
            errors.append(f"clip {n}: " + "; ".join(e["msg"] for e in exc.errors()))
    return clips, errors


# Kept for backwards compatibility with earlier callers/tests.
def _extract_json(text: str) -> dict:
    data = extract_json_payload(text)
    return {"clips": data} if isinstance(data, list) else data


# ---------------------------------------------------------------- provider selection

def _supports_schema(model: str) -> bool:
    try:
        return bool(litellm.supports_response_schema(model=model))
    except Exception:
        return False


def _cloud_key_available(model: str) -> bool:
    try:
        return bool(litellm.validate_environment(model=model).get("keys_in_environment"))
    except Exception:
        return False


def resolve_llm(provider: str, api_key: str | None, model: str,
                ollama_model: str = DEFAULT_OLLAMA_MODEL) -> tuple[str, str]:
    """Decide which backend handles the request. Returns (provider, model_name).

    auto: an "ollama/..." model, or no API key (neither typed nor in the environment) -> Ollama.
    """
    provider = (provider or "auto").lower()
    if provider not in PROVIDERS:
        raise AnalysisError(f"Unknown provider '{provider}'. Use one of {PROVIDERS}.")
    if model.startswith("ollama/") or model.startswith("ollama_chat/"):
        return "ollama", model.split("/", 1)[1]
    if provider == "ollama":
        return "ollama", ollama_model
    if provider == "litellm" or api_key or _cloud_key_available(model):
        return "litellm", model
    return "ollama", ollama_model


def _ollama_request(base_url: str, path: str, payload: dict | None, timeout: float) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            message = body
        if exc.code == 404 and "not found" in message.lower():
            model = (payload or {}).get("model", "")
            raise AnalysisError(f"Ollama model '{model}' is not installed. Run:  ollama pull {model}") from exc
        raise AnalysisError(f"Ollama returned HTTP {exc.code}: {message}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise AnalysisError(
            f"Cannot reach Ollama at {base_url} ({getattr(exc, 'reason', exc)}). "
            "Install it from https://ollama.com and make sure it is running (`ollama serve`)."
        ) from exc


def list_ollama_models(base_url: str = OLLAMA_URL, timeout: float = 3.0) -> list[str]:
    """Installed chat-capable Ollama models (embedding-only models are filtered out)."""
    try:
        tags = _ollama_request(base_url, "/api/tags", None, timeout)
    except AnalysisError:
        return []
    names = []
    for m in tags.get("models", []):
        caps = (m.get("details") or {}).get("capabilities") or m.get("capabilities") or []
        if caps and "completion" not in caps:
            continue
        names.append(m["name"])
    return sorted(names)


@lru_cache(maxsize=32)
def _ollama_capabilities(base_url: str, model: str) -> tuple[str, ...]:
    try:
        return tuple(_ollama_request(base_url, "/api/show", {"model": model}, 10).get("capabilities") or ())
    except AnalysisError:
        return ()


def _call_ollama(messages: list[dict], model: str, base_url: str, use_schema: bool,
                 num_ctx: int, timeout: float = 900.0) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # JSON schema -> Ollama constrains decoding to it; "json" is the older, looser mode.
        "format": ClipSuggestions.model_json_schema() if use_schema else "json",
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    if "thinking" in _ollama_capabilities(base_url, model):
        payload["think"] = False  # reasoning models: we only want the JSON answer
    data = _ollama_request(base_url, "/api/chat", payload, timeout)
    return (data.get("message") or {}).get("content") or ""


# ---------------------------------------------------------------- LLM call

def _request_clips(transcript: str, api_key: str | None, model: str, max_clips: int,
                   min_duration: float, max_duration: float, max_attempts: int = 3,
                   provider: str = "litellm", ollama_url: str = OLLAMA_URL,
                   num_ctx: int = 16384) -> list[ClipSuggestion]:
    system = SYSTEM_PROMPT.format(min_duration=min_duration, max_duration=max_duration, max_clips=max_clips)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"TRANSCRIPT:\n{transcript}"},
    ]
    kwargs: dict = {"model": model, "max_tokens": 4096}
    if api_key:
        kwargs["api_key"] = api_key
    use_schema = True if provider == "ollama" else _supports_schema(model)

    last_error: Exception | None = None
    for _ in range(max_attempts):
        try:
            if provider == "ollama":
                content = _call_ollama(messages, model, ollama_url, use_schema, num_ctx)
            else:
                call_kwargs = dict(kwargs, messages=messages)
                if use_schema:
                    call_kwargs["response_format"] = ClipSuggestions
                response = litellm.completion(**call_kwargs)
                content = response.choices[0].message.content or ""
        except litellm.AuthenticationError as exc:
            raise AnalysisError(f"The LLM provider rejected the API key: {exc}") from exc
        except litellm.BadRequestError as exc:
            if use_schema:  # some provider/model combos reject structured output; retry in prompt-only mode
                use_schema = False
                last_error = exc
                continue
            raise AnalysisError(f"LLM request was rejected: {exc}") from exc
        except AnalysisError as exc:
            if provider == "ollama" and use_schema and "HTTP 400" in str(exc):
                use_schema = False  # old Ollama without JSON-schema support
                last_error = exc
                continue
            raise
        except Exception as exc:
            last_error = exc
            continue

        try:
            clips, clip_errors = parse_clip_suggestions(content)
        except ValueError as exc:
            clips, clip_errors = [], [str(exc)]
        if clips:
            return clips
        last_error = ValueError("; ".join(clip_errors) or "no clips in response")
        messages = messages + [
            {"role": "assistant", "content": content[:4000]},
            {"role": "user", "content": (
                "Your response did not match the required JSON schema. Problems:\n"
                f"{last_error}\n\nReturn ONLY the corrected JSON object."
            )},
        ]
    raise AnalysisError(f"LLM did not return valid clips after {max_attempts} attempts: {last_error}")


# ---------------------------------------------------------------- post-processing

def _snap_to_words(s: ClipSuggestion, words: list[dict], min_d: float, max_d: float) -> dict | None:
    """Snap LLM times to word boundaries and enforce the duration window."""
    tolerance = 0.75
    i = next((k for k, w in enumerate(words) if w["start"] >= s.start_time - tolerance), None)
    j = next((k for k in range(len(words) - 1, -1, -1) if words[k]["end"] <= s.end_time + tolerance), None)
    if i is None or j is None or j < i:
        return None
    start = words[i]["start"]

    # Too long: cut at the last word that fits, preferring a sentence end in the allowed range.
    if words[j]["end"] - start > max_d:
        while j > i and words[j]["end"] - start > max_d:
            j -= 1
        k = j
        while k > i and words[k]["end"] - start >= min_d:
            if _SENTENCE_END.search(words[k]["word"]):
                j = k
                break
            k -= 1

    # Too short: extend forward, again preferring to stop on a sentence end.
    if words[j]["end"] - start < min_d:
        while j + 1 < len(words) and words[j]["end"] - start < min_d and words[j + 1]["end"] - start <= max_d:
            j += 1
        k = j
        while k + 1 < len(words) and words[k + 1]["end"] - start <= max_d and not _SENTENCE_END.search(words[k]["word"]):
            k += 1
        if _SENTENCE_END.search(words[k]["word"]):
            j = k

    duration = words[j]["end"] - start
    if duration < min_d * 0.6:  # source too short around this moment; not worth rendering
        return None

    clip_start = max(0.0, start - 0.1)
    clip_end = words[j]["end"] + 0.3
    if j + 1 < len(words):
        clip_end = min(clip_end, max(words[j]["end"], words[j + 1]["start"] - 0.02))
    return {
        "start_time": round(clip_start, 3),
        "end_time": round(clip_end, 3),
        "duration": round(clip_end - clip_start, 3),
        "headline_title": s.headline_title.strip(),
        "virality_score_rationale": s.virality_score_rationale.strip(),
        "virality_score": int(s.virality_score),
        "words": words[i:j + 1],
    }


def _overlap(a: dict, b: dict) -> float:
    inter = min(a["end_time"], b["end_time"]) - max(a["start_time"], b["start_time"])
    shorter = min(a["duration"], b["duration"])
    return max(0.0, inter) / shorter if shorter > 0 else 0.0


def analyze_transcript(
    word_timestamps: list,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_clips: int = 5,
    min_duration: float = 30.0,
    max_duration: float = 60.0,
    max_chars_per_request: int | None = None,
    provider: str = "auto",
    ollama_model: str = DEFAULT_OLLAMA_MODEL,
    ollama_url: str = OLLAMA_URL,
    ollama_num_ctx: int = 16384,
) -> list[dict]:
    """Ask the LLM for the most viral moments and return validated, word-aligned clips.

    provider: "auto" (Ollama when no API key is available), "litellm" or "ollama".
    Each returned dict has: start_time, end_time, duration, headline_title,
    virality_score_rationale, virality_score, words (the word timestamps inside the clip).
    Sorted by virality_score, highest first.
    """
    if not word_timestamps:
        raise AnalysisError("Transcript is empty; nothing to analyze.")
    if min_duration >= max_duration:
        raise AnalysisError("min_duration must be smaller than max_duration.")

    backend, model_name = resolve_llm(provider, api_key, model, ollama_model)
    if max_chars_per_request is None:
        # Local models get smaller windows: ~4 chars/token, leaving room for prompt + answer.
        max_chars_per_request = 60_000 if backend == "litellm" else max(8_000, int(ollama_num_ctx * 2.4))

    words = sorted(word_timestamps, key=lambda w: w["start"])
    segments = build_segments(words)
    windows = _window_segments(segments, max_chars_per_request)
    per_window = max(2, math.ceil(max_clips / len(windows)) + 1)

    candidates: list[dict] = []
    for window in windows:
        suggestions = _request_clips(
            _format_transcript(window), api_key, model_name,
            max_clips if len(windows) == 1 else per_window,
            min_duration, max_duration,
            provider=backend, ollama_url=ollama_url, num_ctx=ollama_num_ctx,
        )
        for s in suggestions:
            snapped = _snap_to_words(s, words, min_duration, max_duration)
            if snapped:
                candidates.append(snapped)

    candidates.sort(key=lambda c: c["virality_score"], reverse=True)
    selected: list[dict] = []
    for c in candidates:
        if all(_overlap(c, kept) < 0.5 for kept in selected):
            selected.append(c)
        if len(selected) >= max_clips:
            break

    if not selected:
        raise AnalysisError("The LLM did not return any usable clips for this video.")
    return selected
