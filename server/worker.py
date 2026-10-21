"""The two kinds of job: process a new project (the full pipeline) and re-render one clip
from its recipe. Both run one at a time on a background thread, because the GPU/CPU-heavy
steps don't share well. On Modal the same functions run inside the GPU container instead.
"""

from __future__ import annotations

import os
import queue
import shutil
import tempfile
import threading
import time
import traceback
from datetime import timedelta

from openclip.recipe import Recipe, render_hash, render_recipe

from . import config
from .db import Clip, Project, _now, select, session
from .storage import storage

LOG_LINES = 200


def _update(project_id: str, **fields) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        if p is None:
            return
        for k, v in fields.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def _log(project_id: str, line: str) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        if p is None:
            return
        p.log = (list(p.log or []) + [f"{_now():%H:%M:%S}  {line}"])[-LOG_LINES:]
        s.add(p)
        s.commit()


def _workdir(project: Project) -> tuple[str, bool]:
    """Folder holding the project's source video. Local storage: the project folder itself."""
    local = storage.path(project.source_key)
    if local is not None:
        return str(local.parent), False
    d = tempfile.mkdtemp(prefix=f"openclip_{project.id}_")
    storage.download(project.source_key, os.path.join(d, os.path.basename(project.source_key)))
    return d, True


def process_project(project_id: str) -> None:
    # heavy imports (Whisper, OpenCV, LiteLLM) only where jobs run, not in the API container
    from openclip.downloader import probe_duration
    from openclip.pipeline import Settings, run_pipeline_on_file

    with session() as s:
        project = s.get(Project, project_id)
    if project is None:
        return
    _update(project_id, status="processing", progress=0.02, error=None)
    job_dir, temporary = _workdir(project)
    prefix = os.path.dirname(project.source_key)
    try:
        source_path = os.path.join(job_dir, os.path.basename(project.source_key))
        duration = probe_duration(source_path)
        if duration <= 0:
            raise ValueError("This file has no readable video. Try exporting it again as MP4.")
        if duration > config.MAX_VIDEO_S + 1:
            raise ValueError(f"The video is {duration / 60:.0f} minutes long; the free plan takes up to "
                             f"{config.MAX_VIDEO_S // 60} minutes.")
        _update(project_id, duration=duration)

        opts = project.options or {}
        settings = Settings(provider=config.LLM_PROVIDER, ollama_model=config.OLLAMA_MODEL,
                            whisper_model=config.WHISPER_MODEL, workspace=job_dir,
                            max_clips=min(int(opts.get("max_clips", 3)), config.MAX_CLIPS),
                            min_duration=float(opts.get("min_duration", 30)),
                            max_duration=float(opts.get("max_duration", 60)),
                            layout=opts.get("layout", "auto"))
        summary = run_pipeline_on_file(source_path, settings, title=project.title, job_dir=job_dir,
                                       log=lambda m: _log(project_id, m),
                                       progress=lambda p: _update(project_id, progress=round(0.02 + 0.96 * p, 3)))

        with session() as s:
            for n, c in enumerate(summary["clips"], 1):
                recipe = Recipe.model_validate_json(open(c["recipe_path"], encoding="utf-8").read())
                recipe.source.key = project.source_key
                render_key = f"{prefix}/clips/{os.path.basename(c['output_path'])}"
                storage.upload(c["output_path"], render_key, "video/mp4")
                s.add(Clip(project_id=project_id, idx=n, recipe=recipe.model_dump(), render_key=render_key,
                           render_hash=render_hash(recipe), render_status="ready"))
            s.commit()
        transcript = os.path.join(job_dir, f"transcript_{config.WHISPER_MODEL}.json")
        if os.path.exists(transcript):
            storage.upload(transcript, f"{prefix}/transcript.json", "application/json")
        _update(project_id, status="ready", progress=1.0, model=summary.get("model"), finished_at=_now())
    except Exception as exc:
        traceback.print_exc()
        _log(project_id, f"Failed: {exc}")
        _update(project_id, status="failed", error=str(exc) or exc.__class__.__name__, finished_at=_now())
    finally:
        try:
            os.remove(os.path.join(job_dir, "audio.wav"))
        except OSError:
            pass
        if temporary:
            shutil.rmtree(job_dir, ignore_errors=True)


def render_clip(clip_id: str) -> None:
    with session() as s:
        clip = s.get(Clip, clip_id)
        project = s.get(Project, clip.project_id) if clip else None
    if clip is None or project is None:
        return
    recipe = Recipe.model_validate(clip.recipe)
    want = render_hash(recipe)

    def set_clip(**fields):
        with session() as s:
            c = s.get(Clip, clip_id)
            for k, v in fields.items():
                setattr(c, k, v)
            c.updated_at = _now()
            s.add(c)
            s.commit()

    set_clip(render_status="rendering", render_error=None)
    job_dir, temporary = _workdir(project)
    out_dir = tempfile.mkdtemp(prefix="render_")
    try:
        out = render_recipe(recipe, os.path.join(job_dir, os.path.basename(project.source_key)),
                            os.path.join(out_dir, "clip.mp4"))
        key = f"{os.path.dirname(project.source_key)}/renders/{clip_id}_{want}.mp4"
        storage.upload(out, key, "video/mp4")
        old = clip.render_key
        set_clip(render_key=key, render_hash=want, render_status="ready")
        if old and old != key and "/renders/" in old:
            storage.delete(old)
    except Exception as exc:
        traceback.print_exc()
        set_clip(render_status="failed", render_error=str(exc)[:2000])
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        if temporary:
            shutil.rmtree(job_dir, ignore_errors=True)


def delete_project(project_id: str) -> None:
    with session() as s:
        project = s.get(Project, project_id)
        if project is None:
            return
        for c in s.exec(select(Clip).where(Clip.project_id == project_id)):
            s.delete(c)
        s.delete(project)
        s.commit()
    storage.delete_prefix(os.path.dirname(project.source_key))


def sweep_expired() -> None:
    cutoff = _now() - timedelta(days=config.KEEP_DAYS)
    with session() as s:
        old = [p.id for p in s.exec(select(Project).where(Project.created_at < cutoff))]
    for pid in old:
        delete_project(pid)


class LocalRunner:
    """Single background thread working through a FIFO of ("project"|"render", id)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread:
            return
        # jobs interrupted by a restart go back on the queue
        with session() as s:
            for p in s.exec(select(Project).where(Project.status.in_(["queued", "processing"]))):
                self.q.put(("project", p.id))
            for c in s.exec(select(Clip).where(Clip.render_status.in_(["queued", "rendering"]))):
                self.q.put(("render", c.id))
        self.thread = threading.Thread(target=self._loop, daemon=True, name="openclip-worker")
        self.thread.start()

    def submit(self, kind: str, item_id: str) -> None:
        self.q.put((kind, item_id))

    def queue_position(self) -> int:
        return self.q.qsize()

    def _loop(self) -> None:
        last_sweep = 0.0
        while True:
            if time.time() - last_sweep > 3600:
                last_sweep = time.time()
                try:
                    sweep_expired()
                except Exception:
                    traceback.print_exc()
            try:
                kind, item_id = self.q.get(timeout=60)
            except queue.Empty:
                continue
            try:
                run_job(kind, item_id)
            except Exception:
                traceback.print_exc()


class ModalRunner:
    """Production: each job is a call to the GPU class in deploy/modal_web.py. Modal queues the
    calls, and max_containers=1 there keeps it to one job at a time."""

    def start(self) -> None:
        pass

    def submit(self, kind: str, item_id: str) -> None:
        import modal
        modal.Cls.from_name(config.MODAL_APP, "Worker")().run.spawn(kind, item_id)

    def queue_position(self) -> int:
        with session() as s:
            return len(s.exec(select(Project.id).where(Project.status == "queued")).all())


def run_job(kind: str, item_id: str) -> None:
    (process_project if kind == "project" else render_clip)(item_id)


runner = ModalRunner() if config.RUNNER == "modal" else LocalRunner()
