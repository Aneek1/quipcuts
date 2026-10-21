import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, fmtAgo, fmtLeft, fmtTime, useApi, useTitle, type ClipSummary, type Project } from "../api";

// Where the pipeline is, from its progress value and last log line (see openclip/pipeline.py).
function stage(p: Project): { now: number; detail: string } {
  const last = (p.log ?? []).at(-1) ?? "";
  if (p.status === "fetching") return { now: -2, detail: "" };
  if (p.status === "queued") return { now: -1, detail: "" };
  if (p.progress < 0.17) return { now: 0, detail: "" };
  if (p.progress < 0.4) return { now: 1, detail: "" };
  if (p.progress < 0.5) return { now: 2, detail: "" };
  const m = last.match(/\[(\d+)\/(\d+)\]/);
  return { now: 3, detail: m ? `clip ${m[1]} of ${m[2]}` : "" };
}

const STAGES = ["Reading the video", "Transcribing", "Picking moments", "Reframing and adding captions"];

function Progress({ p }: { p: Project }) {
  const s = stage(p);
  const detail = s.detail;
  // link projects get a download step in front, so every later step moves down one
  const yt = p.source === "youtube";
  const stages = yt ? ["Downloading from YouTube", ...STAGES] : STAGES;
  const now = yt ? (s.now === -2 ? 0 : s.now === -1 ? 1 : s.now + 1) : s.now;
  const [, tick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => tick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  const elapsed = (Date.now() - new Date(p.created_at).getTime()) / 1000;
  return (
    <div className="panel" aria-live="polite">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <b>
          {p.status === "fetching"
            ? "Downloading the video from YouTube"
            : p.status === "queued"
            ? p.queue_position && p.queue_position > 1
              ? `In line: ${p.queue_position - 1} ahead of you`
              : "Starting the GPU (up to a minute and a half if it was asleep)"
            : `${Math.round(p.progress * 100)}%`}
        </b>
        <span className="mono faint">{fmtTime(elapsed)} elapsed</span>
      </div>
      <div className="bar"><i style={{ width: `${Math.max(2, p.progress * 100)}%` }} /></div>
      <ol className="steps">
        {stages.map((s, i) => (
          <li key={s} className={i < now ? "done" : i === now ? "now" : ""}>
            <span className="dot" aria-hidden="true" />
            <span>{s}</span>
            <span className="mono">{i === now ? detail : i < now ? "done" : ""}</span>
          </li>
        ))}
      </ol>
      {(p.log?.length ?? 0) > 0 && (
        <details className="log">
          <summary>Show the log</summary>
          <pre>{p.log!.join("\n")}</pre>
        </details>
      )}
      <p className="small faint" style={{ marginTop: 14, marginBottom: 0 }}>
        You can close this tab. The project keeps going and will be under Projects.
      </p>
    </div>
  );
}

function ClipCard({ clip, rank }: { clip: ClipSummary; rank: number }) {
  return (
    <article className="clip">
      <div className="frame">
        {clip.video_url ? (
          // #t=0.1 makes browsers show a real frame instead of a black box before playback
          <video src={`${clip.video_url}#t=0.1`} controls preload="metadata" playsInline />
        ) : (
          <div className="ph">{clip.render_status === "failed" ? "render failed" : "rendering…"}</div>
        )}
        <span className="rank">#{rank}</span>
      </div>
      <div className="clip-meta">
        <div className="top">
          <h3 className="grow">{clip.title}</h3>
          {clip.score !== null && (
            <span className="score" title="The model's own estimate, not measured views">
              <b>{clip.score}</b>
              <small>score</small>
            </span>
          )}
        </div>
        <span className="mono faint">
          {fmtTime(clip.start)} → {fmtTime(clip.end)} · {Math.round(clip.end - clip.start)}s ·{" "}
          {clip.layout === "split" ? "two people" : "one person"}
        </span>
        {clip.reason && <p className="why">{clip.reason}</p>}
        {clip.download_url && (
          <div className="actions">
            <a className="btn sm primary" href={clip.download_url}>Download MP4</a>
          </div>
        )}
      </div>
    </article>
  );
}

export default function ProjectPage() {
  const { id } = useParams();
  const api = useApi();
  const navigate = useNavigate();
  const [p, setP] = useState<Project | null>(null);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  useTitle(p?.title);

  useEffect(() => {
    let stop = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const data = await api<Project>(`/api/projects/${id}`);
        if (stop) return;
        setP(data);
        if (["fetching", "queued", "processing"].includes(data.status)) timer = window.setTimeout(load, 2500);
      } catch (e) {
        if (!stop) setError((e as ApiError).message);
      }
    };
    load();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [api, id]);

  const remove = async () => {
    try {
      await api(`/api/projects/${id}`, { method: "DELETE" });
      navigate("/app");
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  if (error)
    return (
      <main className="wrap page">
        <div className="alert" role="alert">{error}</div>
        <Link to="/app" className="btn">Back to projects</Link>
      </main>
    );
  if (!p)
    return (
      <main className="wrap page">
        <div className="skeleton" style={{ height: 70, maxWidth: 520, marginBottom: 28 }} />
        <div className="clips">{[0, 1, 2].map((i) => <div key={i} className="skeleton" style={{ aspectRatio: "9 / 16" }} />)}</div>
      </main>
    );

  const busy = p.status === "fetching" || p.status === "queued" || p.status === "processing";
  const clips = [...(p.clips ?? [])].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  return (
    <main className="wrap page">
      <Link to="/app" className="mono faint" style={{ textDecoration: "none" }}>← Projects</Link>
      <div className="page-head" style={{ marginTop: 10 }}>
        <div className="grow">
          <h1 className="h-name" style={{ overflowWrap: "anywhere" }}>{p.title}</h1>
          <span className="mono faint">
            {p.duration ? `${fmtTime(p.duration)} video · ` : ""}uploaded {fmtAgo(p.created_at)}
            {p.model ? ` · picked by ${p.model}` : ""} · {fmtLeft(p.expires_at)}
          </span>
        </div>
        {!busy &&
          (confirmDelete ? (
            <span className="row">
              <span className="small muted">Delete the video and all its clips?</span>
              <button className="btn sm danger" onClick={remove}>Delete</button>
              <button className="btn sm ghost" onClick={() => setConfirmDelete(false)}>Keep</button>
            </span>
          ) : (
            <button className="btn sm ghost" onClick={() => setConfirmDelete(true)}>Delete project</button>
          ))}
      </div>

      {busy && <Progress p={p} />}
      {p.status === "failed" && (
        <div className="alert" role="alert">
          {p.error || "Processing failed."} This upload doesn't count toward your daily limit if you try again.
        </div>
      )}
      {p.status === "ready" && clips.length === 0 && (
        <p className="empty">The model didn't find any moments that stand on their own in this video.</p>
      )}
      {clips.length > 0 && (
        <div className="clips">
          {clips.map((c, i) => <ClipCard clip={c} rank={i + 1} key={c.id} />)}
        </div>
      )}
    </main>
  );
}
