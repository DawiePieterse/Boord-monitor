"""The paired-device cookie: the second way in, beside Tailscale.

Tailscale on every phone is the part of this suite the farm finds hardest to
live with, so a device can instead be *paired* once and remembered. Boord
does the pairing (its Admin app has the PIN and the pairing QR) and hands the
browser a signed cookie; every other app on the farm's domain, this one
included, only has to check that cookie. Nothing here issues a first cookie.

THE SAME FILE IN EVERY APP. This module is meant to be copied unchanged into
each Boord app, so it knows nothing about Kudde: which roles an app admits is
the caller's question (see security.py).

The cookie is `<payload>.<signature>`, both base64url:

  payload    JSON: {"id": "...", "role": "admin|field|dashboard",
                    "exp": <unix seconds>, "dom": "bekfontein.co.za"}
  signature  HMAC-SHA256 of the payload text under the farm's secret

  id    names the one device, so a lost phone is revoked alone (a line in
        device_revoked.txt) instead of changing the secret and re-pairing
        every tablet on the farm in the middle of a harvest.
  dom   the cookie's Domain, chosen by Boord at pairing and carried signed,
        so a renewal never has to guess the farm's domain from a Host header.

The secret and the revoked list live in Boord's data folder, because Boord
is the app that writes them. No secret file means no cookie is ever valid:
until Boord has paired its first device, this app behaves exactly as it did
when Tailscale was the only way in.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

COOKIE_NAME = "boord_device"

# Chrome refuses a cookie lifetime beyond 400 days, so that is the most a
# device can be remembered for. It is renewed in use (see needs_renewal), so
# a tablet that is opened at least once a year is never logged out - a
# mid-season re-pairing is exactly the disruption this exists to avoid.
LIFETIME_SECONDS = 400 * 24 * 3600
# Renew once the cookie is a month old rather than on every request.
_RENEW_BELOW_SECONDS = LIFETIME_SECONDS - 30 * 24 * 3600

# Where Boord keeps the secret and the revoked list. The default is Boord
# beside this repo, which is how the farm PC is laid out (C:\Boord, C:\Kudde)
# and the same convention Boord's planner uses for its siblings' databases.
AUTH_DIR_ENV = "BOORD_AUTH_DIR"
_DEFAULT_AUTH_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "Boord", "data")
SECRET_FILE = "device_auth_secret.txt"
REVOKED_FILE = "device_revoked.txt"

# A short secret is a guessable one, and an empty file must never sign
# anything: HMAC under "" is a signature anybody can make.
_MIN_SECRET_LENGTH = 32


def _auth_dir() -> str:
    return os.environ.get(AUTH_DIR_ENV) or _DEFAULT_AUTH_DIR


def _secret() -> Optional[bytes]:
    # ponytail: read on every request that carries a cookie, no cache. One
    # small file and a farm's traffic; cache on mtime if that ever changes.
    try:
        with open(os.path.join(_auth_dir(), SECRET_FILE), encoding="utf-8") as f:
            secret = f.read().strip()
    except OSError:
        return None
    return secret.encode() if len(secret) >= _MIN_SECRET_LENGTH else None


def _revoked_ids() -> set:
    """One device id per line; text after a # is a note (whose phone it was)."""
    try:
        with open(os.path.join(_auth_dir(), REVOKED_FILE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    return {line.split("#")[0].strip() for line in lines} - {""}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, secret: bytes) -> str:
    """The cookie value for this payload."""
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


def verify(value: str, now: Optional[float] = None) -> Optional[dict]:
    """The payload of a cookie this farm signed, that has not expired and
    whose device has not been revoked - or None. Every way of being wrong is
    the same None: a cookie is typed by nobody, so there is no near-miss
    worth explaining to whoever sent it."""
    secret = _secret()
    if secret is None or not value:
        return None
    try:
        body, signature = value.split(".")
        expected = hmac.new(secret, body.encode(), hashlib.sha256).digest()
        # Checked before the payload is even parsed, and in constant time.
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        payload = json.loads(_unb64(body))
        if not isinstance(payload, dict):
            return None
        device_id, role, expires = payload["id"], payload["role"], payload["exp"]
        if not (isinstance(device_id, str) and device_id and isinstance(role, str)):
            return None
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    if expires <= (time.time() if now is None else now):
        return None
    if device_id in _revoked_ids():
        return None
    return payload


def needs_renewal(payload: dict, now: Optional[float] = None) -> bool:
    return payload["exp"] - (time.time() if now is None else now) < _RENEW_BELOW_SECONDS


def renew(response, payload: dict) -> None:
    """Give a device that is still in use another full lifetime. Any app may
    do this, not only Boord: a herdsman's phone may never open Boord at all,
    and it is this app that knows the phone is still being used."""
    secret = _secret()
    if secret is None:
        return
    fresh = dict(payload, exp=int(time.time()) + LIFETIME_SECONDS)
    domain = payload.get("dom")
    response.set_cookie(
        COOKIE_NAME, sign(fresh, secret), max_age=LIFETIME_SECONDS,
        domain=domain if isinstance(domain, str) and domain else None,
        # Lax: sent on a link opened from WhatsApp, never on another site's
        # POST. HttpOnly: page script (and anything injected into it) cannot
        # read the one credential this device has.
        secure=True, httponly=True, samesite="lax", path="/",
    )
