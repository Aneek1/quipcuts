// The homepage example: a real clip cut and edited with Quipcuts.
// Source: "Interview with Jimmy Wales at Wikimania 2025 Nairobi" by DelwarHossain, CC BY 4.0,
// https://commons.wikimedia.org/wiki/File:Interview_with_Jimmy_Wales_at_Wikimania_2025_Nairobi.webm
// Changes made: cut to one clip, reframed to 9:16, captions, hook and CTA text, punch-in zooms.
export const DEMO = {
  video: "/demo/clip.mp4",
  poster: "/demo/poster.jpg",
  score: 85,
  picker: "qwen3:8b",
  // verbatim from the clip's recipe (the model's reason field)
  note: "Hook: 'We'll fully realize our dream of a free encyclopedia for every single person on the planet in their own language' is a bold, aspirational claim. Payoff: The vision of global access. Self-contained and ends on a powerful note.",
  alt: "A vertical clip of an interview at Wikimania 2025, with word-by-word captions, a hook at the top and a call to action at the end.",
  what: "A 4-minute interview in, three clips out in about two minutes. This one scored 85, the model's own estimate rather than measured views. Crop, hook, CTA and zooms were added in the editor.",
  credit: "Footage: “Interview with Jimmy Wales at Wikimania 2025 Nairobi” by DelwarHossain, CC BY 4.0, via Wikimedia Commons. Cut, reframed, captioned and overlaid by Quipcuts. Not affiliated with Wikipedia or the Wikimedia Foundation.",
};
