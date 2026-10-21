"""OpenClip web API.

    uvicorn server.main:app --reload        # from the repo root, then run the web app in web/

Flow: POST /api/uploads gets a signed upload URL and a project id -> the browser PUTs the file
there -> POST /api/projects/{id}/start queues the pipeline -> the browser polls
GET /api/projects/{id} until it's ready. Each clip is a recipe that can be edited
(PUT /api/clips/{id}/recipe) and re-rendered (POST /api/clips/{id}/render).
"""

from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func

from openclip.recipe import Recipe, render_hash

from . import config
from .auth import current_user
from .db import Clip, Fetcher, Project, User, _now, init_db, select, session
from .storage import check_local_sig, safe_key, storage
from .worker import delete_project, runner

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    runner.start()
    yield


app = FastAPI(title="OpenClip API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.WEB_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"], max_age=3600)


# ---------------------------------------------------------------- helpers

def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _used_today(user_id: str) -> int:
    with session() as s:
        return s.exec(select(func.count()).select_from(Project).where(
            Project.owner_id == user_id, Project.created_at >= _today_start(),
            Project.status.notin_(["uploading", "failed"]))).one()  # failures don't use up the day


def _own_project(project_id: str, user: User) -> Project:
    with session() as s:
        p = s.get(Project, project_id)
    if p is None or p.owner_id != user.id:
        raise HTTPException(404, "Project not found.")
    return p


def _own_clip(clip_id: str, user: User) -> tuple[Clip, Project]:
    with session() as s:
        c = s.get(Clip, clip_id)
    if c is None:
        raise HTTPException(404, "Clip not found.")
    return c, _own_project(c.project_id, user)


def _filename(title: str, idx: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()[:60] or "clip"
    return f"{idx:02d}-{slug}.mp4"


def _clip_out(c: Clip, full: bool = False) -> dict:
    r = c.recipe
    out = {
        "id": c.id, "idx": c.idx, "title": r["title"], "score": r.get("score"), "reason": r.get("reason", ""),
        "start": r["start"], "end": r["end"], "layout": r["layout"],
        "render_status": c.render_status, "render_error": c.render_error,
        "video_url": storage.get_url(c.render_key) if c.render_key else None,
        "download_url": storage.get_url(c.render_key, _filename(r["title"], c.idx)) if c.render_key else None,
        "stale": c.render_hash != render_hash(Recipe.model_validate(r)),
    }
    if full:
        out["recipe"] = r
    return out


def _project_out(p: Project, clips: list[Clip] | None = None) -> dict:
    out = {"id": p.id, "title": p.title, "status": p.status, "progress": p.progress, "error": p.error,
           "duration": p.duration, "model": p.model, "options": p.options,
           "source": "youtube" if p.source_url else "upload",
           "expires_at": (p.created_at + timedelta(days=config.KEEP_DAYS)).isoformat(),
           "created_at": p.created_at.isoformat(),
           "finished_at": p.finished_at.isoformat() if p.finished_at else None}
    if clips is not None:
        out["clips"] = [_clip_out(c) for c in sorted(clips, key=lambda c: c.idx)]
        out["log"] = p.log[-40:]
        out["source_url"] = storage.get_url(p.source_key) if p.status not in ("uploading", "fetching") else None
    return out


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health():
    return {"ok": True, "storage": config.STORAGE, "auth": "clerk" if config.CLERK_ISSUER else "dev"}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "dev": user.id == "dev",
            "limits": {"projects_per_day": config.PROJECTS_PER_DAY, "max_upload_mb": config.MAX_UPLOAD_MB,
                       "max_video_min": config.MAX_VIDEO_S // 60, "max_clips": config.MAX_CLIPS,
                       "keep_days": config.KEEP_DAYS},
            "used_today": _used_today(user.id), "links": config.LINKS_ENABLED and _fetcher_online()}


class UploadIn(BaseModel):
    filename: str = Field(max_length=300)
    content_type: str = "video/mp4"
    size: int = Field(gt=0)


@app.post("/api/uploads")
def create_upload(body: UploadIn, user: User = Depends(current_user)):
    ext = os.path.splitext(body.filename)[1].lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f"Upload a video file ({', '.join(sorted(VIDEO_EXTS))}).")
    if body.size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"Files are capped at {config.MAX_UPLOAD_MB} MB on the free plan.")
    if _used_today(user.id) >= config.PROJECTS_PER_DAY:
        raise HTTPException(429, f"You've used today's {config.PROJECTS_PER_DAY} projects. "
                                 "The count resets at midnight UTC.")
    title = os.path.splitext(os.path.basename(body.filename))[0][:120] or "Untitled"
    with session() as s:
        p = Project(owner_id=user.id, title=title, source_key="pending")
        p.source_key = f"projects/{p.id}/source{ext}"
        s.add(p)
        s.commit()
    return {"project_id": p.id, "upload_url": storage.put_url(p.source_key, body.content_type),
            "content_type": body.content_type}


class StartIn(BaseModel):
    layout: str = Field("auto", pattern="^(auto|single|split)$")
    max_clips: int = Field(3, ge=1, le=config.MAX_CLIPS)
    min_duration: float = Field(30, ge=10, le=120)
    max_duration: float = Field(60, ge=15, le=180)


@app.post("/api/projects/{project_id}/start")
def start_project(project_id: str, body: StartIn, user: User = Depends(current_user)):
    p = _own_project(project_id, user)
    if p.status != "uploading":
        raise HTTPException(409, "This project has already started.")
    size = storage.size(p.source_key)
    if not size:
        raise HTTPException(400, "The upload didn't finish. Try again.")
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        delete_project(p.id)
        raise HTTPException(400, f"Files are capped at {config.MAX_UPLOAD_MB} MB on the free plan.")
    if body.max_duration <= body.min_duration:
        raise HTTPException(400, "The longest clip length must be more than the shortest.")
    if _used_today(user.id) >= config.PROJECTS_PER_DAY:
        raise HTTPException(429, f"You've used today's {config.PROJECTS_PER_DAY} projects.")
    with session() as s:
        p = s.get(Project, project_id)
        p.status, p.options = "queued", body.model_dump()
        s.add(p)
        s.commit()
    runner.submit("project", p.id)
    return _project_out(p, [])


# ---------------------------------------------------------------- YouTube links (fetched on a home PC)

YOUTUBE_RE = re.compile(r"^https?://(www\.|m\.|music\.)?(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)[\w\-?=&%./]+$")


def _fetcher_online() -> bool:
    if not config.FETCHER_TOKEN:
        return False
    with session() as s:
        last = s.exec(select(func.max(Fetcher.last_seen))).one()
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() < config.FETCHER_ONLINE_S


class LinkIn(StartIn):
    url: str = Field(max_length=500)


@app.post("/api/projects/from-link")
def create_from_link(body: LinkIn, user: User = Depends(current_user)):
    if not config.LINKS_ENABLED:
        raise HTTPException(410, "Links are switched off. Upload the video file instead.")
    url = body.url.strip()
    if not YOUTUBE_RE.match(url):
        raise HTTPException(400, "Paste a YouTube video link (youtube.com/watch?v=… or youtu.be/…).")
    if not _fetcher_online():
        raise HTTPException(503, "The link downloader is offline right now. Upload the video file instead.")
    if body.max_duration <= body.min_duration:
        raise HTTPException(400, "The longest clip length must be more than the shortest.")
    if _used_today(user.id) >= config.PROJECTS_PER_DAY:
        raise HTTPException(429, f"You've used today's {config.PROJECTS_PER_DAY} projects. "
                                 "The count resets at midnight UTC.")
    with session() as s:
        p = Project(owner_id=user.id, title="YouTube video", status="fetching", source_key="pending",
                    source_url=url, options=body.model_dump(exclude={"url"}))
        p.source_key = f"projects/{p.id}/source.mp4"
        s.add(p)
        s.commit()
    return _project_out(p, [])


def _fetcher(x_fetcher_token: str | None = Header(None)) -> None:
    if not config.FETCHER_TOKEN or not x_fetcher_token or not hmac.compare_digest(x_fetcher_token, config.FETCHER_TOKEN):
        raise HTTPException(403)


class ClaimIn(BaseModel):
    fetcher_id: str = Field("home-pc", max_length=60)


@app.post("/api/fetcher/claim", dependencies=[Depends(_fetcher)])
def fetcher_claim(body: ClaimIn):
    """The downloader checks in and takes the oldest waiting link, if any (204 = nothing to do)."""
    now = _now()
    with session() as s:
        f = s.get(Fetcher, body.fetcher_id) or Fetcher(id=body.fetcher_id)
        f.last_seen = now
        s.add(f)
        # links claimed but never finished (machine slept, container restarted) are given up on
        stale = now - timedelta(seconds=config.FETCH_TIMEOUT_S)
        for p in s.exec(select(Project).where(Project.status == "fetching", Project.fetch_claimed_at != None,  # noqa: E711
                                              Project.fetch_claimed_at < stale)):
            p.status, p.error, p.finished_at = "failed", "Downloading from YouTube took too long. Try again.", now
            s.add(p)
        q = (select(Project).where(Project.status == "fetching", Project.fetch_claimed_at == None)  # noqa: E711
             .order_by(Project.created_at).limit(1))
        if not config.DATABASE_URL.startswith("sqlite"):
            q = q.with_for_update(skip_locked=True)
        p = s.exec(q).first()
        if p is not None:
            p.fetch_claimed_at = now
            p.log = list(p.log or []) + [f"{now:%H:%M:%S}  Downloading from YouTube…"]
            s.add(p)
        s.commit()
    if p is None:
        return Response(status_code=204)
    return {"project_id": p.id, "url": p.source_url, "upload_url": storage.put_url(p.source_key, "video/mp4"),
            "content_type": "video/mp4", "max_video_s": config.MAX_VIDEO_S, "max_upload_mb": config.MAX_UPLOAD_MB}


class FetchLogIn(BaseModel):
    line: str = Field(max_length=500)


@app.post("/api/fetcher/{project_id}/log", dependencies=[Depends(_fetcher)])
def fetcher_log(project_id: str, body: FetchLogIn):
    with session() as s:
        p = s.get(Project, project_id)
        if p is None or p.status != "fetching":
            raise HTTPException(409, "This link is no longer waiting for a download.")
        p.log = (list(p.log or []) + [f"{_now():%H:%M:%S}  {body.line}"])[-200:]
        s.add(p)
        s.commit()
    return {"ok": True}


class FetchDoneIn(BaseModel):
    ok: bool
    error: str | None = Field(None, max_length=1000)
    title: str | None = Field(None, max_length=200)


@app.post("/api/fetcher/{project_id}/done", dependencies=[Depends(_fetcher)])
def fetcher_done(project_id: str, body: FetchDoneIn):
    with session() as s:
        p = s.get(Project, project_id)
        if p is None or p.status != "fetching":
            raise HTTPException(409, "This link is no longer waiting for a download.")
        if body.title:
            p.title = body.title.strip()[:120] or p.title
        if body.ok and storage.size(p.source_key):
            p.status = "queued"
        else:
            p.status, p.finished_at = "failed", _now()
            p.error = body.error or "The download from YouTube didn't arrive. Try again."
        s.add(p)
        s.commit()
    if p.status == "queued":
        runner.submit("project", p.id)
    return {"ok": True, "status": p.status}


@app.get("/api/projects")
def list_projects(user: User = Depends(current_user)):
    with session() as s:
        projects = s.exec(select(Project).where(Project.owner_id == user.id, Project.status != "uploading")
                          .order_by(Project.created_at.desc())).all()
        counts = dict(s.exec(select(Clip.project_id, func.count()).group_by(Clip.project_id)).all())
    return [{**_project_out(p), "clip_count": counts.get(p.id, 0)} for p in projects]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(current_user)):
    p = _own_project(project_id, user)
    with session() as s:
        clips = s.exec(select(Clip).where(Clip.project_id == p.id)).all()
    out = _project_out(p, list(clips))
    if p.status == "queued":
        out["queue_position"] = runner.queue_position()
    return out


@app.delete("/api/projects/{project_id}")
def remove_project(project_id: str, user: User = Depends(current_user)):
    p = _own_project(project_id, user)
    if p.status == "processing":
        raise HTTPException(409, "Wait for processing to finish before deleting.")
    delete_project(p.id)
    return {"ok": True}


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: str, user: User = Depends(current_user)):
    c, p = _own_clip(clip_id, user)
    # the whole video's words, so the editor can extend a clip past the range the model picked
    transcript_key = f"{os.path.dirname(p.source_key)}/transcript.json"
    return {**_clip_out(c, full=True), "project_id": p.id, "project_title": p.title,
            "source_url": storage.get_url(p.source_key),
            "transcript_url": storage.get_url(transcript_key) if storage.size(transcript_key) else None}


@app.put("/api/clips/{clip_id}/recipe")
def save_recipe(clip_id: str, body: dict, user: User = Depends(current_user)):
    c, p = _own_clip(clip_id, user)
    try:
        recipe = Recipe.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False))
    if recipe.source.model_dump() != Recipe.model_validate(c.recipe).source.model_dump():
        raise HTTPException(400, "A clip can't change its source video.")
    with session() as s:
        c = s.get(Clip, clip_id)
        c.recipe = recipe.model_dump()
        s.add(c)
        s.commit()
    return _clip_out(c, full=True)


@app.post("/api/clips/{clip_id}/render")
def rerender(clip_id: str, user: User = Depends(current_user)):
    c, _ = _own_clip(clip_id, user)
    if c.render_status in ("queued", "rendering"):
        return _clip_out(c)
    with session() as s:
        c = s.get(Clip, clip_id)
        c.render_status = "queued"
        s.add(c)
        s.commit()
    runner.submit("render", c.id)
    return _clip_out(c)


# ---------------------------------------------------------------- local file URLs (R2 does this in production)

if config.STORAGE == "local":
    @app.put("/files/{key:path}")
    async def local_put(key: str, request: Request, exp: int, sig: str):
        if not check_local_sig("PUT", key, exp, sig):
            raise HTTPException(403, "Upload link expired.")
        dest = storage.path(safe_key(key))
        dest.parent.mkdir(parents=True, exist_ok=True)
        limit, written = config.MAX_UPLOAD_MB * 1024 * 1024, 0
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"Files are capped at {config.MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        return {"ok": True, "size": written}

    @app.get("/files/{key:path}")
    def local_get(key: str, exp: int, sig: str, dl: str | None = None):
        if not check_local_sig("GET", key, exp, sig):
            raise HTTPException(403, "Link expired.")
        path = storage.path(safe_key(key))
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, filename=dl) if dl else FileResponse(path)
