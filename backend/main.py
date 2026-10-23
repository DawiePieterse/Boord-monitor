"""Boord Monitor: one screen for whether the farm PC, its six apps and its
Tailscale connection are all right - and a phone alert when they are not.

One `uvicorn main:app` process. On the farm PC (MONITOR_ROLE=farm) it runs
beside the apps on port 8050, bound to 127.0.0.1 and published with
`tailscale serve --https=8050`, like every sibling. Off the farm
(MONITOR_ROLE=remote) the same code watches the farm from outside - see
config.py.

Reading is open to anyone who can reach the port, which the binding makes
"anyone on the tailnet". Restarting an app, turning on alerts, acknowledging
an incident and setting a maintenance window need the admin PIN
(admin_auth.py). The remote watcher's copy of the push key needs the remote
token instead, because no person is there to type a PIN.

Besides the dashboard's /api/..., three endpoints follow conventions other
tools expect, so the farm can be plugged into them without changing this:
  /metrics  - Prometheus text format (metrics.py);
  /healthz  - liveness: the process answers (Kubernetes' naming, which
              Uptime Kuma, load balancers and most probes default to);
  /readyz   - readiness: the check loop is running and the history can be
              written. 503 otherwise, so an outside probe sees a stuck
              monitor, not just a dead one.
"""
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import admin_auth
import config
import metrics
import monitor
import push
import restart
import slo
import store
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


# --- history, SLOs, incidents (read) -------------------------------------------

_overview_cache: dict = {}

# Groups whose checks get uptime bars on the status page. Devices are left
# out on purpose: a tablet switched off every night is not an outage, and 90
# grey-and-green bars per tablet would bury the apps.
COMPONENT_GROUPS = ("apps", "tailscale", "pc", "guards", "backups", "farm", "watcher")


def _offset_s(sys: System) -> int:
    """The farm PC's distance from UTC, to the quarter hour, so the uptime
    bars break at the farm's midnight."""
    diff = (sys.local_now() - sys.now()).total_seconds()
    return int(round(diff / 900.0)) * 900


@app.get("/api/overview")
def overview(days: int = Query(90, ge=1, le=400)):
    """Everything the status page needs beyond the current run: SLOs per app,
    uptime bars per component, and the incident measures. Worked out at most
    once per check run - the dashboard asks every 30 s, the answer only
    changes every minute."""
    snap = status()
    key = (snap.get("checked_at"), days)
    if _overview_cache.get("key") == key:
        return _overview_cache["value"]
    sys = System()
    now = sys.now()
    t = store.ts(now)
    results = snap.get("checks") or []
    installed = [c["app"] for c in results if c.get("group") == "apps" and c["status"] != "off"]
    slos = slo.overview(now, installed) if config.ROLE == "farm" else []
    for s_ in slos:
        s_["status"] = slo.status_of(s_)
    components = [c for c in results if c.get("group") in COMPONENT_GROUPS and c["status"] != "off"]
    bars = store.daily([c["id"] for c in components], days, t, _offset_s(sys))
    comp = []
    for c in components:
        b = bars.get(c["id"]) or []
        good = sum((x["availability"] or 0) * x["counted"] for x in b)
        n = sum(x["counted"] for x in b)
        comp.append({"id": c["id"], "group": c["group"], "label": c["label"], "status": c["status"],
                     "availability": (good / n) if n else None, "days": b})
    a30 = [s_["availability"]["30d"] for s_ in slos if s_["availability"]["30d"] is not None]
    budgets = [s_["error_budget"]["remaining"] for s_ in slos if s_["error_budget"]["remaining"] is not None]
    value = {
        "checked_at": snap.get("checked_at"), "days": days,
        "slo": slos, "components": comp,
        "kpi": {
            "availability_30d": (sum(a30) / len(a30)) if a30 else None,
            "error_budget_min": min(budgets) if budgets else None,
            "incidents_open": len(snap.get("incidents") or []),
            "incidents_30d": store.incident_stats(t - 30 * 86400, t),
        },
    }
    _overview_cache.update(key=key, value=value)
    return value


@app.get("/api/history/{check_id}")
def history(check_id: str, hours: int = Query(24, ge=1, le=48)):
    """One check over the last hours, in slots sized so a phone-width chart
    gets about 96 of them (15 min each over a day)."""
    sys = System()
    t = store.ts(sys.now())
    step = max(config.CHECK_INTERVAL_S, hours * 3600 // 96)
    return {"check_id": check_id, "hours": hours, "step_s": step,
            "points": store.series(check_id, t - hours * 3600, t, step),
            "latency": store.latency(check_id, t - hours * 3600, t)}


@app.get("/api/incidents")
def list_incidents(limit: int = Query(50, ge=1, le=500)):
    t = store.ts(System().now())
    return {"incidents": store.incidents(limit=limit),
            "stats": {"7d": store.incident_stats(t - 7 * 86400, t),
                      "30d": store.incident_stats(t - 30 * 86400, t),
                      "90d": store.incident_stats(t - 90 * 86400, t)},
            "silences": store.silences(t, active_only=False)}


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: int):
    inc = store.get_incident(incident_id)
    if inc is None:
        raise HTTPException(404, "No such incident")
    return inc


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


class AckIn(BaseModel):
    by: str = ""


@app.post("/api/incidents/{incident_id}/ack", dependencies=[Depends(require_admin)])
def ack_incident(incident_id: int, body: AckIn):
    """"I've seen it, I'm on it." Stops the reminders for this incident;
    it still resolves by itself when the check recovers."""
    inc = store.acknowledge(incident_id, store.ts(System().now()), (body.by or "admin").strip()[:40])
    if inc is None:
        raise HTTPException(409, "That incident is not open - already acknowledged or resolved")
    _overview_cache.clear()
    return inc


class SilenceIn(BaseModel):
    match: str
    minutes: int
    reason: str = ""


@app.post("/api/silences", dependencies=[Depends(require_admin)])
def add_silence(body: SilenceIn):
    """A maintenance window: from now, for `minutes`, no push for the checks
    `match` names ("*", "group:<key>" or a check id). Capped at a week - a
    silence forgotten for longer is an alert switched off."""
    match = body.match.strip()
    if not match or len(match) > 80:
        raise HTTPException(422, "Say what to silence")
    if not 1 <= body.minutes <= 7 * 24 * 60:
        raise HTTPException(422, "A silence lasts between a minute and a week")
    t = store.ts(System().now())
    return store.add_silence(match, t, t + body.minutes * 60, (body.reason or "maintenance").strip()[:120], t)


@app.post("/api/silences/{silence_id}/end", dependencies=[Depends(require_admin)])
def end_silence(silence_id: int):
    if not store.end_silence(silence_id, store.ts(System().now())):
        raise HTTPException(409, "That silence has already ended")
    return {"ok": True}


# --- standard endpoints for other tools ------------------------------------------

@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    if not monitor.latest:
        monitor.run_once(System())
    return PlainTextResponse(metrics.render(), media_type=metrics.CONTENT_TYPE)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Ready means the loop has run recently and the history can be
    written. Three intervals of silence is a stuck loop, the failure that
    leaves the dashboard looking fine while nothing is being checked."""
    last = monitor.stats["last_run_at"]
    age = time.time() - last if last else None
    loop_ok = os.environ.get("MONITOR_NO_LOOP") is not None or (age is not None and age < 3 * config.CHECK_INTERVAL_S + 60)
    db_ok = store.writable()
    body = {"status": "ok" if loop_ok and db_ok else "fail",
            "checks": {"loop": "ok" if loop_ok else "stale", "history": "ok" if db_ok else "unwritable"},
            "last_run_age_s": round(age, 1) if age is not None else None}
    return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)


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
