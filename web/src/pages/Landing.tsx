import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

// The hero caption mimics the rendered clips: three words on screen, the spoken one in yellow.
const LINES = [
  ["NOBODY", "TELLS", "YOU"],
  ["THE", "FIRST", "YEAR"],
  ["IS", "MOSTLY", "WAITING."],
];
const WORD_MS = 420;

function Caption() {
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => setStep((s) => (s + 1) % (LINES.length * 3)), WORD_MS);
    return () => window.clearInterval(id);
  }, []);
  const line = LINES[Math.floor(step / 3)];
  return (
    <div className="caption" aria-hidden="true">
      {line.map((w, i) => (
        <span key={w + i} className={i === step % 3 ? "on" : ""}>{w}</span>
      ))}
    </div>
  );
}

const STEPS = [
  ["Bring a video", "Upload a talk, podcast or interview up to 15 minutes, or paste a public YouTube link."],
  ["Transcribe", "Whisper writes down every word with its start and end time."],
  ["Pick", "An open-weights model reads the transcript and picks moments that make sense without the rest of the video."],
  ["Cut", "Each moment is reframed to 9:16 around the speaker and captioned word by word."],
  ["Edit", "Trim to the word, add a hook and a CTA, restyle the captions, then export the MP4."],
];

const EDITOR = [
  ["Trim to the word", "Drag the ends of a clip. They snap to word boundaries, and you can go past what the model picked."],
  ["Hook and CTA", "Text over the first seconds to stop the scroll and over the last ones to say what to do next: follow, subscribe, watch the full video."],
  ["Captions your way", "Size, position, the colour of the spoken word, all caps or not. Fix a misheard word or hide one."],
  ["Punch-in zoom", "A quick zoom on the loudest moments of the clip, a few seconds apart, so it moves without looking busy."],
  ["Ask AI", "Hook, CTA and title ideas from the clip's transcript, applied with one click. It runs in your browser, not on our servers."],
];

export default function Landing() {
  return (
    <main>
      <section className="wrap hero">
        <div>
          <span className="eyebrow">Long video → vertical clips</span>
          <h1>
            Find the <mark>forty seconds</mark> worth posting.
          </h1>
          <p className="lead">
            Upload a talk, podcast or interview, or paste a YouTube link. Quipcuts transcribes it, picks the moments
            that stand on their own, reframes them to vertical and burns in captions you can read with the sound off.
            Then trim, add a hook and a call to action, and export.
          </p>
          <div className="cta">
            <Link to="/app" className="btn primary lg">Make clips</Link>
            <span className="note">Free in beta · 3 videos a day</span>
          </div>
        </div>
        <div className="phone" role="img" aria-label="A vertical clip with a word-by-word caption, the spoken word highlighted in yellow">
          <div className="scene" />
          <div className="figure" />
          <div className="tc">
            <span className="rec">00:41</span>
            <span>9:16</span>
          </div>
          <Caption />
          <div className="meta">
            <span>clip 1 of 3</span>
            <span>12:08 → 12:49</span>
          </div>
        </div>
      </section>

      <section className="wrap ruler">
        <div className="ruler-track">
          {STEPS.map(([title, text], i) => (
            <div className="ruler-step" key={title}>
              <span className="mono">{i + 1} / {STEPS.length}</span>
              <h3>{title}</h3>
              <p>{text}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="wrap section split">
        <div>
          <span className="eyebrow">How a moment gets picked</span>
          <h2>It reads the whole thing first.</h2>
          <p className="muted" style={{ marginTop: 16 }}>
            A good clip opens on a line that grabs you, makes sense on its own and ends once the point lands. The
            model gets the full transcript with timestamps, then the cut points are snapped to word boundaries so a
            clip never starts mid-word.
          </p>
          <p className="muted">
            Each clip gets a score out of 100. It's the model's own judgement, not a prediction trained on real views,
            so treat it as a ranking, not a promise.
          </p>
        </div>
        <div className="transcript">
          <span className="t">11:52</span>…so that's roughly how the studio got started, and honestly the details matter less than you'd think.{" "}
          <span className="cut" />
          <span className="t">12:08</span>
          <span className="pick">
            Nobody tells you the first year is mostly waiting. You ship something, and then nothing happens for weeks,
            and you start to think the idea was wrong. It usually wasn't. The waiting is the job.
          </span>
          <span className="cut" /> <span className="t">12:51</span>Anyway, the second thing we changed was pricing…
          <div className="why">
            <span className="eyebrow" style={{ marginBottom: 4 }}>Model's note · score 82 · example</span>
            Opens on a claim most founders recognise, pays off in one line, and doesn't depend on anything said
            earlier.
          </div>
        </div>
      </section>

      <section className="wrap section split" style={{ paddingTop: 0 }}>
        <div>
          <span className="eyebrow">Then make it yours</span>
          <h2>Every clip opens in an editor.</h2>
          <p className="muted" style={{ marginTop: 16 }}>
            The preview plays the vertical cut straight from your video, so changes show up as you make them. Export
            renders the final MP4 on a GPU, usually in under a minute.
          </p>
        </div>
        <ul className="features">
          {EDITOR.map(([title, text]) => (
            <li key={title}><b>{title}</b><span>{text}</span></li>
          ))}
        </ul>
      </section>

      <section className="wrap section" style={{ paddingTop: 0 }}>
        <span className="eyebrow">The free beta, plainly</span>
        <h2 style={{ marginBottom: 20 }}>What you get and what you don't.</h2>
        <table className="spec">
          <tbody>
            <tr><th>Per day</th><td><b>3 videos</b>, each up to 15 minutes and 500 MB, with up to 5 clips per video.</td></tr>
            <tr><th>Speed</th><td>About two minutes for a 4-minute video. If nobody has used it for a while, add about a minute and a half for the GPU to wake up.</td></tr>
            <tr><th>Layouts</th><td>One speaker, or two people stacked top and bottom. The two-person layout is new and can frame the wrong spot when a face isn't detected.</td></tr>
            <tr><th>Retention</th><td>Everything is <b>deleted after 7 days</b>, including your upload. Download the clips you want to keep.</td></tr>
            <tr><th>Sources</th><td>Uploads, and public YouTube links. Links are fetched by a downloader that isn't always online; when the link option is greyed out, upload the file instead.</td></tr>
            <tr><th>AI help</th><td>Ask AI in the editor uses <b>your own Claude API key</b>, kept in your browser, or a small model that runs on your computer (Chrome or Edge, about 1 GB downloaded once). Neither goes through Quipcuts.</td></tr>
            <tr><th>Not yet</th><td>Posting straight to TikTok, Instagram or YouTube, saved brand presets, and a screen-share layout. These are next.</td></tr>
            <tr><th>Source code</th><td>Open source on <a href="https://github.com/Aneek1/quipcuts">GitHub</a>.</td></tr>
          </tbody>
        </table>
      </section>
    </main>
  );
}
