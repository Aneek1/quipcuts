"""Settings for the web API, all from environment variables.

Local development needs none of them: files go to ./data, the database is SQLite and every
request is the "dev" user. Production sets STORAGE=r2, DATABASE_URL (Postgres) and CLERK_ISSUER.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("OPENCLIP_DATA_DIR", ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'openclip.db').as_posix()}")
# Neon hands out postgres(ql)://...; SQLAlchemy needs the driver named
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(_prefix):]

# where browser-facing URLs for local files point (the API itself serves them in local mode)
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
WEB_ORIGINS = [o.strip() for o in os.getenv("WEB_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

STORAGE = os.getenv("STORAGE", "local")          # local | r2
RUNNER = os.getenv("RUNNER", "local")            # local (background thread) | modal (GPU class)
MODAL_APP = os.getenv("MODAL_APP", "quipcuts")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "quipuclip")



def issuer_from_publishable_key(pk: str) -> str:
    """pk_live_<base64 "clerk.example.com$"> -> https://clerk.example.com (Clerk's Frontend API)."""
    import base64
    body = pk.split("_", 2)[-1]
    host = base64.b64decode(body + "=" * (-len(body) % 4)).decode().rstrip("$")
    return f"https://{host}"


# Clerk: the issuer is the Frontend API URL, e.g. https://clerk.quipcuts.app. It can be given
# directly or worked out from the (public) publishable key the frontend also uses.
CLERK_ISSUER = os.getenv("CLERK_ISSUER", "").rstrip("/")
if not CLERK_ISSUER and os.getenv("CLERK_PUBLISHABLE_KEY"):
    CLERK_ISSUER = issuer_from_publishable_key(os.environ["CLERK_PUBLISHABLE_KEY"])
DEV_AUTH = os.getenv("OPENCLIP_DEV_AUTH", "1" if not CLERK_ISSUER else "0") == "1"

# signs local upload/download URLs; generated per run when unset (links then die on restart)
SIGNING_KEY = os.getenv("OPENCLIP_SIGNING_KEY") or secrets.token_hex(32)

# shared secret the link downloader (fetcher/agent.py) sends; links are off when unset
FETCHER_TOKEN = os.getenv("FETCHER_TOKEN", "")
FETCHER_ONLINE_S = 90          # a fetcher seen this recently counts as online
FETCH_TIMEOUT_S = 20 * 60      # a claimed link not finished by then is given up on

# free tier
MAX_UPLOAD_MB = int(os.getenv("OPENCLIP_MAX_UPLOAD_MB", "500"))
MAX_VIDEO_S = int(os.getenv("OPENCLIP_MAX_VIDEO_S", "900"))
PROJECTS_PER_DAY = int(os.getenv("OPENCLIP_PROJECTS_PER_DAY", "3"))
KEEP_DAYS = int(os.getenv("OPENCLIP_KEEP_DAYS", "7"))
MAX_CLIPS = 5

# pipeline
LLM_PROVIDER = os.getenv("OPENCLIP_PROVIDER", "ollama")
OLLAMA_MODEL = os.getenv("OPENCLIP_OLLAMA_MODEL", "qwen3:8b")
WHISPER_MODEL = os.getenv("OPENCLIP_WHISPER_MODEL", "small")
