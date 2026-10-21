import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, fmtAgo, fmtTime, putFile, useApi, useTitle, type Me, type Project } from "../api";

const ACCEPT = ".mp4,.mov,.mkv,.webm,.m4v";

/** Duration from the file's own metadata, so a too-long video is caught before uploading it. */
function readDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    const done = (d: number | null) => (URL.revokeObjectURL(url), resolve(d));
    v.preload = "metadata";
    v.onloadedmetadata = () => done(Number.isFinite(v.duration) ? v.duration : null);
    v.onerror = () => done(null); // e.g. MKV in Safari: the server checks it instead
    v.src = url;
  });
}

function Seg<T extends string | number>({ value, options, onChange, label }: {
  value: T; options: [T, string][]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="field">
      <span>{label}</span>
      <div className="seg" role="group" aria-label={label}>
        {options.map(([v, text]) => (
          <button type="button" key={String(v)} aria-pressed={v === value} onClick={() => onChange(v)}>{text}</button>
        ))}
      </div>
    </div>
  );
}

function NewProject({ me, onError }: { me: Me; onError: (msg: string) => void }) {
  const api = useApi();
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [drag, setDrag] = useState(false);
  const [layout, setLayout] = useState("auto");
  const [maxClips, setMaxClips] = useState(3);
  const [length, setLength] = useState("30-60");
  const [uploading, setUploading] = useState<number | null>(null);
  const [mode, setMode] = useState<"file" | "link">("file");
  const [link, setLink] = useState("");
  const left = me.limits.projects_per_day - me.used_today;
  const linkOk = /^https?:\/\/(www\.|m\.|music\.)?(youtube\.com\/(watch\?|shorts\/|live\/)|youtu\.be\/)\S+$/.test(link.trim());

  const pick = async (f: File | undefined) => {
    if (!f) return;
    onError("");
    if (f.size > me.limits.max_upload_mb * 1048576) {
      onError(`That file is ${(f.size / 1048576).toFixed(0)} MB. The limit is ${me.limits.max_upload_mb} MB.`);
      return;
    }
    const d = await readDuration(f);
    if (d && d > me.limits.max_video_min * 60 + 1) {
      onError(`That video is ${fmtTime(d)} long. The free beta takes up to ${me.limits.max_video_min} minutes; trim it first.`);
      return;
    }
    setFile(f);
    setDuration(d);
  };

  const opts = () => {
    const [min, max] = length.split("-").map(Number);
    return { layout, max_clips: maxClips, min_duration: min, max_duration: max };
  };

  const startLink = async () => {
    try {
      setUploading(1);
      const p = await api<{ id: string }>("/api/projects/from-link", {
        method: "POST",
        body: JSON.stringify({ url: link.trim(), ...opts() }),
      });
      navigate(`/app/p/${p.id}`);
    } catch (e) {
      setUploading(null);
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const start = async () => {
    if (mode === "link") return startLink();
    if (!file) return;
    try {
      setUploading(0);
      const contentType = file.type || "video/mp4";
      const up = await api<{ project_id: string; upload_url: string; content_type: string }>("/api/uploads", {
        method: "POST",
        body: JSON.stringify({ filename: file.name, size: file.size, content_type: contentType }),
      });
      await putFile(up.upload_url, file, up.content_type, setUploading);
      await api(`/api/projects/${up.project_id}/start`, {
        method: "POST",
        body: JSON.stringify(opts()),
      });
      navigate(`/app/p/${up.project_id}`);
    } catch (e) {
      setUploading(null);
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const busy = uploading !== null;
  const ready = mode === "file" ? !!file : linkOk && me.links;
  return (
    <div className="panel">
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={mode === "file"} onClick={() => setMode("file")}>Upload a file</button>
        <button role="tab" aria-selected={mode === "link"} onClick={() => setMode("link")}>
          Paste a YouTube link
          <span className={`live ${me.links ? "on" : ""}`} title={me.links ? "The link downloader is online" : "The link downloader is offline"} />
        </button>
      </div>
      {mode === "link" ? (
        <div className="linkbox">
          <input
            type="url"
            inputMode="url"
            placeholder="https://www.youtube.com/watch?v=…"
            value={link}
            onChange={(e) => setLink(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ready && !busy && start()}
            disabled={busy}
            aria-label="YouTube link"
          />
          <p className="mono faint" style={{ margin: "10px 0 0" }}>
            {me.links
              ? `Public videos up to ${me.limits.max_video_min} min, and only ones you own or have permission to use. Uploading the file is more reliable.`
              : "The link downloader is offline right now (it runs on a home PC). Upload the file instead, or try later."}
          </p>
        </div>
      ) : (
      <div
        className={`drop ${drag ? "over" : ""} ${file ? "has" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => !busy && input.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && !busy && input.current?.click()}
        onDragOver={(e) => (e.preventDefault(), setDrag(true))}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => (e.preventDefault(), setDrag(false), !busy && pick(e.dataTransfer.files[0]))}
      >
        <input ref={input} type="file" accept={ACCEPT} hidden onChange={(e) => pick(e.target.files?.[0])} />
        {file ? (
          <>
            <b>{file.name}</b>
            <span className="mono faint">
              {duration ? `${fmtTime(duration)} · ` : ""}{(file.size / 1048576).toFixed(0)} MB
              {!busy && " · click to change"}
            </span>
          </>
        ) : (
          <>
            <span className="icon" aria-hidden="true">+</span>
            <b>Drop a video here, or click to choose</b>
            <span className="mono faint">
              up to {me.limits.max_video_min} min · {me.limits.max_upload_mb} MB · mp4 mov mkv webm
            </span>
          </>
        )}
      </div>
      )}

      <div className="opts">
        <Seg label="Layout" value={layout} onChange={setLayout}
             options={[["auto", "Auto"], ["single", "One person"], ["split", "Two people"]]} />
        <Seg label="Clip length" value={length} onChange={setLength}
             options={[["15-30", "15–30s"], ["30-60", "30–60s"], ["60-90", "60–90s"]]} />
        <Seg label="Clips" value={maxClips} onChange={setMaxClips}
             options={[1, 2, 3, 4, 5].filter((n) => n <= me.limits.max_clips).map((n) => [n, String(n)] as [number, string])} />
        <button className="btn primary lg go" disabled={!ready || busy || left <= 0} onClick={start}>
          {!busy ? "Find clips" : uploading! < 1 ? `Uploading ${Math.round(uploading! * 100)}%` : "Starting…"}
        </button>
      </div>
      {busy && <div className="bar" aria-hidden="true"><i style={{ width: `${uploading! * 100}%` }} /></div>}
      <p className="rights">
        By starting, you confirm you own this video or have permission to use it. <Link to="/terms">Terms</Link>
      </p>
      <p className="quota">
        {left > 0
          ? `${left} of ${me.limits.projects_per_day} uploads left today · resets 00:00 UTC`
          : "No uploads left today · resets 00:00 UTC"}
      </p>
    </div>
  );
}

const STATUS: Record<Project["status"], string> = {
  uploading: "Uploading",
  fetching: "Downloading",
  queued: "In line",
  processing: "Working",
  ready: "Ready",
  failed: "Failed",
};

export default function Projects() {
  useTitle("Projects");
  const api = useApi();
  const [me, setMe] = useState<Me | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api<Me>("/api/me"), api<Project[]>("/api/projects")])
      .then(([m, p]) => (setMe(m), setProjects(p)))
      .catch((e: ApiError) => setError(e.message));
  }, [api]);

  return (
    <main className="wrap page">
      <div className="page-head">
        <div>
          <span className="eyebrow">New project</span>
          <h1>Upload a video</h1>
        </div>
      </div>
      {error && <div className="alert" role="alert">{error}</div>}
      {me ? <NewProject me={me} onError={setError} /> : !error && <div className="skeleton" style={{ height: 260 }} />}

      <div className="projects">
        <h2 style={{ fontSize: 20, padding: "18px 0 6px" }}>Your projects</h2>
        {!projects && !error && [0, 1].map((i) => <div key={i} className="skeleton" style={{ height: 52, margin: "10px 0" }} />)}
        {projects?.length === 0 && <p className="empty">Nothing here yet. Your first upload will show up here.</p>}
        {projects?.map((p) => (
          <Link to={`/app/p/${p.id}`} className="prow" key={p.id}>
            <span>
              <b>{p.title}</b>
              <span className="mono faint">
                {p.duration ? `${fmtTime(p.duration)} video` : "video"}
                {p.status === "ready" ? ` · ${p.clip_count} clip${p.clip_count === 1 ? "" : "s"}` : ""}
              </span>
            </span>
            <span className="mono faint when">{fmtAgo(p.created_at)}</span>
            <span className={`status ${p.status}`}>{STATUS[p.status]}</span>
          </Link>
        ))}
      </div>
    </main>
  );
}
