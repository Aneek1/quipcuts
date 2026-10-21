"""Front door for the hosted demo: Cloudflare Turnstile, then a reverse proxy to Streamlit.

Streamlit has no request hooks, so it runs on 127.0.0.1:8501 inside the same container and
this FastAPI app sits in front. A visitor without a valid pass cookie gets the Turnstile
page; solving it sets a signed cookie (12 h). Every HTTP path and the Streamlit websocket
need the cookie. Job caps are enforced inside the app (openclip.hosted), which reads the
same cookie. Same pattern as the Quipu chat demo's gate.
"""

from __future__ import annotations

import asyncio
import html
import secrets
import time

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from openclip.hosted import COOKIE, check_pass, pass_key, sign

UPSTREAM = "127.0.0.1:8501"
PASS_SECONDS = 12 * 3600
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer"}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenClip</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#09090D;
color:#EDEDF2;font:15px/1.5 system-ui,sans-serif}}main{{text-align:center;padding:16px}}
p{{color:#8E8E9C}}.err{{color:#F472B6}}</style></head><body><main>
<h1 style="font-weight:700;font-size:20px">OpenClip</h1>
<p>A quick check that you're a person, then the app opens.</p>{error}
<form id="f" method="post" action="/verify">
<div class="cf-turnstile" data-sitekey="{site_key}" data-callback="done" data-theme="dark"></div></form>
<script>function done(){{document.getElementById("f").submit()}}</script>
</main></body></html>"""


def build_app(site_key: str, secret_key: str) -> FastAPI:
    key = pass_key(secret_key)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    client = httpx.AsyncClient(base_url=f"http://{UPSTREAM}", timeout=httpx.Timeout(None, connect=10))

    def page(error: str = "") -> HTMLResponse:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return HTMLResponse(PAGE.format(site_key=html.escape(site_key), error=err))

    async def siteverify(token: str, ip: str | None) -> bool:
        data = {"secret": secret_key, "response": token}
        if ip:
            data["remoteip"] = ip
        async with httpx.AsyncClient(timeout=10) as c:
            out = (await c.post(SITEVERIFY, data=data)).json()
        if not out.get("success"):
            print("turnstile check failed:", out.get("error-codes"), out.get("hostname"), flush=True)
        return bool(out.get("success"))

    @app.get("/verify")
    async def verify_page():
        return page()

    @app.post("/verify")
    async def verify(request: Request):
        form = await request.form()
        token = str(form.get("cf-turnstile-response") or "")
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
        if not token or not await siteverify(token, ip):
            return page("The check didn't go through. Please try again.")
        sid, exp = secrets.token_hex(8), int(time.time()) + PASS_SECONDS
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, f"{sid}.{exp}.{sign(key, sid, exp)}", max_age=PASS_SECONDS,
                        httponly=True, secure=True, samesite="lax")
        return resp

    @app.websocket("/_stcore/stream")
    async def stream(ws: WebSocket):
        if check_pass(key, ws.cookies.get(COOKIE)) is None:
            await ws.close(code=1008)
            return
        protocols = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
        # No Origin: Streamlit compares it with its own host (127.0.0.1) and would reject the
        # upgrade with a 403. The gate above has already checked the visitor.
        headers = {k: v for k, v in ws.headers.items() if k in ("cookie", "user-agent")}
        async with websockets.connect(f"ws://{UPSTREAM}/_stcore/stream", subprotocols=protocols or None,
                                      additional_headers=headers, max_size=None, ping_interval=None) as up:
            await ws.accept(subprotocol=up.subprotocol)

            async def down():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            async def upward():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    await up.send(msg["bytes"] if msg.get("bytes") is not None else msg["text"])

            tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except WebSocketDisconnect:
                pass
            finally:
                for t in tasks:
                    t.cancel()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"])
    async def proxy(path: str, request: Request):
        if check_pass(key, request.cookies.get(COOKIE)) is None:
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/verify", status_code=303)
            return PlainTextResponse("Open the app in a browser to pass the check first.", 403)
        headers = [(k, v) for k, v in request.headers.raw if k.decode().lower() not in HOP | {"host"}]
        upstream = client.build_request(request.method, httpx.URL(path="/" + path, query=request.url.query.encode()),
                                        headers=headers, content=request.stream())
        resp = await client.send(upstream, stream=True)
        out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=_Close(resp))
        # raw list, not a dict: keeps repeated headers such as Set-Cookie
        out.raw_headers = [(k.encode("latin-1"), v.encode("latin-1"))
                           for k, v in resp.headers.multi_items() if k.lower() not in HOP]
        return out

    return app


class _Close:
    """Starlette background task that closes the upstream response once streamed."""
    def __init__(self, resp: httpx.Response):
        self.resp = resp

    async def __call__(self):
        await self.resp.aclose()
