"""The admin PIN - what stands between "anyone on the tailnet" and the restart button.

Looking is open to the whole tailnet, like every sibling app: a dashboard
that says Boord is down tells nobody anything they could not find out by
opening Boord. Acting is not. A restart takes the pack house off its screens
for half a minute, and turning on alerts hands a phone a copy of every
problem this PC has, so both need the PIN.

The PIN is set on the server console (set_pin.py), never through the web
page - otherwise whoever opened the dashboard first on a fresh install
would get to choose it.

A signed-in phone stays signed in: the cookie carries no expiry. It does
carry a fingerprint of the PIN hash, so changing the PIN signs every phone
out - that is how a lost or departed phone is revoked.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import HTTPException, Request

import config

PIN_FILE = os.path.join(config.DATA_DIR, "admin_pin.txt")
SECRET_FILE = os.path.join(config.DATA_DIR, ".secret_key")
COOKIE = "monitor_admin"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600  # "no expiry" - browsers cap cookie lifetimes, so ask for plenty
MIN_PIN_LENGTH = 4
_ITERATIONS = 200_000

# One global counter, not per phone - this app has one or two admins.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
_failures = 0
_locked_until = 0.0
_lock = threading.Lock()


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, _ITERATIONS).hex()


def _stored() -> Optional[tuple]:
    """(salt bytes, hash hex), or None when no PIN has been set."""
    try:
        with open(PIN_FILE, encoding="utf-8") as fh:
            salt_hex, digest = fh.read().strip().split(":")
        return bytes.fromhex(salt_hex), digest
    except (OSError, ValueError):
        return None


def pin_is_set() -> bool:
    return _stored() is not None


def set_pin(pin: str) -> None:
    pin = pin.strip()
    if len(pin) < MIN_PIN_LENGTH:
        raise ValueError(f"The PIN must be at least {MIN_PIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    with open(PIN_FILE, "w", encoding="utf-8") as fh:
        fh.write(f"{salt.hex()}:{_hash(pin, salt)}")


def check_pin(pin: str) -> bool:
    stored = _stored()
    return stored is not None and hmac.compare_digest(_hash(pin.strip(), stored[0]), stored[1])


def _secret() -> bytes:
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
    with open(SECRET_FILE, encoding="utf-8") as fh:
        return fh.read().strip().encode()


def _sign(fingerprint: str) -> str:
    return hmac.new(_secret(), fingerprint.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    fingerprint = _stored()[1][:16]
    return f"{fingerprint}.{_sign(fingerprint)}"


def token_valid(token: Optional[str]) -> bool:
    stored = _stored()
    if not token or stored is None or "." not in token:
        return False
    fingerprint, sig = token.split(".", 1)
    return (hmac.compare_digest(fingerprint, stored[1][:16])
            and hmac.compare_digest(sig, _sign(fingerprint)))


def attempt_login(pin: str) -> None:
    """Raises HTTPException unless `pin` is right; counts failures."""
    global _failures, _locked_until
    if not pin_is_set():
        raise HTTPException(409, "Set the admin PIN on the server first - see MANUAL.md, 'Admin PIN'")
    with _lock:
        if time.monotonic() < _locked_until:
            raise HTTPException(429, "Too many wrong PINs - wait 5 minutes and try again")
        if check_pin(pin):
            _failures = 0
            return
        _failures += 1
        if _failures >= MAX_FAILURES:
            _failures = 0
            _locked_until = time.monotonic() + LOCKOUT_SECONDS
    raise HTTPException(401, "Wrong PIN")


def require_admin(request: Request) -> None:
    if not token_valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Admin PIN needed")
