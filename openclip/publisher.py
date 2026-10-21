"""Module G - Upload finished Shorts to YouTube via the Data API v3.

Auth: OAuth2 installed-app flow with a Google Cloud "Desktop app" client (client_secret.json).
The first call opens a browser for consent; the token (incl. refresh token) is cached in
token.json with owner-only permissions and refreshed automatically afterwards.

Scheduling: YouTube only honours `status.publishAt` on PRIVATE videos - the video stays private
and YouTube flips it to public at that time. So scheduled uploads are always sent as private.
"""

from __future__ import annotations

import os
import random
import re
import stat
import time
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CLIENT_SECRETS = os.getenv("OPENCLIP_YT_CLIENT_SECRETS", os.path.join(PROJECT_DIR, "client_secret.json"))
DEFAULT_TOKEN_PATH = os.getenv("OPENCLIP_YT_TOKEN", os.path.join(PROJECT_DIR, "token.json"))

PRIVACY_STATUSES = ("private", "unlisted", "public")
TITLE_LIMIT = 100
SHORTS_TAG = "#Shorts"
_RETRYABLE_STATUS = {500, 502, 503, 504}


class PublishError(RuntimeError):
    pass


# ---------------------------------------------------------------- auth

def _write_token(creds: Credentials, token_path: str) -> None:
    # Create the file owner-read/write only (0600) before writing the secret into it.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def get_credentials(client_secrets: str = DEFAULT_CLIENT_SECRETS, token_path: str = DEFAULT_TOKEN_PATH,
                    open_browser: bool = True) -> Credentials:
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except (ValueError, OSError):
            creds = None  # corrupt token file -> log in again

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_token(creds, token_path)
            return creds
        except RefreshError:
            creds = None  # refresh token revoked/expired -> full login

    if not os.path.exists(client_secrets):
        raise PublishError(
            f"OAuth client file not found: {client_secrets}\n"
            "Create a 'Desktop app' OAuth client in Google Cloud Console (YouTube Data API v3 enabled) "
            "and save the downloaded JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent",
                                  authorization_prompt_message="Sign in to YouTube in your browser: {url}")
    _write_token(creds, token_path)
    return creds


def get_youtube_client(client_secrets: str = DEFAULT_CLIENT_SECRETS, token_path: str = DEFAULT_TOKEN_PATH):
    return build("youtube", "v3", credentials=get_credentials(client_secrets, token_path), cache_discovery=False)


def is_authenticated(token_path: str = DEFAULT_TOKEN_PATH) -> bool:
    """True if a cached token exists that is valid or refreshable (no browser needed)."""
    if not os.path.exists(token_path):
        return False
    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except (ValueError, OSError):
        return False
    return bool(creds.valid or creds.refresh_token)


# ---------------------------------------------------------------- formatting / scheduling

def format_shorts_title(title: str, limit: int = TITLE_LIMIT) -> str:
    """Clean the title, keep it under `limit` chars at a word boundary and append #Shorts."""
    clean = re.sub(r"[<>]", "", title or "")  # YouTube rejects angle brackets in titles
    clean = re.sub(r"#shorts\b", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip() or "Untitled"
    room = limit - len(SHORTS_TAG) - 1
    if len(clean) > room:
        cut = clean[:room - 1]
        clean = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,.-:;") + "…"
    return f"{clean} {SHORTS_TAG}"


def _to_rfc3339_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> treat as local time
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_drip_schedule(count: int, first_slot: datetime, span_days: float = 7.0) -> list[datetime]:
    """Spread `count` publish times evenly over `span_days`, starting at `first_slot`.

    7 clips -> one per day at the same time; 14 clips -> every 12 hours; 3 clips -> every ~2.3 days.
    """
    if count <= 0:
        return []
    interval = timedelta(days=span_days) / count
    return [first_slot + i * interval for i in range(count)]


# ---------------------------------------------------------------- upload

def upload_short_to_youtube(
    video_path: str,
    title: str,
    description: str,
    privacy_status: str = "private",
    publish_at: datetime | None = None,
    tags: list[str] | None = None,
    category_id: str = "22",  # People & Blogs
    made_for_kids: bool = False,
    youtube=None,
    progress=None,
    max_retries: int = 5,
) -> dict:
    """Upload a vertical clip as a YouTube Short.

    privacy_status: "private" (default), "unlisted" or "public" - applied immediately.
    publish_at: if given, the video is uploaded private and YouTube publishes it at that time
                (naive datetimes are interpreted as local time). Must be in the future.
    Returns {"video_id", "url", "title", "privacy_status", "publish_at"}.
    """
    if not os.path.exists(video_path):
        raise PublishError(f"Video not found: {video_path}")
    if privacy_status not in PRIVACY_STATUSES:
        raise PublishError(f"privacy_status must be one of {PRIVACY_STATUSES}")

    status: dict = {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": made_for_kids}
    publish_at_str = None
    if publish_at is not None:
        aware = publish_at if publish_at.tzinfo else publish_at.astimezone()
        if aware <= datetime.now(timezone.utc) + timedelta(minutes=5):
            raise PublishError("publish_at must be at least a few minutes in the future.")
        publish_at_str = _to_rfc3339_utc(aware)
        status = {**status, "privacyStatus": "private", "publishAt": publish_at_str}

    final_title = format_shorts_title(title)
    desc = (description or "").strip()
    if SHORTS_TAG.lower() not in desc.lower():
        desc = f"{desc}\n\n{SHORTS_TAG}".strip()
    body = {
        "snippet": {
            "title": final_title,
            "description": desc[:5000],
            "tags": (tags or [])[:30],
            "categoryId": category_id,
        },
        "status": status,
    }

    youtube = youtube or get_youtube_client()
    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retries = 0
    while response is None:
        try:
            upload_status, response = request.next_chunk()
            if upload_status and progress:
                progress(upload_status.progress())
            retries = 0
        except HttpError as exc:
            code = getattr(exc.resp, "status", None)
            if code in _RETRYABLE_STATUS and retries < max_retries:
                retries += 1
                time.sleep(min(60, 2 ** retries + random.random()))
                continue
            reason = exc.reason if hasattr(exc, "reason") else str(exc)
            if code == 403 and "quota" in str(exc).lower():
                reason = "YouTube API daily quota exceeded (an upload costs ~1600 units of the default 10,000)."
            raise PublishError(f"YouTube upload failed ({code}): {reason}") from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            if retries < max_retries:
                retries += 1
                time.sleep(min(60, 2 ** retries + random.random()))
                continue
            raise PublishError(f"Network error during upload: {exc}") from exc

    if progress:
        progress(1.0)
    video_id = response["id"]
    return {
        "video_id": video_id,
        "url": f"https://youtube.com/shorts/{video_id}",
        "title": final_title,
        "privacy_status": response.get("status", {}).get("privacyStatus", status["privacyStatus"]),
        "publish_at": publish_at_str,
    }
