"""Boord Monitor: one screen for whether the farm PC, its five apps and its
Tailscale connection are all right - and a phone alert when they are not.

One `uvicorn main:app` process. On the farm PC (MONITOR_ROLE=farm) it runs
beside the apps on port 8050, bound to 127.0.0.1 and published with
`tailscale serve --https=8050`, like every sibling. Off the farm
(MONITOR_ROLE=remote) the same code watches the farm from outside - see
config.py.

Reading is open to anyone who can reach the port, which the binding makes
"anyone on the tailnet". Restarting an app and turning on alerts need the
admin PIN (admin_auth.py). The remote watcher's copy of the push key needs
the remote token instead, because no person is there to type a PIN.
"""
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import admin_auth
import config
import monitor
import push
import restart
from admin_auth import COOKIE, COOKIE_MAX_AGE, require_admin
from system import System

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = FastAPI(title="Boord Monitor")


@app.on_event("startup")
def on_startup():
    if config.ROLE == "farm":
        push.public_key()  # creates the key pair on first start
        if not os.path.exists(config.REMOTE_TOKEN_FILE):
            with open(config.REMOTE_TOKEN_FILE, "w", encoding="ascii") as fh:
                fh.write(secrets.token_urlsafe(32))
    monitor.start()


# --- reading -----------------------------------------------------------------

_last_manual = 0.0


@app.get("/api/status")
def status():
    """The latest run. Before the loop's first run has finished (the first
    minute after boot), run one now rather than show an empty screen."""
    if not monitor.latest:
        monitor.run_once(System())
    return monitor.latest


@app.post("/api/check")
def check_now():
    """The "Check now" button. Open to the tailnet like the reading is - it
    only runs the same checks sooner - but rate-limited, because each run
    starts PowerShell and probes ten URLs, and a stuck finger should not
    make the farm PC do that forty times."""
    global _last_manual
    if time.monotonic() - _last_manual < 10:
        return monitor.latest
    _last_manual = time.monotonic()
    return monitor.run_once(System())


@app.get("/api/session")
def session(request: Request):
    """What this phone may do here. Never 401s: the dashboard calls it on
    every load, signed in or not."""
    farm = config.ROLE == "farm"
    return {
        "role": config.ROLE,
        "admin": admin_auth.token_valid(request.cookies.get(COOKIE)),
        "pin_set": admin_auth.pin_is_set(),
        "push_key": push.public_key() if farm else None,
        "subscribers": len(push.load_subs()),
        "restart": restart.current(),
    }


# --- signing in --------------------------------------------------------------

class LoginIn(BaseModel):
    pin: str


def _set_cookie(response: Response) -> None:
    response.set_cookie(COOKIE, admin_auth.make_token(), max_age=COOKIE_MAX_AGE,
                        httponly=True, secure=True, samesite="strict")


@app.post("/api/admin/login")
def login(body: LoginIn, response: Response):
    admin_auth.attempt_login(body.pin)
    _set_cookie(response)
    return {"ok": True}


@app.post("/api/admin/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, httponly=True, secure=True, samesite="strict")
    return {"ok": True}


# --- acting (PIN) ------------------------------------------------------------

def _farm_only():
    if config.ROLE != "farm":
        raise HTTPException(409, "Only the monitor on the farm PC can do that")


@app.post("/api/apps/{key}/restart", dependencies=[Depends(require_admin), Depends(_farm_only)])
def restart_app(key: str):
    try:
        return restart.start(key)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/restart", dependencies=[Depends(require_admin)])
def restart_status():
    return restart.current() or {}


class SubscribeIn(BaseModel):
    subscription: dict
    label: str = ""


class UnsubscribeIn(BaseModel):
    endpoint: str


@app.post("/api/push/subscribe", dependencies=[Depends(require_admin), Depends(_farm_only)])
def subscribe(body: SubscribeIn):
    try:
        push.add_sub(body.subscription, body.label)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "subscribers": len(push.load_subs())}


@app.post("/api/push/unsubscribe", dependencies=[Depends(_farm_only)])
def unsubscribe(body: UnsubscribeIn):
    """No PIN: turning alerts OFF for a phone only needs that phone's own
    endpoint, which nobody but the phone knows, and a person who wants the
    buzzing to stop should not be asked to remember a PIN first."""
    push.remove_sub(body.endpoint)
    return {"ok": True, "subscribers": len(push.load_subs())}


@app.post("/api/push/test", dependencies=[Depends(require_admin), Depends(_farm_only)])
def push_test():
    return push.send("Farm: test alert", "Alerts from Boord Monitor reach this phone.", tag="test")


# --- the remote watcher's copy of the push key -------------------------------

@app.get("/api/remote/bundle", dependencies=[Depends(_farm_only)])
def remote_bundle(x_monitor_token: str = Header("")):
    """Everything the remote watcher needs to alert the farm's phones on its
    own: the signing key and the subscription list. Behind the remote token,
    not the PIN, because the caller is a program. The key is a credential -
    anyone holding it can push to these phones - which is why it only ever
    travels over the tailnet and only to a caller holding the token."""
    try:
        with open(config.REMOTE_TOKEN_FILE, encoding="ascii") as fh:
            expected = fh.read().strip()
    except OSError:
        expected = ""
    if not expected or not hmac.compare_digest(x_monitor_token.strip(), expected):
        raise HTTPException(403, "Wrong or missing remote token")
    return {"key_pem": push.key_pem(), "subscriptions": push.load_subs()}


# --- version -----------------------------------------------------------------

API_JS = os.path.join(config.FRONTEND_DIR, "api.js")
# Same anchored expression scripts/release.sh greps with - keep them identical.
_VERSION_RE = re.compile(r'^  VERSION: "(.*)",$', re.MULTILINE)


def _git(args: list) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        p = subprocess.run([git] + args, cwd=config.REPO_ROOT, capture_output=True, text=True,
                           timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_version_cache: dict = {}


@app.get("/api/version")
def version():
    """Same field names as Boord's and Labour's, because Boord's
    heartbeat.ps1 reads tag/state/frontend_version from every app."""
    if not _version_cache:
        try:
            with open(API_JS, encoding="utf-8") as fh:
                m = _VERSION_RE.search(fh.read())
            fv = m.group(1) if m else ""
        except OSError:
            fv = ""
        # Ahead of the last tag (a branch checked out to try a fix), `tag` is
        # git describe's "v1.5-3-gabc1234" rather than null, as in Labour's
        # version.py: null told whoever read the heartbeat nothing about what
        # was actually running, which is exactly when they need to know.
        tag = _git(["describe", "--tags", "--exact-match"])
        if tag:
            state = "release"
        else:
            tag = _git(["describe", "--tags", "--always"])
            state = "ahead" if tag else "unknown"
        _version_cache.update({"app": "Boord Monitor", "tag": tag or None, "state": state,
                               "frontend_version": fv, "role": config.ROLE})
    return _version_cache


class NoCacheStaticFiles(StaticFiles):
    """Force revalidation, as every sibling does - a phone left on the
    dashboard for a week must not keep running last month's JS."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Catch-all, must be last.
app.mount("/", NoCacheStaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
