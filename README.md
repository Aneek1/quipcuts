# OpenClip AI

Self-hosted, open-source take on Opus Clip: paste a YouTube link, get vertical 9:16 shorts with
speaker-tracking reframing and word-by-word animated captions, optionally uploaded straight to
your YouTube channel.

```
YouTube URL
  │  A  downloader.py   yt-dlp, ≤1080p DASH video + M4A merged by FFmpeg, 16 kHz mono 16-bit WAV
  │  B  transcriber.py  faster-whisper, word-level timestamps (GPU if available, CPU fallback)
  │  C  analyzer.py     local Ollama or any LiteLLM provider → tolerant JSON parsing → Pydantic-validated clips
  │  D  cropper.py      face detection → 1 or 2 tracked speakers → scene-cut aware smoothed camera paths
  │  E  renderer.py     .ass karaoke captions + one FFmpeg pass: single 9:16 crop, or 2-panel vstack split screen
  │  G  publisher.py    YouTube Data API v3 upload, OAuth token cache, schedule/drip-feed
  ▼  F  app.py          Streamlit UI (or `python -m openclip.pipeline` CLI)
1080×1920 MP4s in workspace/<video_id>/clips/
```

## Install

Requires **Python 3.10+** and **FFmpeg** (with libass + libx264, which all common builds include) on `PATH`.

```bash
# FFmpeg:  winget install Gyan.FFmpeg  |  brew install ffmpeg  |  sudo apt install ffmpeg
git clone <this repo> openclip-ai && cd openclip-ai
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Optional:
- **Fonts**: drop `Montserrat-Black.ttf` (Google Fonts, OFL) into `fonts/` and captions use it
  automatically. Otherwise they use Impact (Windows/macOS) or DejaVu Sans (Linux).
- **GPU transcription**: install the CUDA 12 cuBLAS + cuDNN 9 libraries. Without them, transcription
  falls back to CPU automatically (a 14-minute talk with the `base` model took ~45 s on CPU in testing).
- **yt-dlp + YouTube**: YouTube changes frequently. If downloads fail or only low resolutions are
  offered, `pip install -U yt-dlp` and install [Deno](https://deno.com), which yt-dlp uses for YouTube's JS challenges.

## Run

```bash
streamlit run app.py
```

### Choosing the LLM

| Engine | When it is used | Setup |
|---|---|---|
| **Auto** (default) | Cloud API if a key is typed or set in the environment, otherwise local Ollama | none |
| **Ollama (local)** | always local, free, nothing leaves the machine | install [Ollama](https://ollama.com), `ollama pull llama3.2` |
| **Cloud API** | any [LiteLLM model](https://docs.litellm.ai/docs/providers), default `anthropic/claude-sonnet-5-5` | API key in the sidebar or `ANTHROPIC_API_KEY` etc. |

Ollama is called on its native `/api/chat` endpoint (`OLLAMA_HOST` overrides `http://localhost:11434`) with a
JSON-schema `format`, a 16k context window, and smaller transcript windows sized for that context. Local models
still produce messy output, so every response goes through a tolerant parser: it strips `<think>` blocks and
markdown fences, finds the outermost balanced `{…}`/`[…]` while ignoring brackets inside strings, repairs trailing
commas, smart quotes, Python literals and unquoted keys, salvages truncated arrays, maps key aliases
(`start`, `title`, `score`…), converts `1:23` timestamps and `8/10` scores, then validates each clip on its own,
so one bad clip doesn't discard the rest.

In testing on a 14-minute talk, `llama3.2` (3B) answered in about 30 s but returned only one clip, while
`qwen3:8b` returned three well-chosen clips in about 70 s. Use the largest model your machine runs comfortably.

CLI, no UI:

```bash
python -m openclip.pipeline "https://www.youtube.com/watch?v=arj7oStGLkU" --max-clips 3 --whisper-model small
```

CLI options: `--provider auto|litellm|ollama`, `--ollama-model qwen3:8b`, `--layout auto|single|split`.
Re-running the same URL reuses the downloaded video and the cached transcript.

### Video quality (1080p)

The format selector is `bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]`: separate
DASH video and audio streams, merged by FFmpeg. Before downloading, the app checks that yt-dlp's own merger can
find FFmpeg and shows setup instructions in the UI if it can't, rather than silently saving a video-only or
low-resolution file. If Deno, Node or Bun is installed, it is passed to yt-dlp so that YouTube's JS challenge can
be solved, because without one YouTube hides formats. Some videos have no native 1080p; YouTube then offers an
AI-upscaled 1080p AV1 stream (`-sr` formats), which this selector picks. The pipeline log and `clips.json` flag
this as `ai_upscaled`.

## Publishing to YouTube (Module G)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
2. **OAuth consent screen**: External, add your own Google account as a test user.
3. **Credentials → Create OAuth client ID → Desktop app**. Download the JSON as `client_secret.json`
   in the project root (or set `OPENCLIP_YT_CLIENT_SECRETS`).
4. The first upload opens a Google sign-in tab. The token is cached in `token.json` (owner-only
   permissions, refreshed automatically), so later uploads don't need a browser.

Behaviour:
- Uploads default to **private**. Review them in YouTube Studio before making them public.
- Titles are cleaned, cut at a word boundary to fit YouTube's 100-character limit, and get `#Shorts` appended.
- **Drip-feed** spreads the selected clips evenly over 7 days from a start date/time. YouTube only
  accepts a scheduled `publishAt` on private videos, so scheduled clips stay private until their slot.
- Limits to know about: an unverified Google Cloud app can only upload **private** videos until the
  project passes Google's API audit. The default quota is 10,000 units/day and each upload costs about
  1,600, so roughly 6 uploads per day.

## Layout

```
openclip-ai/
├── app.py                  # Module F: Streamlit UI
├── openclip/
│   ├── downloader.py       # Module A
│   ├── transcriber.py      # Module B
│   ├── analyzer.py         # Module C (system prompt lives here)
│   ├── cropper.py          # Module D
│   ├── renderer.py         # Module E
│   ├── publisher.py        # Module G
│   └── pipeline.py         # orchestration + CLI
├── models/                 # YuNet face model (auto-downloaded, 230 KB)
├── fonts/                  # optional caption fonts
├── tests/smoke_test.py     # offline test: mocked LLM, real tracking + render
└── workspace/<video_id>/   # source.mp4, audio.wav, transcript_*.json, clips.json, clips/*.mp4
```

## Layouts

- **Single**: one 9:16 window follows the primary speaker.
- **Split**: two speakers stacked. Each panel is cropped around its own face, scaled to 1080×960 and combined
  with `vstack` into 1080×1920, with captions on the seam. One decode is `split` into two streams, and a single
  `sendcmd` file drives the named crops `crop@face1` and `crop@face2`.
- **Auto** (default): renders split when both faces are visible in at least 60% of the clip's sampled frames,
  otherwise single.

Two-face tracking keeps identities stable from frame to frame. Each known track continues with the nearest face
within a distance gate. After a scene cut, the two largest faces are re-seeded left to right, so Face_1 is the
left speaker. Frames with more than 4 faces (audience cutaways) are ignored, so a crop never latches onto someone
in the crowd.

## How the reframing works

Frames are sampled at 5 fps and downscaled to 960 px wide for detection. Faces come from MediaPipe when its legacy `solutions` API is installed,
otherwise from OpenCV's YuNet DNN detector, which is the default on current OpenCV/Python versions. The
primary speaker is the largest face, with near-ties going to the face closest to the previous position.
Hard scene cuts are detected by HSV histogram correlation and each shot is tracked separately, so the
virtual camera cuts with the edit instead of panning across it. Within a shot, a dead zone ignores small
head movement and a centred 1.2 s moving average turns the rest into smooth pans. FFmpeg's `sendcmd`
filter then moves the crop window at 30 Hz.

## Known limitations

- The split layout is scaffolding. It was tested on a synthetic two-speaker video (two TED segments side by
  side), not on real podcast footage. When a face isn't detected for a whole shot, its panel falls back to the
  left or right third of the frame and can show the wrong area.
- No active-speaker detection: the single layout follows the largest face, and split always shows both
  speakers, whoever is talking.
- Clip quality depends on the LLM. Suggested times are snapped to word and sentence boundaries and
  clamped to the length range, but a weak model will still pick weak moments.

## Test

```bash
python -m openclip.pipeline "https://www.youtube.com/watch?v=arj7oStGLkU" --max-clips 1  # creates workspace/arj7ostglku (needs an API key)
python -m tests.smoke_test workspace/arj7ostglku   # then runs offline; mocked LLM times assume this talk
python -m tests.test_parser                        # LLM-output parser: 12 malformed-response cases
```

## Hosted demo (Modal)

`deploy/modal_app.py` runs a public demo at https://aneek1--openclip-ui.us-east.modal.run:

- **ui**: CPU container. `deploy/web.py` puts Cloudflare Turnstile (Modal secret `openclip-turnstile`, widget "openclip") in front of a reverse proxy to Streamlit, which runs `app.py` with `OPENCLIP_HOSTED=1`.
- **Clipper**: L4 GPU container, one job at a time. It runs faster-whisper on CUDA, with qwen3:8b served by Ollama inside the image. It scales to zero after 2 idle minutes.
- The two containers share the volume `openclip-jobs` (at `/jobs`). Uploads and audio are deleted after each job, and job folders after 24 hours.
- Caps are kept in the `modal.Dict` `openclip-usage`: 3 runs per visitor per UTC day and 25 in total. Uploads are limited to 500 MB and 15 minutes.
- The hosted app takes **file uploads**, because YouTube answers data-centre IPs with "Sign in to confirm you're not a bot". YouTube publishing and the API-key and Ollama-URL fields are hidden there.

Deploy with `modal deploy deploy/modal_app.py` from the repo root. The first build pulls the 5 GB model and takes about 4 minutes; later code-only deploys take about 10 seconds.
