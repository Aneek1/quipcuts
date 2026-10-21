// Static assets don't answer HTTP Range requests, and Safari won't play a <video> without them.
// This runs only for /demo/* (see run_worker_first in wrangler.jsonc) and slices the file itself.
interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const res = await env.ASSETS.fetch(request);
    const range = request.headers.get("Range");
    const m = range?.match(/^bytes=(\d*)-(\d*)$/);
    if (!res.ok || !m) {
      const headers = new Headers(res.headers);
      headers.set("Accept-Ranges", "bytes");
      return new Response(res.body, { status: res.status, headers });
    }
    const body = await res.arrayBuffer();
    const size = body.byteLength;
    let start = m[1] === "" ? size - Number(m[2]) : Number(m[1]);
    let end = m[1] === "" || m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
    start = Math.max(0, start);
    if (start > end || start >= size) {
      return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
    }
    const headers = new Headers(res.headers);
    headers.set("Accept-Ranges", "bytes");
    headers.set("Content-Range", `bytes ${start}-${end}/${size}`);
    headers.set("Content-Length", String(end - start + 1));
    return new Response(body.slice(start, end + 1), { status: 206, headers });
  },
};
