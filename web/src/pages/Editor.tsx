import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError, fmtTime, useApi, useTitle,
  type CaptionStyle, type ClipDetail, type Keyframe, type Overlay, type Recipe, type Word,
} from "../api";
import { ask, getKey, LOCAL_MODELS, localReady, setKey, type Advice, type LocalSize, type Mode } from "../assist";

// ------------------------------------------------------------------ recipe helpers

const CAPTION_DEFAULTS: CaptionStyle = { font: null, size: 96, enabled: true, position: null, highlight: "#FFFF00", uppercase: true };
const MIN_LEN = 3;
const MAX_LEN = 180;
const OVERLAY_Y = { top: 0.14, middle: 0.42, bottom: 0.78 } as const;

/** Recipes saved before captions options and overlays existed don't have those fields. */
function normalize(r: Recipe): Recipe {
  return { ...r, captions: { ...CAPTION_DEFAULTS, ...r.captions }, overlays: r.overlays ?? [], zoom: r.zoom ?? { enabled: false, strength: 0.08 } };
}

// Punch-in zooms: same rules as openclip/emphasis.py, so the preview zooms where the render will.
const Z = { easeIn: 0.12, easeOut: 0.3, hold: 1.1, minGap: 4, every: 6, minZ: 0.8 };
function pickZooms(r: Recipe): number[] {
  const dur = r.end - r.start;
  const cands: [number, number][] = [];
  for (const w of r.words) {
    const t = w.start - r.start;
    if (w.loud == null || w.loud < Z.minZ || w.end - w.start < 0.12 || t < 0.5 || t > dur - Z.easeOut - 0.3 || !w.word.trim()) continue;
    cands.push([w.loud, Math.round(t * 1000) / 1000]);
  }
  cands.sort((a, b) => b[0] - a[0] || a[1] - b[1]);
  const picked: number[] = [];
  const budget = Math.max(1, Math.floor(dur / Z.every));
  for (const [, t] of cands) {
    if (picked.length >= budget) break;
    if (picked.every((p) => Math.abs(t - p) >= Z.minGap)) picked.push(t);
  }
  return picked.sort((a, b) => a - b);
}
function zoomAt(t: number, zooms: number[], strength: number): number {
  let z = 1;
  for (const s of zooms) {
    if (t < s || t > s + Z.easeIn + Z.hold + Z.easeOut) continue;
    let k: number;
    if (t < s + Z.easeIn) k = 1 - (1 - (t - s) / Z.easeIn) ** 3;
    else if (t < s + Z.easeIn + Z.hold) k = 1;
    else {
      const u = 1 - (t - s - Z.easeIn - Z.hold) / Z.easeOut;
      k = u * u * (3 - 2 * u);
    }
    z = Math.max(z, 1 + strength * k);
  }
  return z;
}
/** Shrink a crop box around the point the renderer zooms towards (centre, a bit above middle). */
function zoomBox(k: Keyframe, z: number): Keyframe {
  if (z <= 1) return k;
  const w = k.w / z;
  const h = k.h / z;
  return { ...k, x: k.x + (k.w - w) / 2, y: k.y + (k.h - h) * 0.35, w, h };
}

const cropLists = (r: Recipe): ("single" | "face_1" | "face_2")[] => (r.layout === "split" ? ["face_1", "face_2"] : ["single"]);

/** Crop box at clip-relative time t, the same way the renderer interpolates (hold across cuts). */
function cropAt(kfs: Keyframe[], t: number): Keyframe {
  let i = 0;
  while (i + 1 < kfs.length && kfs[i + 1].time <= t) i++;
  const a = kfs[i];
  const b = kfs[i + 1];
  if (!b || b.cut || t <= a.time) return a;
  const f = (t - a.time) / (b.time - a.time || 1);
  return { ...a, x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
}

/** Keyframe times are relative to the clip start, so moving the start shifts them. */
function shiftKeyframes(kfs: Keyframe[], delta: number): Keyframe[] {
  const moved = kfs.map((k) => ({ ...k, time: +(k.time + delta).toFixed(3) }));
  const before = moved.filter((k) => k.time <= 0);
  const after = moved.filter((k) => k.time > 0);
  const first = before.length ? { ...before[before.length - 1], time: 0, cut: false } : { ...after[0], time: 0, cut: false };
  return [first, ...after];
}

/** The clip's words from the full transcript, keeping any edits the user made to words in range. */
function wordsFor(start: number, end: number, all: Word[] | null, current: Word[]): Word[] {
  const known = new Map(current.map((w) => [w.start.toFixed(2), w]));
  const pool = all ?? current;
  return pool
    .filter((w) => w.end > start && w.start < end)
    .map((w) => {
      const k = known.get(w.start.toFixed(2));
      // keep edits and loudness; words new to the clip get loudness measured at render time
      return { word: k?.word ?? w.word, start: w.start, end: w.end, loud: k?.loud ?? null };
    });
}

function trim(r: Recipe, start: number, end: number, all: Word[] | null): Recipe {
  start = Math.max(0, +start.toFixed(2));
  end = Math.min(r.source.duration, +end.toFixed(2));
  const delta = r.start - start;
  const crop = { ...r.crop };
  for (const k of cropLists(r)) crop[k] = shiftKeyframes(r.crop[k]!, delta);
  const dur = end - start;
  const overlays = r.overlays
    .map((o) => {
      if (o.kind === "cta") {
        // a CTA stays pinned to the end of the clip
        const len = Math.min(o.end - o.start, dur);
        return { ...o, start: +(dur - len).toFixed(2), end: +dur.toFixed(2) };
      }
      return { ...o, end: Math.min(o.end, +dur.toFixed(2)) };
    })
    .filter((o) => o.end - o.start >= 0.3);
  return { ...r, start, end, crop, overlays, words: wordsFor(start, end, all, r.words) };
}

function nudge(r: Recipe, list: "single" | "face_1" | "face_2", frac: number): Recipe {
  const kfs = r.crop[list]!;
  const step = r.source.width * frac;
  const moved = kfs.map((k) => ({ ...k, x: Math.round(Math.min(r.source.width - k.w, Math.max(0, k.x + step))) }));
  return { ...r, crop: { ...r.crop, [list]: moved } };
}

// Caption pages, like build_ass_subtitles: up to 3 words / 18 characters, break on pauses and punctuation.
type Page = { words: { text: string; start: number; end: number }[]; end: number };
function captionPages(r: Recipe): Page[] {
  const dur = r.end - r.start;
  const prepared = r.words
    .map((w) => {
      const raw = (r.captions.uppercase ? w.word.trim().toUpperCase() : w.word.trim());
      return { raw, text: raw.replace(/[,.;:]+$/, "") || raw, start: Math.max(0, w.start - r.start), end: Math.min(dur, w.end - r.start) };
    })
    .filter((w) => w.raw && w.end > 0 && w.start < dur);
  const groups: (typeof prepared)[] = [];
  let cur: typeof prepared = [];
  for (const w of prepared) {
    if (cur.length) {
      const chars = cur.reduce((n, x) => n + x.text.length + 1, 0) + w.text.length;
      const gap = w.start - cur[cur.length - 1].end;
      if (cur.length >= 3 || chars > 18 || gap > 0.7 || /[.!?…,;:]$/.test(cur[cur.length - 1].raw)) {
        groups.push(cur);
        cur = [];
      }
    }
    cur.push(w);
  }
  if (cur.length) groups.push(cur);
  return groups.map((g, i) => ({
    words: g,
    end: Math.min(g[g.length - 1].end + 0.35, groups[i + 1]?.[0].start ?? dur, dur),
  }));
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

// ------------------------------------------------------------------ preview

function Preview({ r, video, t }: { r: Recipe; video: HTMLVideoElement | null; t: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const rel = t - r.start;
  const zooms = useMemo(() => (r.zoom.enabled ? pickZooms(r) : []), [r]);

  useEffect(() => {
    const c = canvas.current;
    if (!c || !video) return;
    const ctx = c.getContext("2d")!;
    let raf = 0;
    const draw = () => {
      const now = video.currentTime - r.start;
      const z = zooms.length ? zoomAt(now, zooms, r.zoom.strength) : 1;
      if (video.readyState >= 2) {
        if (r.layout === "split") {
          const a = zoomBox(cropAt(r.crop.face_1!, now), z);
          const b = zoomBox(cropAt(r.crop.face_2!, now), z);
          ctx.drawImage(video, a.x, a.y, a.w, a.h, 0, 0, c.width, c.height / 2);
          ctx.drawImage(video, b.x, b.y, b.w, b.h, 0, c.height / 2, c.width, c.height / 2);
        } else {
          const k = zoomBox(cropAt(r.crop.single!, now), z);
          ctx.drawImage(video, k.x, k.y, k.w, k.h, 0, 0, c.width, c.height);
        }
      }
      raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [video, r, zooms]);

  const pages = useMemo(() => captionPages(r), [r]);
  const page = r.captions.enabled ? pages.find((p) => p.words[0].start <= rel && rel < p.end) : undefined;
  const activeIdx = page ? page.words.reduce((a, w, i) => (w.start <= rel ? i : a), 0) : -1;
  const capY = r.captions.position ?? (r.layout === "split" ? 0.5 : 0.62);

  return (
    <div className="stage">
      <canvas ref={canvas} width={540} height={960} />
      {page && (
        <div className="cap" style={{ top: `${capY * 100}%`, fontSize: `${(r.captions.size / 1080) * 100}cqw` }}>
          {page.words.map((w, i) => (
            <span key={i} style={i === activeIdx ? { color: r.captions.highlight, fontSize: "1.1em" } : undefined}>{w.text} </span>
          ))}
        </div>
      )}
      {r.overlays
        .filter((o) => o.start <= rel && rel < o.end)
        .map((o, i) => (
          <div key={i} className={`ov ${o.style}`} style={{ top: `${OVERLAY_Y[o.position] * 100}%` }}>{o.text}</div>
        ))}
    </div>
  );
}

// ------------------------------------------------------------------ timeline

function Timeline({ r, base, t, all, onTrim, onSeek }: {
  r: Recipe; base: Recipe; t: number; all: Word[] | null;
  onTrim: (start: number, end: number) => void; onSeek: (t: number) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const from = Math.max(0, Math.min(base.start, r.start) - 30);
  const to = Math.min(r.source.duration, Math.max(base.end, r.end) + 30);
  const pct = (s: number) => `${((s - from) / (to - from)) * 100}%`;
  const drag = useRef<"start" | "end" | null>(null);

  const timeAt = (clientX: number) => {
    const box = track.current!.getBoundingClientRect();
    return from + Math.min(1, Math.max(0, (clientX - box.left) / box.width)) * (to - from);
  };
  // snap a handle to the nearest word edge within 0.3 s, so cuts don't land mid-word
  const snap = (s: number, edge: "start" | "end") => {
    const words = all ?? r.words;
    let best = s;
    let dist = 0.3;
    for (const w of words) {
      const v = edge === "start" ? w.start : w.end;
      if (Math.abs(v - s) < dist) {
        dist = Math.abs(v - s);
        best = v;
      }
    }
    return best;
  };
  const move = (e: RPointerEvent) => {
    if (!drag.current) return;
    const s = snap(timeAt(e.clientX), drag.current);
    if (drag.current === "start") onTrim(Math.min(Math.max(s, r.end - MAX_LEN), r.end - MIN_LEN), r.end);
    else onTrim(r.start, Math.max(Math.min(s, r.start + MAX_LEN), r.start + MIN_LEN));
  };
  const grab = (which: "start" | "end") => (e: RPointerEvent) => {
    e.stopPropagation();
    drag.current = which;
    track.current!.setPointerCapture(e.pointerId);
  };

  return (
    <div className="timeline">
      <div
        className="track"
        ref={track}
        onPointerMove={move}
        onPointerUp={() => (drag.current = null)}
        onPointerDown={(e) => !drag.current && onSeek(timeAt(e.clientX))}
      >
        <div className="sel" style={{ left: pct(r.start), width: `calc(${pct(r.end)} - ${pct(r.start)})` }} />
        <div className="picked" style={{ left: pct(base.start), width: `calc(${pct(base.end)} - ${pct(base.start)})` }} title="The range the model picked" />
        <button className="handle" style={{ left: pct(r.start) }} onPointerDown={grab("start")} aria-label="Clip start" />
        <button className="handle" style={{ left: pct(r.end) }} onPointerDown={grab("end")} aria-label="Clip end" />
        <div className="head" style={{ left: pct(Math.min(Math.max(t, from), to)) }} />
      </div>
      <div className="row mono faint" style={{ justifyContent: "space-between", marginTop: 8 }}>
        <span>{fmtTime(from)}</span>
        <span className="row" style={{ gap: 6 }}>
          <button className="btn sm ghost" onClick={() => onTrim(r.start - 0.5, r.end)}>Start −0.5s</button>
          <button className="btn sm ghost" onClick={() => onTrim(r.start + 0.5, r.end)}>+0.5s</button>
          <b className="ink">{fmtTime(r.start)} → {fmtTime(r.end)} · {(r.end - r.start).toFixed(1)}s</b>
          <button className="btn sm ghost" onClick={() => onTrim(r.start, r.end - 0.5)}>End −0.5s</button>
          <button className="btn sm ghost" onClick={() => onTrim(r.start, r.end + 0.5)}>+0.5s</button>
        </span>
        <span>{fmtTime(to)}</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ side panels

function Seg<T extends string | number>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="seg sm" role="group">
      {options.map(([v, label]) => (
        <button type="button" key={String(v)} aria-pressed={v === value} onClick={() => onChange(v)}>{label}</button>
      ))}
    </div>
  );
}

const CTA_PRESETS = ["Follow for part 2", "Subscribe for more", "Watch the full video", "Comment your take", "Link in bio"];

function TextPanel({ r, set, rel }: { r: Recipe; set: (r: Recipe) => void; rel: number }) {
  const dur = r.end - r.start;
  const add = (o: Overlay) => set({ ...r, overlays: [...r.overlays, o] });
  const update = (i: number, patch: Partial<Overlay>) =>
    set({ ...r, overlays: r.overlays.map((o, j) => (j === i ? { ...o, ...patch } : o)) });
  const remove = (i: number) => set({ ...r, overlays: r.overlays.filter((_, j) => j !== i) });
  const full = r.overlays.length >= 6;
  const t2 = (v: number) => +Math.min(dur, Math.max(0, v)).toFixed(1);

  return (
    <div className="stack">
      <p className="small muted" style={{ margin: 0 }}>
        A <b>hook</b> sits over the first seconds to stop the scroll. A <b>CTA</b> sits over the last seconds and tells
        people what to do next. Both are burned into the video.
      </p>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn sm" disabled={full} onClick={() => add({ kind: "hook", text: r.title.slice(0, 80), start: 0, end: t2(3), position: "top", style: "light" })}>+ Hook</button>
        <button className="btn sm" disabled={full} onClick={() => add({ kind: "cta", text: CTA_PRESETS[0], start: t2(dur - 3), end: t2(dur), position: "bottom", style: "accent" })}>+ CTA</button>
        <button className="btn sm ghost" disabled={full} onClick={() => add({ kind: "text", text: "Your text", start: t2(rel), end: t2(rel + 3), position: "middle", style: "dark" })}>+ Text at playhead</button>
      </div>
      {r.overlays.length === 0 && <p className="empty small">No text on this clip yet.</p>}
      {r.overlays.map((o, i) => (
        <div className="ovrow" key={i}>
          <div className="row" style={{ gap: 8 }}>
            <span className={`kind ${o.kind}`}>{o.kind}</span>
            <input className="grow" value={o.text} maxLength={80} onChange={(e) => update(i, { text: e.target.value })} aria-label="Text" />
            <button className="btn sm ghost" onClick={() => remove(i)} aria-label="Remove text">✕</button>
          </div>
          {o.kind === "cta" && (
            <div className="chips">
              {CTA_PRESETS.map((p) => (
                <button key={p} className={o.text === p ? "on" : ""} onClick={() => update(i, { text: p })}>{p}</button>
              ))}
            </div>
          )}
          <div className="row" style={{ gap: 10 }}>
            <label className="mini">from
              <input type="number" step={0.1} min={0} max={dur} value={o.start} onChange={(e) => update(i, { start: t2(+e.target.value) })} />
            </label>
            <label className="mini">to
              <input type="number" step={0.1} min={0} max={dur} value={o.end} onChange={(e) => update(i, { end: t2(+e.target.value) })} />
            </label>
            <Seg value={o.position} onChange={(v) => update(i, { position: v })} options={[["top", "Top"], ["middle", "Middle"], ["bottom", "Bottom"]]} />
            <Seg value={o.style} onChange={(v) => update(i, { style: v })} options={[["light", "Light"], ["dark", "Dark"], ["accent", "Orange"], ["plain", "Outline"]]} />
          </div>
        </div>
      ))}
    </div>
  );
}

const HIGHLIGHTS: [string, string][] = [["#FFFF00", "Yellow"], ["#FF4D1F", "Orange"], ["#2BD96B", "Green"], ["#22D3EE", "Cyan"], ["#FFFFFF", "None"]];

function CaptionPanel({ r, set }: { r: Recipe; set: (r: Recipe) => void }) {
  const c = r.captions;
  const cap = (patch: Partial<CaptionStyle>) => set({ ...r, captions: { ...c, ...patch } });
  return (
    <div className="stack">
      <label className="check">
        <input type="checkbox" checked={c.enabled} onChange={(e) => cap({ enabled: e.target.checked })} /> Show captions
      </label>
      <div className="field"><span>Size</span>
        <Seg value={c.size} onChange={(v) => cap({ size: v })} options={[[76, "Small"], [96, "Medium"], [118, "Large"]]} />
      </div>
      <div className="field"><span>Position</span>
        <Seg value={c.position ?? -1} onChange={(v) => cap({ position: v === -1 ? null : v })}
             options={[[0.3, "High"], [0.5, "Centre"], [-1, "Default"], [0.72, "Low"]]} />
      </div>
      <div className="field"><span>Spoken word colour</span>
        <div className="swatches">
          {HIGHLIGHTS.map(([hex, name]) => (
            <button key={hex} aria-pressed={c.highlight === hex} title={name} style={{ background: hex }} onClick={() => cap({ highlight: hex })} />
          ))}
        </div>
      </div>
      <label className="check">
        <input type="checkbox" checked={c.uppercase} onChange={(e) => cap({ uppercase: e.target.checked })} /> ALL CAPS
      </label>
    </div>
  );
}

function WordsPanel({ r, set, rel, seek }: { r: Recipe; set: (r: Recipe) => void; rel: number; seek: (t: number) => void }) {
  const [sel, setSel] = useState<number | null>(null);
  const w = sel !== null ? r.words[sel] : null;
  const edit = (word: string) => set({ ...r, words: r.words.map((x, i) => (i === sel ? { ...x, word } : x)) });
  return (
    <div className="stack">
      <p className="small muted" style={{ margin: 0 }}>Click a word to jump there and fix it. Hiding a word only removes it from the captions, not the audio.</p>
      {w && (
        <div className="row" style={{ gap: 8 }}>
          <input className="grow" value={w.word} onChange={(e) => edit(e.target.value)} aria-label="Caption word" autoFocus />
          <button className="btn sm ghost" onClick={() => edit("")}>Hide</button>
        </div>
      )}
      <div className="words">
        {r.words.map((x, i) => {
          const now = x.start - r.start <= rel && rel < x.end - r.start;
          return (
            <button key={x.start} className={`${i === sel ? "sel" : ""} ${now ? "now" : ""} ${x.word.trim() ? "" : "off"}`}
                    onClick={() => (setSel(i), seek(x.start))}>
              {x.word.trim() || "·"}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function FramingPanel({ r, set, seek }: { r: Recipe; set: (r: Recipe) => void; seek: (t: number) => void }) {
  const rows: ["single" | "face_1" | "face_2", string][] =
    r.layout === "split" ? [["face_1", "Top panel"], ["face_2", "Bottom panel"]] : [["single", "Frame"]];
  const zoom = (patch: Partial<Recipe["zoom"]>) => set({ ...r, zoom: { ...r.zoom, ...patch } });
  const measured = r.words.some((w) => w.loud != null);
  const zooms = r.zoom.enabled ? pickZooms(r) : [];
  return (
    <div className="stack">
      <label className="check">
        <input type="checkbox" checked={r.zoom.enabled} onChange={(e) => zoom({ enabled: e.target.checked })} /> Punch-in zoom on the loudest moments
      </label>
      {r.zoom.enabled && (
        <>
          <div className="field"><span>Zoom</span>
            <Seg value={r.zoom.strength} onChange={(v) => zoom({ strength: v })} options={[[0.05, "Subtle"], [0.08, "Medium"], [0.12, "Strong"]]} />
          </div>
          {measured ? (
            zooms.length ? (
              <div className="row small" style={{ gap: 6 }}>
                <span className="muted">Zooms at</span>
                {zooms.map((z) => (
                  <button key={z} className="btn sm ghost" onClick={() => seek(r.start + z - 0.5)}>{z.toFixed(1)}s</button>
                ))}
              </div>
            ) : (
              <p className="small muted" style={{ margin: 0 }}>No moment stands out enough in this clip, so it won't zoom.</p>
            )
          ) : (
            <p className="small muted" style={{ margin: 0 }}>The loudest moments are measured when you render; the preview shows them after that.</p>
          )}
        </>
      )}
      <hr className="rule" />
      <p className="small muted" style={{ margin: 0 }}>Face tracking still follows the speaker; this shifts the whole path sideways.</p>
      {rows.map(([list, label]) => (
        <div className="row" key={list} style={{ gap: 8 }}>
          <span className="grow">{label}</span>
          <button className="btn sm" onClick={() => set(nudge(r, list, -0.03))}>← Left</button>
          <button className="btn sm" onClick={() => set(nudge(r, list, 0.03))}>Right →</button>
        </div>
      ))}
    </div>
  );
}

type Exchange = { ask: string; advice?: Advice; error?: string; status?: string };

function AiSettings({ size, setSize, onClose }: { size: LocalSize; setSize: (s: LocalSize) => void; onClose: () => void }) {
  const [key, setKeyInput] = useState(getKey());
  const saved = getKey();
  return (
    <div className="ovrow">
      <b className="small">Where the AI runs</b>
      <p className="small muted" style={{ margin: 0 }}>
        Quipcuts doesn't run this on its servers. With your own Claude API key, your browser asks Claude directly and the key stays
        in this browser. Without a key, a small model runs on this computer's GPU after a one-time download.
      </p>
      <div className="row" style={{ gap: 8 }}>
        <input className="grow" type="password" value={key} placeholder="Claude API key (sk-ant-…), optional"
               onChange={(e) => setKeyInput(e.target.value.trim())} aria-label="Claude API key" autoComplete="off" />
        <button className="btn sm" disabled={!key.startsWith("sk-ant-") || key === saved} onClick={() => (setKey(key), onClose())}>Save key</button>
        {saved && <button className="btn sm ghost" onClick={() => (setKey(""), setKeyInput(""))}>Remove</button>}
      </div>
      <div className="row" style={{ gap: 8 }}>
        <span className="small grow">Model on this device when there's no key</span>
        <Seg value={size} onChange={setSize} options={(Object.keys(LOCAL_MODELS) as LocalSize[]).map((k) => [k, LOCAL_MODELS[k].label])} />
      </div>
    </div>
  );
}

function AskPanel({ r, set }: { r: Recipe; set: (r: Recipe) => void }) {
  const [q, setQ] = useState("");
  const [log, setLog] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState(false);
  const [size, setSize] = useState<LocalSize>("small");
  const dur = r.end - r.start;
  const where = getKey() ? "your Claude key" : localReady(size) ? "this device (model loaded)" : "this device";

  const run = async (mode: Mode, label: string, question = "") => {
    setBusy(true);
    const i = log.length;
    const patch = (p: Partial<Exchange>) => setLog((l) => l.map((x, j) => (j === i ? { ...x, ...p } : x)));
    setLog((l) => [...l, { ask: label }]);
    try {
      const advice = await ask(r, mode, question, size, (status) => patch({ status }));
      patch({ advice });
    } catch (e) {
      patch({ error: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  // applying a hook or CTA replaces the existing one of that kind, or adds it
  const place = (kind: "hook" | "cta", text: string) => {
    const existing = r.overlays.findIndex((o) => o.kind === kind);
    const fresh: Overlay = kind === "hook"
      ? { kind, text, start: 0, end: +Math.min(3, dur).toFixed(1), position: "top", style: "light" }
      : { kind, text, start: +Math.max(0, dur - 3).toFixed(1), end: +dur.toFixed(1), position: "bottom", style: "accent" };
    const overlays = existing >= 0 ? r.overlays.map((o, j) => (j === existing ? { ...o, text } : o)) : [...r.overlays, fresh];
    set({ ...r, overlays: overlays.slice(0, 6) });
  };
  const has = (kind: string, text: string) => r.overlays.some((o) => o.kind === kind && o.text === text);

  return (
    <div className="stack">
      <div className="row" style={{ gap: 8 }}>
        <p className="small muted grow" style={{ margin: 0 }}>
          Advice for this clip from its transcript. Suggestions only change the clip when you click Use.
          <span className="mono faint"> Runs on {where}.</span>
        </p>
        <button className="btn sm ghost" onClick={() => setSettings((s) => !s)}>AI settings</button>
      </div>
      {settings && <AiSettings size={size} setSize={setSize} onClose={() => setSettings(false)} />}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn sm" disabled={busy} onClick={() => run("review", "Review this clip")}>Review this clip</button>
        <button className="btn sm ghost" disabled={busy} onClick={() => run("hooks", "Hook ideas")}>Hook ideas</button>
        <button className="btn sm ghost" disabled={busy} onClick={() => run("ctas", "CTA ideas")}>CTA ideas</button>
        <button className="btn sm ghost" disabled={busy} onClick={() => run("titles", "Title ideas")}>Title ideas</button>
      </div>
      <form className="row" style={{ gap: 8 }} onSubmit={(e) => {
        e.preventDefault();
        if (q.trim() && !busy) (run("ask", q.trim(), q.trim()), setQ(""));
      }}>
        <input className="grow" value={q} maxLength={500} onChange={(e) => setQ(e.target.value)}
               placeholder="Ask anything, e.g. is the ending too abrupt?" aria-label="Question for the AI" />
        <button className="btn sm primary" disabled={busy || !q.trim()}>Ask</button>
      </form>
      <div className="asklog">
        {[...log].reverse().map((x, i) => (
          <div className="ex" key={log.length - i}>
            <b className="small">{x.ask}</b>
            {!x.advice && !x.error && <p className="small faint">{x.status ?? "Thinking…"}</p>}
            {x.error && <p className="small" style={{ color: "var(--bad)" }}>{x.error}</p>}
            {x.advice && (
              <>
                {x.advice.reply && <p className="small">{x.advice.reply}</p>}
                {([["hooks", "hook", "Hook"], ["ctas", "cta", "CTA"]] as const).map(([key, kind, label]) =>
                  x.advice![key].map((s) => (
                    <div className="sugg" key={key + s}>
                      <span className={`kind ${kind}`}>{label}</span>
                      <span className="grow">{s}</span>
                      <button className="btn sm" disabled={has(kind, s)} onClick={() => place(kind, s)}>{has(kind, s) ? "Used" : "Use"}</button>
                    </div>
                  )),
                )}
                {x.advice.titles.map((s) => (
                  <div className="sugg" key={"t" + s}>
                    <span className="kind text">Title</span>
                    <span className="grow">{s}</span>
                    <button className="btn sm" disabled={r.title === s} onClick={() => set({ ...r, title: s })}>{r.title === s ? "Used" : "Use"}</button>
                  </div>
                ))}
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ page

type Tab = "ask" | "text" | "captions" | "words" | "framing";

export default function Editor() {
  const { id } = useParams();
  const api = useApi();
  const [clip, setClip] = useState<ClipDetail | null>(null);
  const [saved, setSaved] = useState<Recipe | null>(null); // what the server has
  const [base, setBase] = useState<Recipe | null>(null);   // as loaded, for the "picked" marker
  const [r, setR] = useState<Recipe | null>(null);
  const [all, setAll] = useState<Word[] | null>(null);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [tab, setTab] = useState<Tab>("text");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useTitle(r ? `Edit: ${r.title}` : "Edit clip");

  const load = useCallback(async () => {
    const c = await api<ClipDetail>(`/api/clips/${id}`);
    const rec = normalize(c.recipe);
    setClip(c);
    setSaved(rec);
    return { c, rec };
  }, [api, id]);

  useEffect(() => {
    load()
      .then(({ c, rec }) => {
        setR(rec);
        setBase(rec);
        if (c.transcript_url)
          fetch(c.transcript_url).then((res) => res.json()).then((tr) => setAll(tr.words)).catch(() => setAll(null));
      })
      .catch((e: ApiError) => setError(e.message));
  }, [load]);

  // keep playback inside the clip and loop it
  useEffect(() => {
    if (!video || !r) return;
    let raf = 0;
    let last = -1;
    const tick = () => {
      if (video.currentTime >= r.end || video.currentTime < r.start - 0.25) video.currentTime = r.start;
      // the canvas draws every frame on its own; the rest of the page only needs ~15 updates a second
      if (Math.abs(video.currentTime - last) > 0.066) setT((last = video.currentTime));
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [video, r]);

  const dirty = !!r && !!saved && !same(r, saved);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const toggle = useCallback(() => {
    if (!video) return;
    if (video.paused) video.play().then(() => setPlaying(true)).catch(() => {});
    else (video.pause(), setPlaying(false));
  }, [video]);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (e.code === "Space" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "BUTTON") {
        e.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [toggle]);

  const seek = (s: number) => video && (video.currentTime = s);

  // poll while the clip renders
  useEffect(() => {
    if (!clip || !["queued", "rendering"].includes(clip.render_status)) return;
    const timer = window.setTimeout(() => load().catch(() => {}), 2000);
    return () => window.clearTimeout(timer);
  }, [clip, load]);

  const saveAndRender = async () => {
    if (!r) return;
    setBusy(true);
    setError("");
    try {
      const clean = { ...r, title: r.title.trim() || "Clip", overlays: r.overlays.filter((o) => o.text.trim()) };
      await api(`/api/clips/${id}/recipe`, { method: "PUT", body: JSON.stringify(clean) });
      await api(`/api/clips/${id}/render`, { method: "POST" });
      const { rec } = await load();
      setR(rec);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !clip)
    return (
      <main className="wrap page">
        <div className="alert" role="alert">{error}</div>
        <Link to="/app" className="btn">Back to projects</Link>
      </main>
    );
  if (!clip || !r || !base)
    return (
      <main className="wrap page">
        <div className="editor">
          <div className="skeleton" style={{ aspectRatio: "9 / 16", maxWidth: 340 }} />
          <div className="skeleton" style={{ height: 320 }} />
        </div>
      </main>
    );

  const rel = t - r.start;
  const rendering = ["queued", "rendering"].includes(clip.render_status);
  return (
    <main className="wrap page">
      <Link to={`/app/p/${clip.project_id}`} className="mono faint" style={{ textDecoration: "none" }}>← {clip.project_title}</Link>
      <div className="page-head" style={{ marginTop: 10 }}>
        <input className="title-input grow" value={r.title} maxLength={120} onChange={(e) => setR({ ...r, title: e.target.value })} aria-label="Clip title" />
        <div className="row" style={{ gap: 8 }}>
          {dirty && <button className="btn sm ghost" onClick={() => setR(saved)}>Undo changes</button>}
          <button className="btn primary" disabled={busy || rendering || (!dirty && !clip.stale)} onClick={saveAndRender}>
            {busy ? "Saving…" : rendering ? "Rendering…" : dirty || clip.stale ? "Save & render" : "Saved"}
          </button>
        </div>
      </div>
      {error && <div className="alert" role="alert">{error}</div>}

      <div className="editor">
        <div className="left">
          <video ref={setVideo} src={clip.source_url} preload="auto" playsInline hidden
                 onLoadedMetadata={(e) => (e.currentTarget.currentTime = r.start)} onPause={() => setPlaying(false)} />
          <Preview r={r} video={video} t={t} />
          <div className="row" style={{ justifyContent: "space-between", marginTop: 10 }}>
            <button className="btn sm" onClick={toggle}>{playing ? "Pause" : "Play"}</button>
            <span className="mono faint">{fmtTime(Math.max(0, rel))} / {fmtTime(r.end - r.start)}</span>
          </div>
          <p className="mono faint" style={{ marginTop: 8 }}>Preview. The export uses the caption font and exact timing.</p>
        </div>

        <div className="right panel">
          <div className="tabs" role="tablist">
            {([["text", "Hook & CTA"], ["ask", "Ask AI"], ["captions", "Captions"], ["words", "Words"], ["framing", "Zoom & framing"]] as [Tab, string][]).map(([k, label]) => (
              <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</button>
            ))}
          </div>
          {tab === "text" && <TextPanel r={r} set={setR} rel={rel} />}
          <div hidden={tab !== "ask"}><AskPanel r={r} set={setR} /></div>
          {tab === "captions" && <CaptionPanel r={r} set={setR} />}
          {tab === "words" && <WordsPanel r={r} set={setR} rel={rel} seek={seek} />}
          {tab === "framing" && <FramingPanel r={r} set={setR} seek={seek} />}
        </div>
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <span className="eyebrow">Trim</span>
        <Timeline r={r} base={base} t={t} all={all} onSeek={seek}
                  onTrim={(s, e) => setR(trim(r, Math.max(0, s), Math.min(r.source.duration, e), all))} />
      </div>

      <div className="panel result" style={{ marginTop: 18 }}>
        <span className="eyebrow">Exported clip</span>
        {rendering ? (
          <p className="muted">Rendering on the GPU. This usually takes under a minute, longer if the GPU was asleep.</p>
        ) : clip.render_status === "failed" ? (
          <div className="alert">The render failed: {clip.render_error?.split("\n")[0]}</div>
        ) : clip.video_url ? (
          <div className="row" style={{ alignItems: "flex-start", gap: 20 }}>
            <video key={clip.video_url} src={`${clip.video_url}#t=0.1`} controls preload="metadata" playsInline className="out" />
            <div className="stack">
              <p className="small muted" style={{ margin: 0 }}>
                {clip.stale || dirty ? "This file is from before your latest changes. Save & render to update it." : "Up to date with your edits."}
              </p>
              {clip.download_url && <a className="btn sm primary" href={clip.download_url}>Download MP4</a>}
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
