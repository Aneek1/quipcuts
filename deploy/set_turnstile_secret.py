"""Create the Modal secret "openclip-turnstile" for the hosted demo's Cloudflare check.

    python deploy/set_turnstile_secret.py

Asks for the widget's secret key with hidden input, so it never lands in shell history
or on screen. The site key is public (it's embedded in the check page).
"""
import getpass

import modal

SITE_KEY = "0x4AAAAAAFeeaUV5eJuoZFr8"  # Cloudflare Turnstile widget "openclip"

secret = getpass.getpass("Turnstile secret key for the 'openclip' widget (input hidden): ").strip()
if not secret.startswith("0x"):
    raise SystemExit("That doesn't look like a Turnstile secret key (they start with 0x). Nothing was saved.")
# create() with allow_existing keeps old values, so replace the secret on a re-run
modal.Secret.objects.delete("openclip-turnstile", allow_missing=True)
modal.Secret.objects.create("openclip-turnstile", {"TURNSTILE_SITE_KEY": SITE_KEY, "TURNSTILE_SECRET_KEY": secret})
print("Saved Modal secret 'openclip-turnstile'.")
