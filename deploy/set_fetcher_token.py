r"""Create (or rotate) the shared token between the API and the home-PC link downloader.

    .venv\Scripts\python deploy\set_fetcher_token.py

Saves it as the Modal secret "quipcuts-fetcher" and in fetcher/.env (git-ignored) for Docker.
Redeploy the API afterwards (modal deploy deploy/modal_web.py) and restart the container.
"""
import secrets
from pathlib import Path

import modal

token = secrets.token_urlsafe(32)
modal.Secret.objects.delete("quipcuts-fetcher", allow_missing=True)
modal.Secret.objects.create("quipcuts-fetcher", {"FETCHER_TOKEN": token})
(Path(__file__).resolve().parent.parent / "fetcher" / ".env").write_text(f"FETCHER_TOKEN={token}\n", encoding="utf-8")
print("Saved the Modal secret 'quipcuts-fetcher' and fetcher/.env.")
