"""One-time setup for the hosted Quipcuts app. Run it yourself in a terminal, from the repo root:

    .venv\\Scripts\\python deploy\\set_quipcuts_secrets.py

Secret values are typed at hidden prompts, so they never appear on screen, in shell history or
in a chat. It checks each one actually works before saving anything:
  1. connects to the Neon database and creates the tables,
  2. writes, reads and deletes a test file in the R2 bucket, and sets the bucket's CORS rules
     so browsers on quipcuts.app can upload straight to it,
  3. saves everything as the Modal secret "quipcuts",
  4. writes web/.env.production with the public values the frontend build needs.
"""

import getpass
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUCKET = "quipuclip"  # the R2 bucket as created in the dashboard
API_URL = "https://aneek1--quipcuts-api.us-east.modal.run"
ORIGINS = ["https://quipcuts.app", "https://www.quipcuts.app", "http://localhost:5173"]


def ask(prompt: str, hidden: bool, check=lambda v: True, hint: str = "") -> str:
    while True:
        v = (getpass.getpass if hidden else input)(prompt).strip()
        if v and check(v):
            return v
        got = "nothing (if you pasted, try right-click or Ctrl+Shift+V)" if not v else f"{len(v)} characters"
        print(f"  That doesn't look right: got {got}. {hint}")


print("Quipcuts setup. Hidden prompts show nothing while you paste; just press Enter after.\n")
db_url = ask("Neon POOLED connection string (hidden): ", True,
             lambda v: v.startswith(("postgres://", "postgresql://")) and "-pooler" in v,
             "Neon dashboard -> Connect, with 'Connection pooling' switched on (the host contains -pooler).")
clerk_pk = ask("Clerk publishable key (pk_live_... or pk_test_...): ", False, lambda v: v.startswith("pk_"),
               "Clerk dashboard -> API keys -> Publishable key.")
r2_account = ask("Cloudflare account ID (32 hex characters): ", False, lambda v: len(v) == 32,
                 "Cloudflare dashboard -> R2 -> Account details.")
r2_key = ask("R2 access key ID (hidden): ", True, lambda v: len(v) == 32,
             "The Access Key ID is 32 characters (not the 'Token value').")
r2_secret = ask("R2 secret access key (hidden): ", True, lambda v: len(v) == 64,
                "The Secret Access Key is 64 characters.")
contact = input("Contact email shown on the privacy and terms pages (optional): ").strip()

values = {
    "DATABASE_URL": db_url, "CLERK_PUBLISHABLE_KEY": clerk_pk, "R2_ACCOUNT_ID": r2_account,
    "R2_ACCESS_KEY_ID": r2_key, "R2_SECRET_ACCESS_KEY": r2_secret, "R2_BUCKET": BUCKET,
    "OPENCLIP_SIGNING_KEY": secrets.token_hex(32),
}

print("\n1/4 Checking the database...")
os.environ.update(values)
# create the tables over the direct connection; the app itself uses the pooled one
os.environ["DATABASE_URL"] = db_url.replace("-pooler", "", 1)
sys.path.insert(0, str(ROOT))
os.environ["STORAGE"] = "local"  # don't build the R2 client from config yet
from server.db import init_db  # noqa: E402

try:
    init_db()
except Exception as exc:
    sys.exit(f"  Couldn't connect to Neon: {exc}\nNothing was saved.")
print("  OK, tables are ready.")

print("2/4 Checking the R2 bucket...")
import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

s3 = boto3.client("s3", endpoint_url=f"https://{r2_account}.r2.cloudflarestorage.com", aws_access_key_id=r2_key,
                  aws_secret_access_key=r2_secret, region_name="auto", config=Config(signature_version="s3v4"))
try:
    s3.put_object(Bucket=BUCKET, Key="setup-check.txt", Body=b"ok")
    assert s3.get_object(Bucket=BUCKET, Key="setup-check.txt")["Body"].read() == b"ok"
    s3.delete_object(Bucket=BUCKET, Key="setup-check.txt")
except Exception as exc:
    sys.exit(f"  R2 check failed: {exc}\nCheck the bucket is named '{BUCKET}' and the token can read and write it. "
             "Nothing was saved.")
print("  OK, read/write works.")
try:
    s3.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": ORIGINS, "AllowedMethods": ["PUT", "GET", "HEAD"],
        "AllowedHeaders": ["content-type"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]})
    print("  OK, browser uploads are allowed from quipcuts.app.")
except Exception:
    # an Object Read & Write token can't change bucket settings; add the rule in the dashboard instead
    print("  Note: this token can't set the bucket's CORS rules (expected for a read/write token).\n"
          f"  Add them in the dashboard: R2 -> {BUCKET} -> Settings -> CORS policy.")

print("3/4 Saving the Modal secret 'quipcuts'...")
import modal  # noqa: E402

# create() with allow_existing keeps old values, so replace the secret on a re-run
modal.Secret.objects.delete("quipcuts", allow_missing=True)
modal.Secret.objects.create("quipcuts", values)
print("  OK.")

print("4/4 Writing web/.env.production (public values only)...")
(ROOT / "web" / ".env.production").write_text(
    f"VITE_API_URL={API_URL}\nVITE_CLERK_PUBLISHABLE_KEY={clerk_pk}\nVITE_CONTACT_EMAIL={contact}\n", encoding="utf-8")
print("  OK.\n\nAll set. Tell Claude it's done.")
