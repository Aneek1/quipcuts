"""Database tables: users, projects (one uploaded video each) and clips (one recipe each).

SQLite locally, Postgres (Neon) in production; same models, picked by DATABASE_URL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex[:12]


class User(SQLModel, table=True):
    id: str = Field(primary_key=True)          # Clerk user id, or "dev"
    email: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Project(SQLModel, table=True):
    id: str = Field(default_factory=_id, primary_key=True)
    owner_id: str = Field(index=True, foreign_key="user.id")
    title: str
    status: str = "uploading"     # uploading | fetching | queued | processing | ready | failed
    progress: float = 0.0
    log: list = Field(default_factory=list, sa_column=Column(JSON))
    error: str | None = None
    source_key: str
    source_url: str | None = None          # YouTube link, fetched by the downloader on a home PC
    fetch_claimed_at: datetime | None = None
    duration: float | None = None
    options: dict = Field(default_factory=dict, sa_column=Column(JSON))
    model: str | None = None
    created_at: datetime = Field(default_factory=_now, index=True)
    finished_at: datetime | None = None


class Fetcher(SQLModel, table=True):
    """The link downloader (fetcher/agent.py) checking in; one row per machine."""
    id: str = Field(primary_key=True)
    last_seen: datetime = Field(default_factory=_now)


class Clip(SQLModel, table=True):
    id: str = Field(default_factory=_id, primary_key=True)
    project_id: str = Field(index=True, foreign_key="project.id")
    idx: int
    recipe: dict = Field(sa_column=Column(JSON))
    render_key: str | None = None
    render_hash: str | None = None     # recipe hash the current render_key was made from
    render_status: str = "none"        # none | queued | rendering | ready | failed
    render_error: str | None = None
    updated_at: datetime = Field(default_factory=_now)


_sqlite = config.DATABASE_URL.startswith("sqlite")
# Neon's pooled endpoint is PgBouncer in transaction mode: psycopg's server-side prepared
# statements can land on a different backend there, so turn them off.
engine = create_engine(config.DATABASE_URL, pool_pre_ping=not _sqlite, pool_recycle=300,
                       connect_args={"check_same_thread": False} if _sqlite else {"prepare_threshold": None})


# Columns added after the first release; create_all() makes new tables but never alters old ones.
_ADDED_COLUMNS = [("project", "source_url", "VARCHAR"), ("project", "fetch_claimed_at", "TIMESTAMP WITH TIME ZONE")]


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    from sqlalchemy import inspect, text
    have = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in {t for t, _, _ in _ADDED_COLUMNS}}
    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if column not in have[table]:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {sql_type if not _sqlite else "TEXT"}'))


def session() -> Session:
    return Session(engine, expire_on_commit=False)


__all__ = ["User", "Project", "Clip", "Fetcher", "engine", "init_db", "session", "select", "_now"]
