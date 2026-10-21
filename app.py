"""Module F - Streamlit UI.  Run:  streamlit run app.py"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import traceback
from datetime import date, datetime, time as dtime, timedelta

import streamlit as st

from openclip import hosted

HOSTED = hosted.HOSTED  # public demo on Modal: uploads instead of links, GPU worker, no publishing
if not HOSTED:  # the pipeline's heavy imports only exist where it runs in-process
    from openclip.analyzer import DEFAULT_MODEL, DEFAULT_OLLAMA_MODEL, OLLAMA_URL, list_ollama_models
    from openclip.downloader import FFmpegMissingError, check_ffmpeg
    from openclip.pipeline import Settings, run_pipeline

st.set_page_config(page_title="OpenClip AI", page_icon=":material/movie:", layout="wide",
                   initial_sidebar_state="auto")  # auto = open on desktop, collapsed on phones

WORKSPACE = os.path.abspath(os.getenv("OPENCLIP_WORKSPACE", "workspace"))
st.session_state.setdefault("my_jobs", [])  # hosted: this browser session's job ids
STEPS = ["Download", "Transcribe", "Find hooks", "Reframe", "Render"]

st.session_state.setdefault("results", None)
st.session_state.setdefault("logs", [])

# ---------------------------------------------------------------- styling
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root { --panel:#13131A; --panel2:#1B1B24; --line:#26262F; --text:#EDEDF2; --muted:#8E8E9C;
        --a1:#8B5CF6; --a2:#EC4899; --good:#22C55E; --mid:#F59E0B; --low:#71717A; }
html, body, .stApp, .stMarkdown, button, input, textarea, select { font-family:'Inter', sans-serif !important; }
.stApp { background: radial-gradient(1100px 480px at 50% -8%, rgba(139,92,246,.20), transparent 62%), #09090D; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.0rem; max-width: 1180px; }
section[data-testid="stSidebar"] { background:#0E0E14; border-right:1px solid var(--line); }
section[data-testid="stSidebar"] .stButton button { justify-content:flex-start; text-align:left; border-radius:12px; }
section[data-testid="stSidebar"] .stButton button p { text-align:left; }

.oc-nav { display:flex; align-items:center; justify-content:space-between; padding:6px 2px 18px; }
.oc-logo { font-weight:800; font-size:22px; letter-spacing:-.02em; color:var(--text); }
.oc-logo .mark { display:inline-grid; place-items:center; width:30px; height:30px; margin-right:10px;
  border-radius:9px; background:linear-gradient(135deg,var(--a1),var(--a2)); color:#fff; font-size:15px; vertical-align:-6px; }
.oc-logo .ai { font-size:11px; font-weight:700; padding:2px 7px; margin-left:8px; border-radius:6px;
  background:rgba(139,92,246,.18); color:#C4B5FD; vertical-align:3px; }
.oc-status { display:flex; gap:8px; flex-wrap:wrap; }
.oc-pill { font-size:12px; font-weight:600; padding:5px 11px; border-radius:999px; background:var(--panel2);
  border:1px solid var(--line); color:var(--muted); }
.oc-pill .dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:6px; vertical-align:1px; }

.oc-hero { text-align:center; margin: 18px 0 22px; }
.oc-hero h1 { font-size:46px; line-height:1.08; font-weight:800; letter-spacing:-.03em; margin:0 0 10px; color:var(--text); }
.oc-hero p { color:var(--muted); font-size:16px; margin:0; }
.grad { background:linear-gradient(90deg,#A78BFA,#F472B6); -webkit-background-clip:text; background-clip:text; color:transparent; }

.st-key-hero_card { background:var(--panel); border:1px solid var(--line); border-radius:20px;
  padding:18px 20px 8px; box-shadow:0 24px 70px rgba(0,0,0,.45); max-width:860px; margin:0 auto; }
.st-key-hero_card input { height:50px; font-size:16px !important; }
.stButton button[kind="primary"], .stDownloadButton button[kind="primary"] {
  background:linear-gradient(90deg,#7C3AED,#DB2777); border:0; border-radius:12px; font-weight:700; }
.st-key-go button { height:50px; }
.stButton button[kind="primary"]:hover { filter:brightness(1.12); }

.oc-steps { display:flex; gap:10px; margin:6px 0 4px; }
.oc-step { flex:1; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:12px 14px; }
.oc-step .n { font-size:11px; font-weight:700; color:var(--muted); letter-spacing:.06em; text-transform:uppercase; }
.oc-step .t { font-size:14px; font-weight:700; color:var(--text); margin-top:2px; }
.oc-step .bar { height:4px; border-radius:4px; background:var(--line); margin-top:10px; overflow:hidden; }
.oc-step.done .bar { background:var(--good); }
.oc-step.active { border-color:rgba(139,92,246,.6); box-shadow:0 0 0 3px rgba(139,92,246,.15); }
.oc-step.active .bar span { display:block; height:100%; width:45%; background:linear-gradient(90deg,var(--a1),var(--a2));
  animation: oc-slide 1.1s ease-in-out infinite alternate; }
@keyframes oc-slide { from { margin-left:0 } to { margin-left:55% } }
.oc-now { color:var(--muted); font-size:13px; margin:8px 2px 0; }

.oc-section { display:flex; align-items:end; justify-content:space-between; margin:28px 0 12px; gap:12px; flex-wrap:wrap; }
.oc-section h2 { font-size:22px; font-weight:800; letter-spacing:-.02em; margin:0; color:var(--text); }
.oc-section .sub { color:var(--muted); font-size:13px; margin-top:4px; }
.oc-chip { display:inline-block; padding:3px 10px; border-radius:999px; background:var(--panel2); border:1px solid var(--line);
  color:var(--muted); font-size:12px; font-weight:600; margin:0 6px 6px 0; }

div[class*="st-key-clip_"] { background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:12px 12px 4px; }
div[class*="st-key-clip_"]:hover { border-color:#3A3A48; }
div[class*="st-key-clip_"] video { border-radius:12px; background:#000; }
.oc-score { display:flex; align-items:center; justify-content:space-between; margin:2px 2px 10px; }
.oc-score .num { font-size:30px; font-weight:800; letter-spacing:-.03em; line-height:1; }
.oc-score .num small { font-size:13px; color:var(--muted); font-weight:600; }
.oc-score .lbl { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; padding:4px 9px; border-radius:8px; }
.oc-title { font-size:15px; font-weight:700; color:var(--text); margin:10px 2px 6px; line-height:1.3; min-height:40px; }
.st-key-publish_card, .st-key-error_card { background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px 20px; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- environment checks
@st.cache_data(ttl=60, show_spinner=False)
def ffmpeg_status() -> tuple[bool, str]:
    if HOSTED:
        return True, "hosted"
    try:
        return True, check_ffmpeg()["version"]
    except FFmpegMissingError as exc:
        return False, str(exc)


@st.cache_data(ttl=30, show_spinner=False)
def ollama_models(url: str) -> list[str]:
    return list_ollama_models(url)


def pill(text: str, color: str) -> str:
    return f'<span class="oc-pill"><span class="dot" style="background:{color}"></span>{html.escape(text)}</span>'


def score_style(score: int) -> tuple[str, str]:
    if score >= 85:
        return "#22C55E", "Viral"
    if score >= 70:
        return "#A3E635", "Strong"
    if score >= 55:
        return "#F59E0B", "Solid"
    return "#71717A", "Low"


def fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def load_projects() -> list[dict]:
    projects = []
    for path in glob.glob(os.path.join(WORKSPACE, "*", "clips.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["_mtime"] = os.path.getmtime(path)
            projects.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(projects, key=lambda p: p["_mtime"], reverse=True)


ff_ok, ff_info = ffmpeg_status()

# ---------------------------------------------------------------- sidebar: projects + advanced
if HOSTED:
    # Only the sample and this session's own jobs: never list other visitors' videos.
    with st.sidebar:
        st.markdown("#### :material/video_library: Projects")
        for job_id in [hosted.SAMPLE_ID] + st.session_state.my_jobs[::-1]:
            p = hosted.load_job(job_id)
            if not p:
                continue
            title = ("Sample · " if job_id == hosted.SAMPLE_ID else "") + p["title"]
            if st.button(f"{title[:42]}{'…' if len(title) > 42 else ''}", key=f"proj_{job_id}",
                         icon=":material/play_circle:", width="stretch", help=f"{len(p['clips'])} clips"):
                st.session_state.results = p
                st.session_state.logs = []
                st.query_params["project"] = job_id
        wanted = st.query_params.get("project")
        if wanted and st.session_state.results is None:
            st.session_state.results = hosted.load_job(wanted)
        st.divider()
        st.caption(f"Public demo on a Modal GPU. Uploads up to {hosted.MAX_UPLOAD_MB} MB and "
                   f"{hosted.MAX_VIDEO_S // 60} minutes. Your video is deleted once it's processed; "
                   "the clips are kept for 24 hours, then deleted.")
        st.caption("YouTube links and publishing work when you run OpenClip on your own machine.")
    api_key = cloud_model = ollama_url = ""
    whisper_model = "small"
else:
    with st.sidebar:
        st.markdown("#### :material/video_library: Projects")
        projects = load_projects()
        if not projects:
            st.caption("Your processed videos will appear here.")
        for p in projects[:20]:
            label = f"{p['title'][:42]}{'…' if len(p['title']) > 42 else ''}"
            if st.button(label, key=f"proj_{p['job_dir']}", icon=":material/play_circle:", width="stretch",
                         help=f"{len(p['clips'])} clips · {datetime.fromtimestamp(p['_mtime']):%d %b %H:%M}"):
                st.session_state.results = p
                st.session_state.logs = []
                st.query_params["project"] = os.path.basename(p["job_dir"])
        # deep link: ?project=<job folder> reopens that project (shareable, survives a browser refresh)
        wanted = st.query_params.get("project")
        if wanted and st.session_state.results is None:
            st.session_state.results = next((p for p in projects if os.path.basename(p["job_dir"]) == wanted), None)
        st.divider()
        st.markdown("#### :material/tune: Advanced")
        api_key = st.text_input("Cloud API key", type="password", value=os.getenv("OPENCLIP_API_KEY", ""),
                                help="Only for the Cloud engine. Without a key, Auto uses local Ollama.")
        cloud_model = st.text_input("Cloud model (LiteLLM)", value=os.getenv("OPENCLIP_MODEL", DEFAULT_MODEL))
        ollama_url = st.text_input("Ollama URL", value=OLLAMA_URL)
        whisper_model = st.selectbox("Transcription model", ["tiny", "base", "small", "medium", "large-v3"], index=2,
                                     help="Larger = more accurate word timing, slower on CPU.")

installed = [] if HOSTED else ollama_models(ollama_url)

# ---------------------------------------------------------------- top bar
if HOSTED:
    status = [pill("Modal · L4 GPU", "#22C55E"), pill(f"{hosted.MODEL} · open weights", "#22C55E")]
    tagline = ("Upload a video. AI finds the best hooks, reframes to 9:16 around the speaker "
               "and adds animated captions.")
else:
    status = [pill(f"FFmpeg {ff_info.split('-')[0]}" if ff_ok else "FFmpeg missing", "#22C55E" if ff_ok else "#EF4444"),
              pill(f"Ollama · {len(installed)} models" if installed else "Ollama offline",
                   "#22C55E" if installed else "#71717A")]
    tagline = ("Paste a YouTube link. AI finds the best hooks, reframes to 9:16 around the speaker "
               "and adds animated captions.")
st.markdown(f"""
<div class="oc-nav">
  <div class="oc-logo"><span class="mark">▶</span>OpenClip<span class="ai">AI</span></div>
  <div class="oc-status">{''.join(status)}</div>
</div>
<div class="oc-hero">
  <h1>Turn long videos into <span class="grad">viral shorts</span></h1>
  <p>{tagline}</p>
</div>
""", unsafe_allow_html=True)

if not ff_ok:
    with st.container(key="error_card"):
        st.error("**FFmpeg setup required**", icon=":material/error:")
        st.code(ff_info, language=None)

# ---------------------------------------------------------------- hero input + options
url, upload, provider, ollama_model = "", None, "ollama", ""
with st.container(key="hero_card"):
    if HOSTED:
        c_url, c_go = st.columns([5, 1.25], vertical_alignment="center")
        upload = c_url.file_uploader("Video file", type=hosted.EXTS, label_visibility="collapsed",
                                     help=f"Up to {hosted.MAX_UPLOAD_MB} MB and {hosted.MAX_VIDEO_S // 60} minutes.")
        with c_go.container(key="go"):
            go = st.button("Get clips", type="primary", icon=":material/auto_awesome:", width="stretch",
                           disabled=upload is None)
        o3, o4, o5 = st.columns([1.45, 1.0, 0.7])
        max_clips_cap, default_clips = hosted.MAX_CLIPS, 3
    else:
        c_url, c_go = st.columns([5, 1.25], vertical_alignment="bottom")
        url = c_url.text_input("YouTube link", placeholder="Drop a YouTube link  —  https://www.youtube.com/watch?v=…",
                               label_visibility="collapsed")
        with c_go.container(key="go"):
            go = st.button("Get clips", type="primary", icon=":material/auto_awesome:", width="stretch",
                           disabled=not (url.strip() and ff_ok))

        o1, o2, o3, o4, o5 = st.columns([1.05, 1.3, 1.45, 1.0, 0.7])
        engine = o1.selectbox("AI engine", ["Auto", "Ollama (local)", "Cloud API"],
                              help="Auto = Cloud API when a key is set, otherwise local Ollama.")
        if engine == "Cloud API":
            o2.text_input("Model", value=cloud_model, disabled=True)
            ollama_model = DEFAULT_OLLAMA_MODEL
        else:
            options = installed or [DEFAULT_OLLAMA_MODEL]
            default = next((i for i, m in enumerate(options) if m.split(":")[0] == DEFAULT_OLLAMA_MODEL), 0)
            ollama_model = o2.selectbox("Local model", options, index=default)
        provider = {"Auto": "auto", "Ollama (local)": "ollama", "Cloud API": "litellm"}[engine]
        max_clips_cap, default_clips = 10, 4
    layout_label = o3.segmented_control("Layout", ["Auto", "Single", "Split"], default="Auto",
                                        help="Split = two speakers stacked top/bottom.")
    length = o4.selectbox("Clip length", ["15–30s", "30–60s", "60–90s"], index=1)
    max_clips = o5.number_input("Clips", 1, max_clips_cap, default_clips)

min_d, max_d = (float(v) for v in re.findall(r"\d+", length))

# ---------------------------------------------------------------- processing
def render_steps(placeholder, active: int, done: bool = False, now: str = "") -> None:
    cells = []
    for i, name in enumerate(STEPS):
        state = "done" if done or i < active else "active" if i == active else ""
        cells.append(f'<div class="oc-step {state}"><div class="n">Step {i + 1}</div>'
                     f'<div class="t">{name}</div><div class="bar"><span></span></div></div>')
    placeholder.markdown(f'<div class="oc-steps">{"".join(cells)}</div>'
                         f'<div class="oc-now">{html.escape(now)}</div>', unsafe_allow_html=True)


def run_hosted(log, progress) -> dict:
    """Upload to the shared volume, then stream the GPU worker's events."""
    sid = hosted.visitor_id(st.context.cookies)
    if sid is None:
        raise RuntimeError("Your session check expired. Reload the page to continue.")
    job_id = hosted.new_job_id()
    path = hosted.save_upload(job_id, upload.name, upload)
    seconds = hosted.probe_duration(path)
    if not seconds:
        raise RuntimeError("This file doesn't look like a video FFmpeg can read.")
    if seconds > hosted.MAX_VIDEO_S:
        raise RuntimeError(f"This video is {seconds / 60:.0f} minutes long. The demo takes up to "
                           f"{hosted.MAX_VIDEO_S // 60} minutes; trim it first, or run OpenClip locally.")
    reason = hosted.take_job_slot(sid)
    if reason:
        raise RuntimeError(reason)
    log("Waiting for the GPU (it takes about a minute to start when idle)…")
    title = os.path.splitext(upload.name)[0].replace("_", " ").strip() or "Upload"
    opts = {"max_clips": int(max_clips), "min_d": min_d, "max_d": max_d, "layout": (layout_label or "Auto").lower()}
    for kind, payload in hosted.submit(job_id, os.path.basename(path), title, opts):
        if kind == "log":
            log(payload)
        elif kind == "progress":
            progress(payload)
        elif kind == "error":
            raise RuntimeError(payload)
        elif kind == "done":
            st.session_state.my_jobs.append(job_id)
            st.query_params["project"] = job_id
            return hosted.load_job(job_id) or payload
    raise RuntimeError("The worker stopped without a result. Please try again.")


if go:
    st.session_state.results = None
    st.session_state.logs = []
    st.markdown('<div class="oc-section"><div><h2>Processing</h2></div></div>', unsafe_allow_html=True)
    steps_ph = st.empty()
    progress_ph = st.progress(0.0)
    log_box = st.expander("Activity log", expanded=False, icon=":material/terminal:")
    state = {"step": 0}
    render_steps(steps_ph, 0, now="Starting…")

    def log(msg: str) -> None:
        m = re.search(r"Step (\d)/5", msg)
        if m:
            state["step"] = int(m.group(1)) - 1
        st.session_state.logs.append(f"`{datetime.now():%H:%M:%S}` {msg}")
        log_box.markdown(st.session_state.logs[-1])
        render_steps(steps_ph, state["step"], now=msg)

    def show_progress(p: float) -> None:
        progress_ph.progress(min(1.0, p))

    try:
        if HOSTED:
            st.session_state.results = run_hosted(log, show_progress)
        else:
            settings = Settings(api_key=api_key.strip(), model=cloud_model.strip(), whisper_model=whisper_model,
                                max_clips=int(max_clips), min_duration=min_d, max_duration=max_d,
                                workspace=WORKSPACE, provider=provider, ollama_model=ollama_model,
                                ollama_url=ollama_url.strip(), layout=(layout_label or "Auto").lower())
            st.session_state.results = run_pipeline(url.strip(), settings, log=log, progress=show_progress)
        render_steps(steps_ph, len(STEPS), done=True, now="All clips are ready.")
        progress_ph.empty()
    except Exception as exc:
        log(f"Error: {exc}")
        progress_ph.empty()
        st.error(str(exc), icon=":material/error:")
        if not HOSTED:  # tracebacks would show server paths to visitors
            with st.expander("Technical details"):
                st.code(traceback.format_exc())

# ---------------------------------------------------------------- results grid
results = st.session_state.results
if results:
    clips = results["clips"]
    src = results.get("source") or {}
    chips = [f"{len(clips)} clips"]
    if src.get("height"):
        chips.append(f"Source {src['height']}p {str(src.get('vcodec') or '').upper()}"
                     + (" · AI-upscaled" if src.get("ai_upscaled") else ""))
    chips.append(f"{results.get('provider', 'llm')} · {results['model']}")
    chips.append(f"{results['elapsed_s']:.0f}s processing")
    st.markdown(
        f'<div class="oc-section"><div><h2>{html.escape(results["title"])}</h2>'
        f'<div class="sub">{"".join(f"<span class=oc-chip>{html.escape(c)}</span>" for c in chips)}</div></div></div>',
        unsafe_allow_html=True)

    per_row = 3
    for row_start in range(0, len(clips), per_row):
        cols = st.columns(per_row, gap="medium")
        for col, (n, clip) in zip(cols, list(enumerate(clips))[row_start:row_start + per_row]):
            with col, st.container(key=f"clip_{n}"):
                path = clip["output_path"]
                score = clip.get("virality_score")
                if score is None:  # e.g. layout test renders that were never scored by the LLM
                    color, label, number = "#8E8E9C", "Layout test", "—"
                else:
                    (color, label), number = score_style(int(score)), f"{score}<small>/100</small>"
                st.markdown(
                    f'<div class="oc-score"><div class="num" style="color:{color}">{number}</div>'
                    f'<div class="lbl" style="color:{color};background:{color}22">{label}</div></div>',
                    unsafe_allow_html=True)
                if os.path.exists(path):
                    st.video(path)
                else:
                    st.warning("Rendered file missing.")
                meta = [f"#{n + 1}", f"{clip['duration']:.0f}s",
                        f"{fmt_ts(clip['start_time'])}–{fmt_ts(clip['end_time'])}",
                        (clip.get("layout") or "single").capitalize()]
                st.markdown(f'<div class="oc-title">{html.escape(clip["headline_title"])}</div>'
                            + "".join(f"<span class=oc-chip>{html.escape(m)}</span>" for m in meta),
                            unsafe_allow_html=True)
                with st.expander("Why it could go viral"):
                    st.write(clip["virality_score_rationale"])
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        st.download_button("Download", f.read(), file_name=os.path.basename(path), mime="video/mp4",
                                           key=f"dl_{path}", icon=":material/download:", width="stretch")

    st.download_button("Export clip metadata (JSON)", json.dumps(results, indent=2, ensure_ascii=False),
                       file_name="clips.json", mime="application/json", icon=":material/data_object:")

    # ------------------------------------------------------------ Module G: publish to YouTube
    if not HOSTED:  # never offer the owner's YouTube token to visitors
        st.markdown('<div class="oc-section"><div><h2>Publish to YouTube</h2>'
                    '<div class="sub">Uploads start private. Schedule a drip-feed to post one Short at a time.</div>'
                    '</div></div>', unsafe_allow_html=True)
        try:
            from openclip import publisher
        except ImportError:
            publisher = None
            st.info("Install google-api-python-client and google-auth-oauthlib to enable publishing.")

        if publisher:
            with st.container(key="publish_card"):
                if publisher.is_authenticated():
                    st.markdown(pill("YouTube connected (token.json)", "#22C55E"), unsafe_allow_html=True)
                else:
                    st.markdown(pill("Not connected · first upload opens Google sign-in", "#F59E0B"),
                                unsafe_allow_html=True)

                labels = {f"{c.get('virality_score') or '—'} · {c['headline_title']}": c for c in clips}
                chosen = st.multiselect("Clips to upload", list(labels), default=list(labels))
                p1, p2 = st.columns(2)
                privacy = p1.selectbox("Privacy", publisher.PRIVACY_STATUSES, index=0,
                                       help="Private is the safe default: review in YouTube Studio first.")
                mode = p2.segmented_control("When", ["Upload now", "Drip-feed over one week"], default="Upload now")
                schedule: list = []
                if mode == "Drip-feed over one week":
                    d1, d2 = st.columns(2)
                    first_day = d1.date_input("First publish date", value=date.today() + timedelta(days=1))
                    first_time = d2.time_input("Publish time (local)", value=dtime(9, 0))
                    schedule = publisher.build_drip_schedule(
                        len(chosen), datetime.combine(first_day, first_time).astimezone(), span_days=7)
                    st.caption("Scheduled videos stay **private** until their slot, then YouTube makes them public.")
                    for label, when in zip(chosen, schedule):
                        st.markdown(f"<span class=oc-chip>{when:%a %d %b · %H:%M}</span> "
                                    f"{html.escape(label.split('·', 1)[1].strip())}", unsafe_allow_html=True)

                if st.button("Upload to YouTube", type="primary", icon=":material/upload:", disabled=not chosen):
                    uploads = []
                    for i, label in enumerate(chosen):
                        clip = labels[label]
                        bar = st.progress(0.0, text=f"Uploading {clip['headline_title']}…")
                        try:
                            res = publisher.upload_short_to_youtube(
                                clip["output_path"],
                                title=clip["headline_title"],
                                description=f"{clip['headline_title']}\n\nFrom: {results['title']}\n{results['url']}",
                                privacy_status=privacy,
                                publish_at=schedule[i] if schedule else None,
                                progress=lambda p, b=bar: b.progress(min(1.0, p)),
                            )
                            uploads.append(res)
                            when = f" · goes public {res['publish_at']}" if res["publish_at"] else ""
                            st.success(f"[{res['title']}]({res['url']}) · {res['privacy_status']}{when}")
                        except Exception as exc:
                            st.error(f"{clip['headline_title']}: {exc}")
                    if uploads:
                        with open(os.path.join(results["job_dir"], "uploads.json"), "a", encoding="utf-8") as f:
                            for u in uploads:
                                f.write(json.dumps(u, ensure_ascii=False) + "\n")
elif not go:
    st.markdown(f'<div class="oc-hero" style="margin-top:42px"><p>No project open. {"Upload a video" if HOSTED else "Paste a link"} above, '
                'or pick a past project from the sidebar.</p></div>', unsafe_allow_html=True)
