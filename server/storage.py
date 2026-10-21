"""File storage behind one interface: a local folder, or a Cloudflare R2 bucket.

Browsers never stream video through the API in R2 mode: they PUT uploads and GET clips with
presigned URLs straight to R2. Local mode imitates that with HMAC-signed URLs served by the API,
so the frontend code is the same in both.

Keys look like  projects/<project_id>/source.mp4,  projects/<id>/clips/01_title.mp4
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from . import config

URL_TTL_S = 6 * 3600


def _sig(method: str, key: str, exp: int) -> str:
    msg = f"{method}\n{key}\n{exp}".encode()
    return hmac.new(config.SIGNING_KEY.encode(), msg, hashlib.sha256).hexdigest()


def check_local_sig(method: str, key: str, exp: int, sig: str) -> bool:
    return exp > time.time() and hmac.compare_digest(_sig(method, key, exp), sig)


def safe_key(key: str) -> str:
    parts = key.replace("\\", "/").split("/")
    if not key or any(p in ("", ".", "..") for p in parts) or ":" in key:
        raise ValueError(f"bad storage key: {key!r}")
    return "/".join(parts)


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root / "files"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        return self.root / safe_key(key)

    def put_url(self, key: str, content_type: str) -> str:
        exp = int(time.time()) + URL_TTL_S
        return f"{config.PUBLIC_API_URL}/files/{quote(key)}?exp={exp}&sig={_sig('PUT', key, exp)}"

    def get_url(self, key: str, download: str | None = None) -> str:
        exp = int(time.time()) + URL_TTL_S
        extra = f"&{urlencode({'dl': download})}" if download else ""
        return f"{config.PUBLIC_API_URL}/files/{quote(key)}?exp={exp}&sig={_sig('GET', key, exp)}{extra}"

    def size(self, key: str) -> int | None:
        p = self.path(key)
        return p.stat().st_size if p.exists() else None

    def download(self, key: str, dest: str) -> None:
        if os.path.abspath(dest) != os.path.abspath(self.path(key)):
            shutil.copyfile(self.path(key), dest)

    def upload(self, src: str, key: str, content_type: str = "application/octet-stream") -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copyfile(src, dest)

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self.root / safe_key(prefix.rstrip("/")), ignore_errors=True)


class R2Storage:
    def __init__(self):
        import boto3
        from botocore.config import Config
        self.bucket = config.R2_BUCKET
        self.s3 = boto3.client(
            "s3", endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=config.R2_ACCESS_KEY_ID, aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            region_name="auto", config=Config(signature_version="s3v4"))

    def path(self, key: str) -> None:
        return None

    def put_url(self, key: str, content_type: str) -> str:
        return self.s3.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": safe_key(key), "ContentType": content_type},
            ExpiresIn=URL_TTL_S)

    def get_url(self, key: str, download: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": safe_key(key)}
        if download:  # makes the browser save the file instead of playing it
            params["ResponseContentDisposition"] = f'attachment; filename="{download}"'
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=URL_TTL_S)

    def size(self, key: str) -> int | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=safe_key(key))["ContentLength"]
        except self.s3.exceptions.ClientError:
            return None

    def download(self, key: str, dest: str) -> None:
        self.s3.download_file(self.bucket, safe_key(key), dest)

    def upload(self, src: str, key: str, content_type: str = "application/octet-stream") -> None:
        self.s3.upload_file(src, self.bucket, safe_key(key), ExtraArgs={"ContentType": content_type})

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=safe_key(key))

    def delete_prefix(self, prefix: str) -> None:
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=safe_key(prefix.rstrip("/")) + "/")
        for page in pages:
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": objs})


storage = R2Storage() if config.STORAGE == "r2" else LocalStorage(config.DATA_DIR)
