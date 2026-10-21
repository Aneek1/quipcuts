import { useCallback, useEffect } from "react";
import { useSession } from "./auth";

export const API_URL = ((import.meta.env.VITE_API_URL as string) || "http://localhost:8000").replace(/\/$/, "");

export type ClipSummary = {
  id: string;
  idx: number;
  title: string;
  score: number | null;
  reason: string;
  start: number;
  end: number;
  layout: "single" | "split";
  render_status: "none" | "queued" | "rendering" | "ready" | "failed";
  render_error: string | null;
  video_url: string | null;
  download_url: string | null;
  stale: boolean;
};

export type Project = {
  id: string;
  title: string;
  status: "uploading" | "fetching" | "queued" | "processing" | "ready" | "failed";
  source: "upload" | "youtube";
  progress: number;
  error: string | null;
  duration: number | null;
  model: string | null;
  created_at: string;
  finished_at: string | null;
  expires_at: string;
  clip_count?: number;
  clips?: ClipSummary[];
  log?: string[];
  queue_position?: number;
};

export type Me = {
  id: string;
  dev: boolean;
  used_today: number;
  links: boolean; // the home-PC link downloader is online
  limits: { projects_per_day: number; max_upload_mb: number; max_video_min: number; max_clips: number; keep_days: number };
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function errorText(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg ?? String(d)).join("; ");
  return `Request failed (${status}).`;
}

export function useApi() {
  const { getToken } = useSession();
  return useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      const token = await getToken();
      const headers = new Headers(init.headers);
      if (token) headers.set("Authorization", `Bearer ${token}`);
      if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      let res: Response;
      try {
        res = await fetch(API_URL + path, { ...init, headers });
      } catch {
        throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
      }
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new ApiError(res.status, errorText(body, res.status));
      return body as T;
    },
    [getToken],
  );
}

/** PUT a file to a signed URL with progress (fetch has no upload progress). */
export function putFile(url: string, file: File, contentType: string, onProgress: (f: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(`Upload failed (${xhr.status}).`)));
    xhr.onerror = () => reject(new Error("Upload failed. Check your connection and try again."));
    xhr.send(file);
  });
}

export const fmtTime = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export function fmtAgo(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function fmtLeft(iso: string): string {
  const d = Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000);
  return d <= 0 ? "deleting soon" : d === 1 ? "deleted tomorrow" : `deleted in ${d} days`;
}

export function useTitle(title?: string) {
  useEffect(() => {
    document.title = title ? `${title} · Quipcuts` : "Quipcuts: long videos into captioned vertical clips";
  }, [title]);
}
