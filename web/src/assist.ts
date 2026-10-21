// "Ask AI" in the editor runs on the user's side, never on the Quipcuts servers:
//  - with their own Claude or Mistral API key: the browser calls that API directly; the key stays in this browser
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

// ---------------------------------------------------------------- their own API key (Claude or Mistral)

export type Provider = "claude" | "mistral";
export const PROVIDERS: Record<Provider, { label: string; hint: string; looksValid: (k: string) => boolean; keysUrl: string }> = {
  claude: { label: "Claude", hint: "sk-ant-…", looksValid: (k) => k.startsWith("sk-ant-"), keysUrl: "https://console.anthropic.com/settings/keys" },
  mistral: { label: "Mistral", hint: "32-character key", looksValid: (k) => /^[A-Za-z0-9]{20,}$/.test(k), keysUrl: "https://console.mistral.ai/api-keys" },
};

const store = {
  get(name: string) {
    try {
      return localStorage.getItem(name) ?? "";
    } catch {
      return "";
    }
  },
  set(name: string, v: string) {
    try {
      if (v) localStorage.setItem(name, v);
      else localStorage.removeItem(name);
    } catch {
      /* private mode: lasts for this page only */
    }
  },
};
const keyName = (p: Provider) => (p === "claude" ? "quipcuts.claudeKey" : `quipcuts.${p}Key`);
export const getKey = (p: Provider) => store.get(keyName(p));
export const setKey = (p: Provider, k: string) => store.set(keyName(p), k);
/** Which saved key to use: the chosen provider if it has a key, otherwise whichever one does. */
export function activeProvider(): Provider | null {
  const chosen = store.get("quipcuts.aiProvider") as Provider;
  if (chosen in PROVIDERS && getKey(chosen)) return chosen;
  return (Object.keys(PROVIDERS) as Provider[]).find((p) => getKey(p)) ?? null;
}
export const chooseProvider = (p: Provider) => store.set("quipcuts.aiProvider", p);

async function askMistral(key: string, r: Recipe, mode: Mode, question: string): Promise<Advice> {
  let res: Response;
  try {
    res = await fetch("https://api.mistral.ai/v1/chat/completions", {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        model: "mistral-small-latest",
        max_tokens: 700,
        temperature: 0.7,
        response_format: { type: "json_object" },
        messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt(r, mode, question, 1500) }],
      }),
    });
  } catch {
    throw new Error("Couldn't reach Mistral. Check your connection.");
  }
  if (res.status === 401) throw new Error("Mistral rejected that API key. Check it under AI settings.");
  if (res.status === 429) throw new Error("Your Mistral key hit its rate limit. Try again in a minute.");
  if (!res.ok) throw new Error(`Mistral returned an error (${res.status}).`);
  const body = await res.json();
  return parseAdvice(body.choices?.[0]?.message?.content ?? "");
}

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
  const p = activeProvider();
  if (p === "claude") return askClaude(getKey(p), r, mode, question);
  if (p === "mistral") return askMistral(getKey(p), r, mode, question);
  return askLocal(size, r, mode, question, onProgress);
}
