"""What the monitor looks at, as plain functions over a System (system.py).

Every check returns one dict:

    {"id", "group", "label", "status", "summary", "detail": [...], "alert": bool, ...}

`status` is one of:
    ok      - fine
    warn    - worth a look on the dashboard; never wakes a phone
    fail    - broken; wakes a phone once it has failed FAIL_AFTER runs in a row
    off     - not installed on this PC, so not a problem
    unknown - could not be checked (e.g. a Windows-only check on a Mac)

Warnings deliberately never push. The things that are only warnings - a key
expiring in ten days, a reboot an hour ago, a 500 from one endpoint - are
real but not urgent, and a phone that buzzes for them is a phone whose
owner learns to ignore it before the night Boord actually goes down.
"""
import datetime as dt
import ntpath
import os
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

import apps
import config
from system import System, parse_json

STATUS_ORDER = {"fail": 3, "warn": 2, "ok": 1, "unknown": 0, "off": 0}

# What "just rebooted" means, for the Uptime tile and for the grace given to
# Google Drive while whoever restarted the PC logs back in.
RECENT_BOOT_S = 30 * 60


# The dashboard's groups, in screen order, per role. Sent with every status so
# a new family of checks shows up without a frontend change.
GROUPS = {
    "farm": [
        ["apps", "Apps"],
        ["tailscale", "Tailscale"],
        ["pc", "Farm PC"],
        ["guards", "Safety nets"],
        ["backups", "Drive backups"],
        ["devices", "Devices"],
    ],
    "remote": [
        ["farm", "The farm, seen from outside"],
        ["watcher", "This watcher"],
    ],
}

# Tile names, by check id. Full labels stay in the sheet; a tile has room for
# about seven letters, and five tiles have to fit across a phone. Kept here,
# next to the labels, so renaming a check can't leave a long name on a tile.
SHORT = {
    "app.owner": "Owner",
    "app.notes": "Notes",
    "app.labour": "Labour",
    "ts.state": "Online",
    "ts.key": "Key",
    "ts.serve": "Ports",
    "ts.cert": "Cert",
    "pc.uptime": "Uptime",
    "guards.log": "Restarts",
    "drive.app": "Drive",
    "guards.update": "Update",
}

# A tablet that has held a crate unsent for longer than this is worth a look.
# An hour, not minutes: a picker working the far end of a block is out of
# signal for a while as a matter of course, and the crates sync on the walk
# back. Past an hour the tablet is either broken off the network or every
# sync is failing, and either way those crates exist nowhere else.
QUEUE_WARN_S = 60 * 60


def check(id: str, group: str, label: str, status: str, summary: str,
          detail: Optional[List[str]] = None, alert: bool = True, **data) -> dict:
    out = {"id": id, "group": group, "label": label, "short": SHORT.get(id, label),
           "status": status, "summary": summary, "detail": detail or [], "alert": alert}
    out.update(data)
    return out


def worst(checks: List[dict]) -> str:
    level = max((STATUS_ORDER[c["status"]] for c in checks), default=0)
    return {3: "fail", 2: "warn", 1: "ok", 0: "ok"}[level]


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _short(seconds: float) -> str:
    """An age or a duration in at most four characters, for the dashboard's
    tiles: 40s, 17m, 5h, 3d."""
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size * (2 if unit == "d" else 1):
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _days_value(days: float) -> str:
    """Days left, as a tile shows it: 62d, 47h, and 0d once it has passed."""
    return _short(days * 86400) if days > 0 else "0d"


KEY_FIX = "In the Tailscale admin console, open the machine's menu and choose 'Disable key expiry'."


def _key_expiry(value: object, now: dt.datetime) -> Optional[tuple]:
    """(days left, date) for a Tailscale KeyExpiry, or None when expiry is
    turned off - Tailscale then reports no date, or one before 2000."""
    expiry = _parse_iso(value)
    if expiry is None or expiry.year < 2000:
        return None
    return (expiry - now).total_seconds() / 86400, expiry.strftime("%Y-%m-%d")


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def _parse_iso(value: object) -> Optional[dt.datetime]:
    """An ISO timestamp as naive UTC. Tailscale writes "...Z" and sometimes
    nanoseconds, which Python 3.9's fromisoformat rejects, so both are
    trimmed first."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


# --- scheduled tasks ---------------------------------------------------------

# One PowerShell call for every task, once a minute. Get-ScheduledTaskInfo
# rather than `schtasks /query /v`, whose output is laid out in the PC's
# display language - a farm PC set to Afrikaans would break a parser written
# against the English labels. JSON is the same in every language.
#
# NextRun is worked out before the hashtable because PowerShell 5.1, which is
# what the farm PC has, is unreliable about an `if` used as a value inside a
# hashtable literal.
_TASKS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$out = foreach ($n in @(%s)) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {
    $i = $t | Get-ScheduledTaskInfo
    $last = $null; if ($i.LastRunTime) { $last = $i.LastRunTime.ToUniversalTime().ToString('o') }
    [pscustomobject]@{ Name = $n; State = "$($t.State)"; LastRun = $last; LastResult = $i.LastTaskResult }
  }
}
ConvertTo-Json -InputObject @($out) -Compress
"""


def query_tasks(sys: System, names: List[str]) -> Optional[Dict[str, dict]]:
    """{task name: info} for the ones registered; None when it could not be
    asked at all (not Windows, or PowerShell failed). None and {} mean very
    different things: None is "don't know which apps are installed", so all
    of them are checked; {} is "none are", so all of them are off."""
    if not sys.is_windows():
        return None
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    rc, out, _err = sys.powershell(_TASKS_PS % quoted)
    if rc != 0:
        return None
    parsed = parse_json(out.strip() or "[]")
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    return {t["Name"]: t for t in parsed if isinstance(t, dict) and t.get("Name")}


def _task_check(sys: System, tasks: Optional[Dict[str, dict]], name: str, id: str,
                label: str, every_min: int, missing_why: str) -> dict:
    if tasks is None:
        return check(id, "guards", label, "unknown", "Scheduled tasks can only be read on the farm PC.")
    t = tasks.get(name)
    if not t:
        return check(id, "guards", label, "warn", f"Not set up on this PC. {missing_why}")
    last = _parse_iso(t.get("LastRun"))
    # Task Scheduler reports a task that has never run as 1999-11-30.
    if last is None or last.year < 2000:
        return check(id, "guards", label, "warn", "Registered, but has never run.")
    age = (sys.now() - last).total_seconds()
    result = t.get("LastResult")
    summary = f"Last ran {_ago(age)} ago"
    status = "ok"
    if age > every_min * 60 * 3 + 120:
        status, summary = "warn", f"Has not run for {_ago(age)} - it should run every {every_min} min."
    elif str(t.get("State")).lower() == "disabled":
        status, summary = "warn", "The task is disabled in Task Scheduler."
    elif result not in (0, None, 267009):  # 267009 = still running right now
        status = "warn"
        summary += f", and its last run ended with code {result}"
    return check(id, "guards", label, status, summary + ("." if not summary.endswith(".") else ""),
                 value=_short(age))


# --- the apps ----------------------------------------------------------------

def _url(host: str, port: int, path: str) -> str:
    return f"https://{host}{'' if port == 443 else ':' + str(port)}{path}"


def app_check(sys: System, app: apps.App, installed: bool, dnsname: Optional[str]) -> dict:
    local = sys.http(f"http://127.0.0.1:{app.port}{app.health}", timeout=10)
    data = {"app": app.key, "port": app.port, "ts_port": app.ts_port,
            "local_ms": local.ms, "local_status": local.status,
            "url": _url(dnsname, app.ts_port, app.path) if dnsname else None,
            "tailnet": None, "version": None, "restartable": installed}

    if not local.answered and not installed:
        return check("app." + app.key, "apps", app.name, "off",
                     "Not installed on this PC.", value="off", word="Not installed", **data)
    if not local.answered:
        return check("app." + app.key, "apps", app.name, "fail",
                     f"Not answering on port {app.port} ({local.error}).", value="down", **data)

    detail = []
    status = "ok"
    summary = f"Up - answered in {local.ms} ms"
    # The tile's one word: the answer time when all is well, otherwise the
    # shortest name for what is wrong.
    value = f"{local.ms}ms"
    if local.status and local.status >= 500:
        status, summary = "warn", f"Running, but answering HTTP {local.status} - a bug a restart may not fix"
        value = str(local.status)
    elif local.ms is not None and local.ms > 5000:
        status, summary = "warn", f"Up, but slow - took {local.ms / 1000:.1f} s to answer"

    if app.version:
        v = sys.http(f"http://127.0.0.1:{app.port}{app.version}", timeout=10, want_json=True)
        info = v.body if isinstance(v.body, dict) else {}
        data["version"] = info.get("version") or info.get("tag") or info.get("frontend_version")
        if info.get("migrations_applied") is False:
            # Boord only. The schema is behind the code: update_server.bat
            # did not finish its migration step, and some screens will 500.
            status, value = "fail", "db"
            summary = "Running, but its database migrations were not applied - re-run update_server.bat"
        update = info.get("update")
        if isinstance(update, dict) and update.get("available") and update.get("latest"):
            detail.append(f"Update available: {update['latest']} (running {update.get('current') or data['version']})")
        backups = info.get("backups")
        if isinstance(backups, dict) and backups.get("last"):
            detail.append(f"Last backup: {backups['last']}")

    if dnsname:
        remote = sys.http(_url(dnsname, app.ts_port, app.health), timeout=10)
        data["tailnet"] = remote.answered
        if not remote.answered:
            status, value = "fail", "no TS"
            summary = (f"Running on this PC, but not reachable over Tailscale at "
                       f":{app.ts_port} ({remote.error}) - phones cannot open it")

    return check("app." + app.key, "apps", app.name, status, summary + ".", detail, value=value, **data)


# --- Tailscale ---------------------------------------------------------------

_STATE_WHY = {
    "NeedsLogin": "Tailscale on this PC is signed out. Sign in again from the Tailscale icon by the clock.",
    "NeedsMachineAuth": "This PC is waiting to be approved in the Tailscale admin console.",
    "Stopped": "Tailscale is switched off on this PC. Turn it on from the icon by the clock.",
    "Starting": "Tailscale is still starting.",
    "NoState": "Tailscale's service is not running.",
}


def tailscale_status(sys: System) -> tuple:
    """(parsed `tailscale status --json`, error text)."""
    rc, out, err = sys.run([config.TAILSCALE_EXE, "status", "--json"], timeout=15)
    if rc == 127:
        return None, "The Tailscale command-line tool was not found on this machine."
    parsed = parse_json(out)
    if not isinstance(parsed, dict):
        return None, (err or out or f"tailscale status exited {rc}").strip()[:200]
    return parsed, ""


def dns_name(status: Optional[dict]) -> Optional[str]:
    name = ((status or {}).get("Self") or {}).get("DNSName") or ""
    return name.rstrip(".") or None


def tailscale_checks(sys: System, status: Optional[dict], err: str, group: str = "tailscale",
                     who: str = "this PC") -> List[dict]:
    if status is None:
        return [check("ts.state", group, "Tailscale", "fail", err)]
    state = status.get("BackendState")
    me = status.get("Self") or {}
    if state != "Running":
        why = _STATE_WHY.get(state, f"Tailscale reports '{state}'.")
        return [check("ts.state", group, "Tailscale", "fail", why.replace("this PC", who))]

    out = []
    health = [h for h in (status.get("Health") or []) if isinstance(h, str)]
    ips = ", ".join(me.get("TailscaleIPs") or [])
    if me.get("Online") is False:
        out.append(check("ts.state", group, "Tailscale", "warn",
                         "Running, but cannot reach Tailscale's coordination servers - "
                         "is the internet down? Existing connections may keep working.", health))
    elif health:
        out.append(check("ts.state", group, "Tailscale", "warn",
                         f"Connected as {dns_name(status)}, with warnings.", health))
    else:
        out.append(check("ts.state", group, "Tailscale", "ok",
                         f"Connected as {dns_name(status)} ({ips})."))

    # Node keys expire after 180 days unless expiry is turned off for the
    # machine in the admin console. When it lapses the PC silently drops off
    # the tailnet and every phone loses every app at once - the single most
    # avoidable outage this whole setup has.
    key = _key_expiry(me.get("KeyExpiry"), sys.now())
    if key is None:
        out.append(check("ts.key", group, "Tailscale key", "ok",
                         "Key expiry is turned off for this machine - it will not be signed out.", value="never"))
    else:
        days, when = key
        if days < 0:
            status, summary = "fail", f"Expired on {when}. {KEY_FIX}"
        elif days < 3:
            status, summary = "fail", f"Expires {when}, in {days * 24:.0f} hours. {KEY_FIX}"
        elif days < 21:
            status, summary = "warn", f"Expires {when}, in {int(days)} days. {KEY_FIX}"
        else:
            status, summary = "ok", f"Expires {when}, in {int(days)} days."
        out.append(check("ts.key", group, "Tailscale key", status, summary, value=_days_value(days)))
    return out


def parse_serve(serve: dict) -> tuple:
    """({tailnet https port: (proxy target port or None, target text)}, [funnel ports])."""
    mappings = {}
    for hostport, web in ((serve.get("Web") or {}).items()):
        try:
            ts_port = int(str(hostport).rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        handlers = (web or {}).get("Handlers") or {}
        root = handlers.get("/") or next(iter(handlers.values()), {}) or {}
        target = root.get("Proxy") or root.get("Path") or root.get("Text") or "?"
        parsed = urlparse(target if "://" in target else "http://" + target)
        port = parsed.port if parsed.hostname in ("localhost", "127.0.0.1", "::1") else None
        mappings[ts_port] = (port, target)
    for port_text, tcp in ((serve.get("TCP") or {}).items()):
        try:
            p = int(port_text)
        except ValueError:
            continue
        if p not in mappings and isinstance(tcp, dict) and tcp.get("TCPForward"):
            fwd = tcp["TCPForward"]
            mappings[p] = (int(fwd.rsplit(":", 1)[1]) if ":" in fwd else None, "tcp " + fwd)
    funnel = []
    for hostport, on in ((serve.get("AllowFunnel") or {}).items()):
        if on:
            try:
                funnel.append(int(str(hostport).rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                funnel.append(str(hostport))
    return mappings, funnel


def serve_check(sys: System, installed: List[apps.App]) -> dict:
    rc, out, err = sys.run([config.TAILSCALE_EXE, "serve", "status", "--json"], timeout=15)
    serve = parse_json(out) if rc == 0 else None
    if serve is None and rc == 0 and not out.strip():
        serve = {}
    if not isinstance(serve, dict):
        return check("ts.serve", "tailscale", "Published ports", "unknown",
                     "Could not read `tailscale serve status`: " + (err or out).strip()[:160])

    mappings, funnel = parse_serve(serve)
    by_port = {a.port: a for a in apps.APPS + [apps.SELF]}
    problems, notes = [], []
    status = "ok"
    for p in funnel:
        status = "fail"
        problems.append(f"FUNNEL IS ON for :{p} - that publishes the app on the open internet, "
                        f"with no sign-in. Turn it off: tailscale funnel --https={p} off")
    wanted = installed + [apps.SELF]
    right = 0
    for app in wanted:
        got = mappings.get(app.ts_port)
        if got is not None and got[0] == app.port:
            right += 1
        elif got is None:
            if app is apps.SELF:
                notes.append(f"This monitor is not published on :{app.ts_port} - phones can't open it.")
                status = "warn" if status == "ok" else status
            else:
                status = "fail"
                problems.append(f"{app.name} is not published: nothing on :{app.ts_port}. "
                                f"Run: tailscale serve --bg --https={app.ts_port} http://localhost:{app.port}")
        else:
            other = by_port.get(got[0])
            status = "fail"
            problems.append(f":{app.ts_port} should be {app.name} (localhost:{app.port}) but goes to "
                            f"{got[1]}{' (' + other.name + ')' if other else ''}. "
                            f"Run: tailscale serve --bg --https={app.ts_port} http://localhost:{app.port}")
    expected = {a.ts_port for a in wanted}
    for ts_port, (port, target) in sorted(mappings.items()):
        if ts_port not in expected:
            notes.append(f":{ts_port} -> {target} is published but is not one of the farm apps.")
    table = [f":{p} -> {t}" for p, (_port, t) in sorted(mappings.items())]
    if status == "fail":
        summary = problems[0]
    elif notes:
        status, summary = "warn", notes[0]
    else:
        summary = f"All {len(wanted)} apps are published where the phones expect them."
    return check("ts.serve", "tailscale", "Published ports", status, summary,
                 problems + notes + ["Live mapping:"] + (table or ["(nothing published)"]),
                 value="FUNNEL" if funnel else f"{right}/{len(wanted)}")


def cert_check(sys: System, dnsname: str, port: int) -> dict:
    expiry, err = sys.tls_expiry(dnsname, port)
    if expiry is None:
        return check("ts.cert", "tailscale", "HTTPS certificate", "warn",
                     f"Could not check the certificate on :{port}: {err}")
    days = (expiry - sys.now()).total_seconds() / 86400
    when = expiry.strftime("%Y-%m-%d")
    value = _days_value(days)
    # Tailscale renews these on its own about a month ahead. One that gets
    # within a week of expiring means renewal is failing - usually because
    # the PC cannot reach Let's Encrypt - and the day it lapses every phone
    # refuses every app with a certificate error.
    if days < 0:
        return check("ts.cert", "tailscale", "HTTPS certificate", "fail",
                     f"Expired on {when} - phones will refuse every app.", value=value)
    if days < 7:
        return check("ts.cert", "tailscale", "HTTPS certificate", "warn",
                     f"Expires {when}, in {int(days)} days, and Tailscale has not renewed it.",
                     value=value)
    return check("ts.cert", "tailscale", "HTTPS certificate", "ok",
                 f"Valid until {when} ({int(days)} days).", value=value)


# --- the PC ------------------------------------------------------------------

def pc_checks(sys: System) -> List[dict]:
    out = []
    path = config.BOORD_DIR if os.path.isdir(config.BOORD_DIR) else config.DATA_DIR
    try:
        total, free = sys.disk(path)
        pct = free * 100 / total if total else 0
        drive = os.path.splitdrive(os.path.abspath(path))[0] or "/"
        # `pct` is what the dashboard draws as a bar: how full, not how free.
        meter = {"pct": round(100 - pct), "value": f"{free / 1024 ** 3:.0f} GB free"}
        summary = f"{_gb(free)} free of {_gb(total)} on {drive} ({pct:.0f}%)."
        # SQLite needs room to write its journal and the nightly backups need
        # room to land. A full disk does not crash the apps politely: writes
        # start failing mid-request and the scans at the door stop syncing.
        if free < 2 * 1024 ** 3:
            status, summary = "fail", "Almost full: " + summary
        elif free < 5 * 1024 ** 3 or pct < 10:
            status, summary = "warn", "Getting full: " + summary
        else:
            status = "ok"
        out.append(check("pc.disk", "pc", "Disk", status, summary, **meter))
    except OSError as e:
        out.append(check("pc.disk", "pc", "Disk", "unknown", str(e)))

    mem = sys.memory()
    if mem:
        total, avail = mem
        used = 100 - avail * 100 / total if total else 0
        status = "warn" if used > 92 else "ok"
        out.append(check("pc.memory", "pc", "Memory", status,
                         f"{used:.0f}% in use ({_gb(avail)} free of {_gb(total)}).",
                         pct=round(used), value=f"{used:.0f}% used"))

    up = sys.uptime_s()
    if up is not None:
        # A recent reboot is a warning, not a failure: it is either Windows
        # Update or a power cut, and either way the person should know the
        # apps restarted - a pack house that lost power at 05:00 wants to
        # know why the 05:00 scans are missing.
        status = "warn" if up < RECENT_BOOT_S else "ok"
        boot = sys.now() - dt.timedelta(seconds=up)
        out.append(check("pc.uptime", "pc", "Running since", status,
                         f"Up {_ago(up)} (booted {boot.strftime('%Y-%m-%d %H:%M')} UTC)."
                         + (" It restarted recently." if status == "warn" else ""), alert=False,
                         value=_short(up)))
    return out


# --- Windows Update -------------------------------------------------------------

def update_check(sys: System) -> Optional[dict]:
    """Whether Windows is waiting to restart the PC to finish installing
    updates. Windows restarts by itself outside its "active hours", and on a
    farm PC those are often wrong for the pack house: a restart at 15:00 is
    five minutes of every app gone in the middle of a shift, with the
    watchdog standing down for the first five minutes after boot. The point
    of this tile is to know beforehand and choose the moment.

    A warning, never an alert: it is not broken, and the morning summary
    carries it to the phone once a day."""
    info = sys.windows_update()
    if info is None:
        return None
    hours = info.get("active_hours")
    active = f"{hours[0]:02d}:00-{hours[1]:02d}:00" if hours else None
    if not info.get("reboot_pending"):
        return check("guards.update", "guards", "Windows Update", "ok",
                     "No restart waiting." + (f" Active hours: {active}." if active else ""),
                     alert=False, value="ok")
    when = _parse_iso(info.get("scheduled"))
    summary = "Windows has installed updates and is waiting to restart this PC."
    if when:
        local = when.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        summary += f" It plans to restart at {local.strftime('%Y-%m-%d %H:%M')} (this PC's time)."
    summary += (" Restart it yourself at a quiet time - every app is down for a few minutes - "
                "or it will restart on its own outside its active hours"
                + (f" ({active})" if active else "")
                + ". Set active hours to cover the pack house in Settings > Windows Update > "
                  "Advanced options.")
    detail = [f"Waiting for: {', '.join(info.get('reasons') or [])}"]
    if active:
        detail.append(f"Active hours: {active}")
    return check("guards.update", "guards", "Windows Update", "warn", summary, detail,
                 alert=False, value="restart")


# --- the watchdog's own record ----------------------------------------------

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})  (.*)$")


def watchdog_log_check(sys: System) -> dict:
    """What the watchdog has done in the last day, from Boord's
    data\\watchdog.log. Its timestamps are the PC's local time (it writes
    Get-Date), so they are compared with local time here."""
    path = os.path.join(config.BOORD_DIR, "data", "watchdog.log")
    text = sys.read_tail(path)
    if text is None:
        return check("guards.log", "guards", "Watchdog log", "unknown",
                     "No data\\watchdog.log in Boord's folder yet.", alert=False)
    now_local = dt.datetime.now()
    recent, restarts, trouble = [], [], []
    for line in text.splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        try:
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        recent.append(line)
        if (now_local - when).total_seconds() > 86400:
            continue
        msg = m.group(2)
        if msg.startswith("Restarting "):
            restarts.append(msg[len("Restarting "):].rstrip("."))
        if "REFUSING" in msg or "FAILED" in msg or "did not answer" in msg:
            trouble.append(line)
    tail = recent[-15:]
    value = f"{len(restarts)}x"
    if trouble:
        return check("guards.log", "guards", "Watchdog log", "warn",
                     f"The watchdog could not fix something in the last 24 h: {trouble[-1][21:]}",
                     tail, alert=False, value=value)
    if restarts:
        counts = {}
        for name in restarts:
            counts[name] = counts.get(name, 0) + 1
        said = ", ".join(f"{n} x{c}" if c > 1 else n for n, c in counts.items())
        return check("guards.log", "guards", "Watchdog log", "ok",
                     f"Restarted in the last 24 h: {said}.", tail, alert=False, value=value)
    return check("guards.log", "guards", "Watchdog log", "ok",
                 "No restarts in the last 24 h.", tail, alert=False, value=value)


# --- Google Drive backups ----------------------------------------------------

DRIVE_EXE = "GoogleDriveFS.exe"


def _config_lines(path: str) -> Optional[List[List[str]]]:
    """The `|`-separated, stripped fields of each line of one of the
    hand-edited data/*.txt files, skipping blanks and # comments; None when
    the file isn't there. utf-8-sig because Notepad on the farm PC may save
    with a BOM."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    return [[f.strip() for f in line.split("|")] for line in (raw.strip() for raw in lines)
            if line and not line.startswith("#")]


def read_drive_folders() -> List[tuple]:
    """[(folder, max days)] from data/drive_backups.txt; [] when it isn't
    there. A malformed day count falls back to the default rather than
    dropping the line - a folder silently left unwatched is the one failure
    this file must not have."""
    out = []
    for fields in _config_lines(config.DRIVE_BACKUPS_FILE) or []:
        days = fields[1] if len(fields) > 1 else ""
        try:
            limit = float(days) if days else config.DRIVE_DEFAULT_DAYS
        except ValueError:
            limit = config.DRIVE_DEFAULT_DAYS
        out.append((fields[0].strip('"'), limit))
    return out


def drive_checks(sys: System) -> List[dict]:
    """Two questions: is Google Drive for Desktop running to upload anything
    at all, and has a fresh backup reached each folder it uploads?

    Drive for Desktop is not a service. It runs only while someone is logged
    in to Windows, so a PC that rebooted for an update at 03:00 and sat at the
    login screen keeps writing backups that nothing uploads - every folder
    check stays green while nothing leaves the farm. That is why the process
    is checked on its own, and why it alerts.
    """
    folders = read_drive_folders()
    if not folders:
        return []
    out = []
    running = sys.process_running(DRIVE_EXE)
    if running is None:
        out.append(check("drive.app", "backups", "Google Drive", "unknown",
                         "Whether Google Drive is running can only be checked on the farm PC."))
    elif running:
        out.append(check("drive.app", "backups", "Google Drive", "ok", "Running, so it can upload."))
    else:
        up = sys.uptime_s()
        why = ("Not running, so nothing is being uploaded. It only runs while someone is logged in "
               "to Windows: log in on the farm PC, and start Google Drive if it doesn't start itself.")
        # Grace after a boot: whoever restarted the PC is usually about to
        # log in, and paging them for the minutes in between teaches them
        # to ignore this. The tile still says what is true - Drive is not
        # running - but the alert waits, through the same `alert` flag that
        # keeps every other check quiet.
        grace = up is not None and up < RECENT_BOOT_S
        out.append(check("drive.app", "backups", "Google Drive", "fail", why, alert=not grace))

    for i, (folder, limit) in enumerate(folders):
        # ntpath, not os.path: these are Windows paths, and the tests (and
        # anyone trying this on a Mac) run where os.path can't split them.
        name = ntpath.basename(folder.rstrip("\\/")) or folder
        cid = f"drive.folder.{i}"
        try:
            newest = sys.newest_file(folder)
        except OSError:
            hint = ""
            drive = ntpath.splitdrive(folder)[0].upper()
            if drive and drive != "C:":
                hint = (f" If {drive} is Google Drive's own drive, the monitor can't see it: Drive "
                        "mounts it only for the logged-in user, and the monitor runs as SYSTEM. "
                        "Name the folder on C: that Drive uploads from instead.")
            out.append(check(cid, "backups", name, "warn",
                             f"Can't open {folder}.{hint}", alert=False))
            continue
        if newest is None:
            out.append(check(cid, "backups", name, "warn", f"{folder} has no files in it yet."))
            continue
        path, when = newest
        age = (sys.now() - when).total_seconds()
        file_name = ntpath.basename(path)
        if age > limit * 86400:
            status = "fail"
            summary = (f"The newest backup is {_ago(age)} old ({file_name}). A new one is "
                       f"expected at least every {limit:g} days.")
        else:
            status, summary = "ok", f"Newest backup {_ago(age)} ago: {file_name}."
        out.append(check(cid, "backups", name, status, summary, [folder], value=_short(age)))
    return out


# --- devices on the tailnet --------------------------------------------------

def read_device_list() -> tuple:
    """([(tailscale name, label, boord id)] or None, {hidden names}) from
    data/devices.txt.

    A line starting with "-" hides that device: an old tablet still on the
    tailnet, or someone's laptop nobody needs a tile for. The list is None
    when the file names no devices to show - absent, or only hides - and
    then every device on the tailnet is shown but the hidden ones. Without
    that, hiding one retired tablet would mean listing every other device by
    hand."""
    shown, hidden = [], set()
    for f in _config_lines(config.DEVICES_FILE) or []:
        if f[0].startswith("-"):
            name = f[0][1:].strip().lower()
            if name:
                hidden.add(name)
            continue
        p = f + ["", ""]
        if p[0]:
            shown.append((p[0], p[1] or p[0], p[2]))
    return (shown or None), hidden


# "device-01", "Device 01" and "device01" are all Boord slot 01: tablets get
# named by hand in Tailscale, so the separator is not reliable.
_BOORD_ID = re.compile(r"^device[\s_-]*(\d+)$", re.IGNORECASE)


def _tile_name(name: str) -> str:
    """The name a tile shows for a tailnet device when devices.txt gives none.
    Boord slots read "Dev 01" like their siblings, the farm PC's own tailnet
    name is the unhelpful "localhost", and a MacBook's is a long
    "macbook-pro-of-..." that the tile would cut to nothing anyway."""
    low = name.strip().lower()
    if low == "localhost":
        return "Farm PC"
    if low.startswith("macbook"):
        return "Mac"
    return _BOORD_ID.sub(r"Dev \1", name.strip())


def _device_order(name: str) -> tuple:
    """Boord's tablets first, in slot order (Dev 01 before Dev 02, whatever
    the separator in their Tailscale name did to a plain alphabetical sort),
    then everything else by name."""
    m = _BOORD_ID.match(name.strip())
    return (0, int(m.group(1)), "") if m else (1, 0, name.lower())


def _peer_names(peer: dict) -> set:
    """Every name a person might write for a peer: its host name and the
    first label of its MagicDNS name, which differ when two machines share a
    host name and Tailscale appends -1."""
    names = {str(peer.get("HostName") or "").lower()}
    dns = str(peer.get("DNSName") or "")
    if dns:
        names.add(dns.split(".")[0].lower())
    names.discard("")
    return names


def device_checks(sys: System, status: Optional[dict]) -> List[dict]:
    """One tile per phone, tablet and computer on the tailnet: is it online
    now, or when was it last seen, and is its Tailscale key about to lapse.
    For Boord's own devices, also when each last sent a crate.

    None of this ever pages anyone. Tablets are switched off at night and lose
    signal in the orchard, so "offline" is the normal state for half the day
    - it shows grey, not amber, and the banner ignores it. The one thing
    worth a warning is a key about to expire: that device drops off the
    tailnet, and every app with it, on a date that can be seen coming."""
    if not status or status.get("BackendState") != "Running":
        return []
    listed, hidden = read_device_list()
    peers = [p for p in (status.get("Peer") or {}).values()
             if isinstance(p, dict) and not p.get("ShareeNode") and not (_peer_names(p) & hidden)]
    boord = sys.boord_devices(config.BOORD_DB)

    if listed is None:
        wanted = [(n, _tile_name(n), "")
                  for n in sorted({p.get("HostName") or "?" for p in peers}, key=_device_order)]
    else:
        wanted = [w for w in listed if w[0].lower() not in hidden]

    # First peer wins a shared name, as the old next(...) scan did.
    by_name = {}
    for p in peers:
        for n in _peer_names(p):
            by_name.setdefault(n, p)

    out = []
    for name, label, boord_id in wanted:
        key = name.lower()
        peer = by_name.get(key)
        cid = "dev." + key
        # Boord's own ids (device-01) are cut to "Dev 01" on the tile: there
        # is room for about six letters, and "device-..." nine times over says
        # nothing. Any other name can be set in data/devices.txt.
        short = _BOORD_ID.sub(r"Dev \1", label)
        if peer is None:
            out.append(check(cid, "devices", label, "warn",
                             f"No device called '{name}' on the tailnet. Renamed, or removed from "
                             "Tailscale? Check data\\devices.txt against the Tailscale admin console.",
                             alert=False, value="?", short=short))
            continue

        detail = []
        os_name = peer.get("OS") or "?"
        ips = ", ".join(peer.get("TailscaleIPs") or [])
        detail.append(f"Tailscale: {peer.get('HostName')} ({os_name}) {ips}")
        if peer.get("Online"):
            status_, value, summary = "ok", "on", "Online now."
        else:
            seen = _parse_iso(peer.get("LastSeen"))
            if seen and seen.year > 2000:
                age = (sys.now() - seen).total_seconds()
                status_, value = "off", _short(age)
                summary = f"Offline - last seen {_ago(age)} ago."
            else:
                status_, value, summary = "off", "off", "Offline."

        # A device's own key expiry, read as for the farm PC's
        # (tailscale_checks) but only ever a warning: devices never page.
        expiry = _key_expiry(peer.get("KeyExpiry"), sys.now())
        if expiry and expiry[0] < 14:
            days, when = expiry
            status_ = "warn"
            summary = (f"Its Tailscale key {'expired on' if days < 0 else 'expires'} {when}: "
                       f"it will drop off the tailnet. {KEY_FIX} " + summary)

        # Boord: by the id given in devices.txt, or by the Tailscale name when
        # the tablet was simply named after its Boord slot (device-03).
        m = _BOORD_ID.match(name.strip())
        slot = f"device-{m.group(1)}" if m else key
        bid = boord_id or (slot if boord and slot in boord else "")
        info = (boord or {}).get(bid) if bid else None
        if info:
            detail.append(f"Boord: {bid}" + (f" - {info['station']}" if info["station"] else "")
                          + f" ({info['role']})")
            # SQLite's "2026-10-18 07:20:00" is ISO enough for fromisoformat.
            sync = _parse_iso(info["last_sync"])
            opened = _parse_iso(info["last_seen"])
            if sync:
                age = (sys.now() - sync).total_seconds()
                detail.append(f"Last crate received: {_ago(age)} ago ({sync.strftime('%Y-%m-%d %H:%M')} UTC)")
                summary += f" Last crate received {_ago(age)} ago."
            elif info["role"] == "field":
                detail.append("Last crate received: never")
            if opened:
                detail.append(f"Boord last opened: {_ago((sys.now() - opened).total_seconds())} ago")
            # What the tablet last said it is holding unsent (Boord 3.17+).
            pending = info.get("queue_pending")
            reported = _parse_iso(info.get("queue_reported_at"))
            if pending and reported:
                said = _ago((sys.now() - reported).total_seconds())
                oldest = _parse_iso(info.get("queue_oldest"))
                what = f"{pending} waiting to sync on the tablet"
                if oldest:
                    held = (sys.now() - oldest).total_seconds()
                    what += f", the oldest picked {_ago(held)} ago"
                    if held > QUEUE_WARN_S:
                        # A warning even when the tablet is offline - that is
                        # the case that matters: those crates exist only on
                        # it, and a dead battery or a fall loses them.
                        status_ = "warn"
                        what += (" - they exist only on that tablet until it syncs. Bring it "
                                 "into signal, or check its sync status on its screen")
                summary += f" {what} (as of {said} ago)."
                value = f"{pending} wait"
                detail.append(f"Waiting to sync: {pending} (reported {said} ago)")
            elif pending == 0 and reported:
                detail.append(f"Waiting to sync: nothing (reported "
                              f"{_ago((sys.now() - reported).total_seconds())} ago)")
        elif bid:
            detail.append(f"Boord: no device '{bid}' in Boord's list")

        # The sheet's word for grey here is "Offline", not "Not installed".
        extra = {"word": "Offline"} if status_ == "off" else {}
        out.append(check(cid, "devices", label, status_, summary, detail, alert=False,
                         value=value, short=short, online=bool(peer.get("Online")),
                         queue=(info or {}).get("queue_pending") or 0, **extra))
    return out


# --- the whole picture, per role --------------------------------------------

def farm_checks(sys: System) -> List[dict]:
    names = [a.task for a in apps.APPS] + [apps.WATCHDOG_TASK, apps.HEARTBEAT_TASK]
    tasks = query_tasks(sys, names)
    status, err = tailscale_status(sys)
    dnsname = dns_name(status) if status and status.get("BackendState") == "Running" else None

    out = []
    installed = []
    for app in apps.APPS:
        is_installed = tasks is None or app.task in tasks
        c = app_check(sys, app, is_installed, dnsname)
        if c["status"] != "off":
            installed.append(app)
        out.append(c)

    out.extend(tailscale_checks(sys, status, err))
    if dnsname:
        out.append(serve_check(sys, installed))
        first = installed[0].ts_port if installed else apps.SELF.ts_port
        out.append(cert_check(sys, dnsname, first))
    out.extend(pc_checks(sys))
    out.append(_task_check(sys, tasks, apps.WATCHDOG_TASK, "guards.watchdog", "Watchdog", 5,
                           "Nothing restarts an app that crashes - run Boord's setup_watchdog.bat."))
    out.append(_task_check(sys, tasks, apps.HEARTBEAT_TASK, "guards.heartbeat", "Heartbeat", 10,
                           "No email reaches anyone when this PC is off - run Boord's setup_heartbeat.bat."))
    out.append(watchdog_log_check(sys))
    update = update_check(sys)
    if update:
        out.append(update)
    out.extend(drive_checks(sys))
    out.extend(device_checks(sys, status))
    return out


def remote_checks(sys: System, last_farm: Optional[dict]) -> tuple:
    """(checks, the farm's own status if it answered). Seen from outside, the
    way a phone sees the farm."""
    out = []
    status, err = tailscale_status(sys)
    mine = tailscale_checks(sys, status, err, group="watcher", who="this watcher")
    for c in mine:
        c["id"] = "watcher." + c["id"]
        c["label"] = c["label"].replace("Tailscale", "This watcher's Tailscale")
        # The watcher's own problems never wake a phone: if its Tailscale is
        # down it cannot see the farm, and paging the farmer about the
        # owner's laptop being asleep is exactly the false alarm that teaches
        # people to mute this.
        c["alert"] = False
    out.extend(mine)
    watcher_ok = bool(status) and status.get("BackendState") == "Running"

    if not config.FARM_URL:
        out.append(check("farm.monitor", "farm", "Farm PC", "unknown",
                         "MONITOR_FARM_URL is not set, so there is nothing to watch.", alert=False))
        return out, None

    host = urlparse(config.FARM_URL).hostname
    farm = sys.http(config.FARM_URL + "/api/status", timeout=15, want_json=True)
    farm_status = farm.body if isinstance(farm.body, dict) else None

    # Which apps to probe: the ones the farm said it has, last time it said
    # anything. Probing an app this farm never installed would report it
    # down every minute forever.
    known = (farm_status or last_farm or {}).get("checks") or []
    present = {c.get("app") for c in known if c.get("app") and c.get("status") != "off"}
    to_probe = [a for a in apps.APPS if not present or a.key in present]
    answering = 0
    for app in to_probe:
        p = sys.http(_url(host, app.ts_port, app.health), timeout=10)
        answering += 1 if p.answered else 0
        out.append(check("remote." + app.key, "farm", app.name,
                         "ok" if p.answered else "fail",
                         (f"Reachable over Tailscale ({p.ms} ms)." if p.answered
                          else f"Not reachable over Tailscale ({p.error})."),
                         alert=False, app=app.key, url=_url(host, app.ts_port, app.path),
                         value=f"{p.ms}ms" if p.answered else "down"))

    if farm_status is not None:
        checked = _parse_iso(farm_status.get("checked_at"))
        age = (sys.now() - checked).total_seconds() if checked else None
        if age is not None and age > max(300, config.CHECK_INTERVAL_S * 5):
            c = check("farm.monitor", "farm", "Farm PC", "warn",
                      f"The farm monitor answers, but its last check was {_ago(age)} ago - its checks are stuck.")
        else:
            c = check("farm.monitor", "farm", "Farm PC", "ok", "The farm monitor is answering.")
    elif not watcher_ok:
        c = check("farm.monitor", "farm", "Farm PC", "unknown",
                  "Can't tell - this watcher is not on the tailnet itself.", alert=False)
    elif answering:
        c = check("farm.monitor", "farm", "Farm PC", "fail",
                  f"The monitor on the farm PC is not answering ({farm.error or farm.status}), but "
                  f"{answering} of {len(to_probe)} apps are - the PC is on and only the monitor has stopped.")
    else:
        c = check("farm.monitor", "farm", "Farm PC", "fail",
                  f"Nothing on the farm PC answers over Tailscale ({farm.error or farm.status}). "
                  "It is off, asleep, has lost its internet, or Tailscale has stopped on it.")
    out.insert(len(mine), c)
    return out, farm_status
