"""Web Push to the phones that asked for alerts.

One key pair, held by the farm monitor, signs every push. A subscription is
tied to the key it was made with, not to the server that sends to it, so the
remote watcher - which needs to push precisely when the farm PC cannot -
uses a copy of the same key and the same subscription list, fetched from the
farm (`/api/remote/bundle`) while the farm is still up. The phone installs
the dashboard once, from the farm, and gets alerts from both.

On iPhone, Web Push only works for a site added to the Home Screen (iOS 16.4
or later), and only once the person taps "Turn on alerts" there - Safari
will not ask from an ordinary tab. MANUAL.md walks through it.
"""
import base64
import datetime as dt
import json
import logging
import os
import threading
from typing import List, Optional

import config

log = logging.getLogger("monitor.push")

KEY_FILE = os.path.join(config.DATA_DIR, "vapid_private.pem")
SUBS_FILE = os.path.join(config.DATA_DIR, "subscriptions.json")
_lock = threading.Lock()


def _vapid():
    from py_vapid import Vapid
    if not os.path.exists(KEY_FILE):
        if config.ROLE != "farm":
            return None
        v = Vapid()
        v.generate_keys()
        v.save_key(KEY_FILE)
    return Vapid.from_file(KEY_FILE)


def public_key() -> Optional[str]:
    """The applicationServerKey a phone subscribes with: the raw P-256 point,
    base64url without padding, which is what PushManager.subscribe wants."""
    from cryptography.hazmat.primitives import serialization
    v = _vapid()
    if v is None:
        return None
    raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_subs() -> List[dict]:
    try:
        with open(SUBS_FILE, encoding="utf-8") as fh:
            subs = json.load(fh)
        return subs if isinstance(subs, list) else []
    except (OSError, ValueError):
        return []


def _save_subs(subs: List[dict]) -> None:
    tmp = SUBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(subs, fh, indent=1)
    os.replace(tmp, SUBS_FILE)


def add_sub(sub: dict, label: str) -> None:
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("That is not a complete push subscription")
    with _lock:
        subs = [s for s in load_subs() if s.get("endpoint") != endpoint]
        subs.append({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                     "label": (label or "phone")[:60],
                     "added": dt.datetime.utcnow().isoformat(timespec="seconds")})
        _save_subs(subs)


def remove_sub(endpoint: str) -> bool:
    with _lock:
        subs = load_subs()
        kept = [s for s in subs if s.get("endpoint") != endpoint]
        if len(kept) != len(subs):
            _save_subs(kept)
        return len(kept) != len(subs)


def replace_all(key_pem: str, subs: List[dict]) -> None:
    """Remote role: take the farm's key and subscription list as they are."""
    with _lock:
        with open(KEY_FILE + ".tmp", "w", encoding="ascii") as fh:
            fh.write(key_pem)
        os.replace(KEY_FILE + ".tmp", KEY_FILE)
        _save_subs(subs)


def key_pem() -> str:
    with open(KEY_FILE, encoding="ascii") as fh:
        return fh.read()


def _webpush(sub: dict, payload: str) -> int:
    """Sends one push. Returns the push service's HTTP status. Kept apart so
    the tests can replace it - nothing in the suite touches the network."""
    from pywebpush import WebPushException, webpush
    try:
        resp = webpush(subscription_info=sub, data=payload, vapid_private_key=KEY_FILE,
                       vapid_claims={"sub": config.VAPID_SUB}, ttl=6 * 3600, timeout=15)
        return resp.status_code
    except WebPushException as e:
        return e.response.status_code if e.response is not None else 0


def send(title: str, body: str, url: str = "/", tag: str = "monitor",
         badge: Optional[int] = None) -> dict:
    """Pushes to every subscribed phone. A subscription the push service
    says is gone (404/410 - the app was removed from the Home Screen, or
    alerts were turned off in iOS Settings) is dropped, so it is not retried
    every minute forever. Only the farm drops them: the remote watcher's list
    is a copy, and the farm's next bundle brings it back in line.

    `badge`, when given, is the number the service worker puts on the Home
    Screen icon; a test push leaves it out, so it doesn't touch the badge."""
    subs = load_subs()
    if not subs or not os.path.exists(KEY_FILE):
        return {"sent": 0, "failed": 0, "subscribers": len(subs)}
    message = {"title": title, "body": body, "url": url, "tag": tag}
    if badge is not None:
        message["badge"] = badge
    payload = json.dumps(message)
    sent, failed, gone = 0, 0, []
    for sub in subs:
        try:
            code = _webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, payload)
        except Exception as e:  # noqa: BLE001 - one bad phone must not stop the others
            log.warning("push to %s failed: %s", sub.get("label"), e)
            code = 0
        if 200 <= code < 300:
            sent += 1
        else:
            failed += 1
            if code in (404, 410):
                gone.append(sub["endpoint"])
    if gone and config.ROLE == "farm":
        for endpoint in gone:
            remove_sub(endpoint)
    return {"sent": sent, "failed": failed, "subscribers": len(subs), "removed": len(gone)}
