// "Ask AI" in the editor runs on the user's side, never on the Quipcuts servers:
//  - with their own Claude API key: the browser calls Anthropic directly; the key stays in this browser
//  - without one: a small open model runs inside the browser on their GPU (WebLLM / WebGPU)
import type { MLCEngine } from "@mlc-ai/web-llm";
import type { Recipe } from "./api";

export type Mode = "review" | "hooks" | "ctas" | "titles" | "ask";
export type Advice = { reply: string; hooks: string[]; ctas: string[]; titles: string[] };

const SYSTEM = `You help creators turn a clip from a longer video into a short that performs on YouTube Shorts, TikTok and Instagram Reels.
You get the clip's transcript, its length and its current on-screen text. Be specific to this clip, short and practical.
On-screen text has to be readable in under two seconds: hooks and CTAs are at most 8 words, no hashtags, at most one emoji.
Don't invent facts that aren't in the transcript.
Reply with JSON only, no prose around it:
{"reply": "<advice in 1-4 short sentences, plain text>", "hooks": ["..."], "ctas": ["..."], "titles": ["..."]}
Fill the lists that fit the request (3 items each) and leave the others empty.`;

const ASKS: Record<Mode, string> = {
  hooks: "Suggest 3 hooks for the first 3 seconds that make someone stop scrolling.",
  ctas: "Suggest 3 CTAs for the last seconds that fit what this clip is about.",
  titles: "Suggest 3 titles for the upload (YouTube Shorts style, under 60 characters).",
  review: "Review this clip as a short: is the start strong enough, does it end well, and what one change would help most? Include a hook and a CTA if they'd help.",
  ask: "",
};

function prompt(r: Recipe, mode: Mode, question: string, maxWords: number): string {
  const words = r.words.map((w) => w.word.trim()).filter(Boolean);
  const text = words.slice(0, maxWords).join(" ");
  const overlays = r.overlays.map((o) => `${o.kind} "${o.text}" at ${o.start.toFixed(1)}-${o.end.toFixed(1)}s`).join("; ") || "none";
  const ask = [ASKS[mode], question && `The creator asks: ${question}`].filter(Boolean).join("\n");
  return `Clip title: ${r.title}\nLength: ${Math.round(r.end - r.start)} seconds\nOn-screen text now: ${overlays}\n` +
    `Why it was picked: ${r.reason || "n/a"}\nTranscript: ${text}\n\nRequest: ${ask}`;
}

export function parseAdvice(text: string): Advice {
  const m = text.match(/\{[\s\S]*\}/);
  let data: Record<string, unknown> = {};
  try {
    data = m ? JSON.parse(m[0]) : {};
  } catch {
    /* fall through to plain text */
  }
  const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => String(x).trim().slice(0, 80)).filter(Boolean).slice(0, 5) : []);
  return {
    reply: String(data.reply ?? (m ? "" : text)).trim().slice(0, 1200),
    hooks: list(data.hooks),
    ctas: list(data.ctas),
    titles: list(data.titles),
  };
}

// ---------------------------------------------------------------- their Claude key

const KEY_STORE = "quipcuts.claudeKey";
export const getKey = () => {
  try {
    return localStorage.getItem(KEY_STORE) ?? "";
  } catch {
    return "";
  }
};
export const setKey = (k: string) => {
  try {
    if (k) localStorage.setItem(KEY_STORE, k);
    else localStorage.removeItem(KEY_STORE);
  } catch {
    /* private mode: the key lasts for this page only */
  }
};

async function askClaude(key: string, r: Recipe, mode: Mode, question: string): Promise<Advice> {
  let res: Response;
  try {
    res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        // the key belongs to the person using this browser, so calling from the page is intended
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify({
        model: "claude-haiku-5-5",
        max_tokens: 700,
        system: SYSTEM,
        messages: [{ role: "user", content: prompt(r, mode, question, 1500) }],
      }),
    });
  } catch {
    throw new Error("Couldn't reach Claude. Check your connection.");
  }
  if (res.status === 401) throw new Error("Claude rejected that API key. Check it under AI settings.");
  if (res.status === 429) throw new Error("Your Claude key hit its rate limit. Try again in a minute.");
  if (!res.ok) throw new Error(`Claude returned an error (${res.status}).`);
  const body = await res.json();
  return parseAdvice(body.content?.map((b: { text?: string }) => b.text ?? "").join("") ?? "");
}

// ---------------------------------------------------------------- on this device

export const LOCAL_MODELS = {
  small: { label: "Small (about 1 GB, faster)", id: "Qwen2.5-1.5B-Instruct" },
  better: { label: "Better (about 2 GB)", id: "Qwen2.5-3B-Instruct" },
} as const;
export type LocalSize = keyof typeof LOCAL_MODELS;

let engine: MLCEngine | null = null;
let engineModel = "";

export async function deviceSupport(): Promise<{ ok: boolean; f16: boolean }> {
  const gpu = (navigator as Navigator & { gpu?: { requestAdapter(): Promise<{ features: Set<string> } | null> } }).gpu;
  if (!gpu) return { ok: false, f16: false };
  const adapter = await gpu.requestAdapter().catch(() => null);
  return { ok: !!adapter, f16: !!adapter?.features.has("shader-f16") };
}

export const localReady = (size: LocalSize) => !!engine && engineModel.startsWith(LOCAL_MODELS[size].id);

async function askLocal(size: LocalSize, r: Recipe, mode: Mode, question: string, onProgress: (text: string) => void): Promise<Advice> {
  const support = await deviceSupport();
  if (!support.ok) throw new Error("This browser can't run AI on your device (it needs WebGPU: recent Chrome or Edge on a computer). Add a Claude key instead.");
  const id = `${LOCAL_MODELS[size].id}-${support.f16 ? "q4f16_1" : "q4f32_1"}-MLC`;
  if (!engine || engineModel !== id) {
    const webllm = await import("@mlc-ai/web-llm"); // ~6 MB of code, only loaded when someone uses it
    if (engine) await engine.unload();
    engine = null;
    onProgress("Downloading the model to this browser (one time)…");
    engine = await webllm.CreateMLCEngine(id, {
      initProgressCallback: (p) => onProgress(p.progress < 1 ? `Loading the model: ${Math.round(p.progress * 100)}%` : "Model ready"),
    });
    engineModel = id;
  }
  onProgress("Thinking on your device…");
  const out = await engine.chat.completions.create({
    messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt(r, mode, question, 600) }],
    temperature: 0.7,
    max_tokens: 500,
    response_format: { type: "json_object" },
  });
  return parseAdvice(out.choices[0]?.message?.content ?? "");
}

export function ask(r: Recipe, mode: Mode, question: string, size: LocalSize, onProgress: (text: string) => void): Promise<Advice> {
  const key = getKey();
  return key ? askClaude(key, r, mode, question) : askLocal(size, r, mode, question, onProgress);
}
