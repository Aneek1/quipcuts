import { useTitle } from "../api";

// Plain-language policies for the free beta. Keep them in step with the API's real behaviour
// (server/config.py: KEEP_DAYS, limits) and the processors actually in use.
const UPDATED = "21 October 2026";
const CONTACT = (import.meta.env.VITE_CONTACT_EMAIL as string) || "the contact address on the site";

export function Privacy() {
  useTitle("Privacy");
  return (
    <main className="wrap page prose">
      <h1>Privacy policy</h1>
      <span className="mono faint">Last updated {UPDATED}</span>

      <h2>What we collect</h2>
      <ul>
        <li><b>Your account:</b> your email address and sign-in details, handled by our sign-in provider, Clerk.</li>
        <li><b>Videos you upload</b>, plus the transcripts and clips made from them.</li>
        <li><b>If you connect a social account</b> to post clips: the access token that platform gives us, used only to post what you ask us to post.</li>
        <li><b>Basic logs:</b> request times, errors and IP addresses, kept for security and debugging.</li>
      </ul>

      <h2>How long we keep it</h2>
      <p>Projects (the uploaded video, its transcript and its clips) are deleted automatically 7 days after upload, or as soon as you delete them. Your account stays until you delete it.</p>

      <h2>What we do with it</h2>
      <p>We use your videos only to make your clips. We don't sell your data, show it to other users, or use your videos to train AI models. Transcription and clip selection run on our own servers with open-weights models; your video isn't sent to an outside AI company.</p>

      <h2>Who processes it for us</h2>
      <ul>
        <li><b>Cloudflare</b>: website hosting, file storage and bot protection.</li>
        <li><b>Modal</b>: the GPU servers that process videos.</li>
        <li><b>Clerk</b>: accounts and sign-in.</li>
        <li><b>Neon</b>: the database holding project details.</li>
      </ul>

      <h2>Your choices</h2>
      <p>You can delete any project at any time from its page. To delete your account or ask what we hold about you, contact {CONTACT}.</p>
    </main>
  );
}

export function Terms() {
  useTitle("Terms");
  return (
    <main className="wrap page prose">
      <h1>Terms of service</h1>
      <span className="mono faint">Last updated {UPDATED}</span>

      <h2>The service</h2>
      <p>Quipcuts turns videos you upload into short vertical clips. It's free during the beta, with daily limits, and we may change or end it at any time. It's provided as is, without warranties, and clip quality and availability aren't guaranteed.</p>

      <h2>Your content</h2>
      <ul>
        <li>Only upload videos you own or have permission to use. You're responsible for what you upload and for where you post the clips.</li>
        <li>You keep all rights to your videos and clips. You give us permission to store and process them only to provide the service.</li>
        <li>Don't upload anything illegal, anything that infringes someone else's rights, or sexual content involving minors. We'll remove such content and may close the account.</li>
      </ul>

      <h2>Fair use of the free beta</h2>
      <p>Don't try to get around the limits, overload the service or access other users' projects. We may suspend accounts that do.</p>

      <h2>Posting to other platforms</h2>
      <p>When you connect a YouTube, TikTok, Instagram or other account, posts made through Quipcuts are subject to that platform's own terms, and you can disconnect at any time.</p>

      <h2>Contact</h2>
      <p>Questions about these terms: {CONTACT}.</p>
    </main>
  );
}
