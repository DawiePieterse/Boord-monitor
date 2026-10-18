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


def check(id: str, group: str, label: str, status: str, summary: str,
          detail: Optional[List[str]] = None, alert: bool = True, **data) -> dict:
    out = {"id": id, "group": group, "label": label, "status": status,
           "summary": summary, "detail": detail or [], "alert": alert}
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
                     "Not installed on this PC.", value="off", **data)
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
    expiry = _parse_iso(me.get("KeyExpiry"))
    if expiry is None or expiry.year < 2000:
        out.append(check("ts.key", group, "Tailscale key", "ok",
                         "Key expiry is turned off for this machine - it will not be signed out.", value="never"))
    else:
        days = (expiry - sys.now()).total_seconds() / 86400
        when = expiry.strftime("%Y-%m-%d")
        key_value = f"{max(0, int(days))}d"
        fix = ("In the Tailscale admin console, open this machine's menu and choose "
               "'Disable key expiry'.")
        if days < 0:
            out.append(check("ts.key", group, "Tailscale key", "fail", f"Expired on {when}. {fix}", value="0d"))
        elif days < 3:
            out.append(check("ts.key", group, "Tailscale key", "fail",
                             f"Expires {when}, in {days * 24:.0f} hours. {fix}", value=f"{days * 24:.0f}h"))
        elif days < 21:
            out.append(check("ts.key", group, "Tailscale key", "warn",
                             f"Expires {when}, in {int(days)} days. {fix}", value=key_value))
        else:
            out.append(check("ts.key", group, "Tailscale key", "ok",
                             f"Expires {when}, in {int(days)} days.", value=key_value))
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
    for app in installed + [apps.SELF]:
        got = mappings.get(app.ts_port)
        if got is None:
            if app is apps.SELF:
                notes.append(f"This monitor is not published on :{app.ts_port} - phones can't open it.")
                status = "warn" if status == "ok" else status
            else:
                status = "fail"
                problems.append(f"{app.name} is not published: nothing on :{app.ts_port}. "
                                f"Run: tailscale serve --bg --https={app.ts_port} http://localhost:{app.port}")
        elif got[0] != app.port:
            other = by_port.get(got[0])
            status = "fail"
            problems.append(f":{app.ts_port} should be {app.name} (localhost:{app.port}) but goes to "
                            f"{got[1]}{' (' + other.name + ')' if other else ''}. "
                            f"Run: tailscale serve --bg --https={app.ts_port} http://localhost:{app.port}")
    expected = {a.ts_port for a in installed + [apps.SELF]}
    for ts_port, (port, target) in sorted(mappings.items()):
        if ts_port not in expected:
            notes.append(f":{ts_port} -> {target} is published but is not one of the farm apps.")
    table = [f":{p} -> {t}" for p, (_port, t) in sorted(mappings.items())]
    wanted = installed + [apps.SELF]
    right = sum(1 for a in wanted if (mappings.get(a.ts_port) or (None,))[0] == a.port)
    if status == "fail":
        summary = problems[0]
    elif notes:
        status, summary = "warn", notes[0]
    else:
        summary = f"All {len(installed) + 1} apps are published where the phones expect them."
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
    # Tailscale renews these on its own about a month ahead. One that gets
    # within a week of expiring means renewal is failing - usually because
    # the PC cannot reach Let's Encrypt - and the day it lapses every phone
    # refuses every app with a certificate error.
    if days < 0:
        return check("ts.cert", "tailscale", "HTTPS certificate", "fail",
                     f"Expired on {when} - phones will refuse every app.", value="0d")
    if days < 7:
        return check("ts.cert", "tailscale", "HTTPS certificate", "warn",
                     f"Expires {when}, in {int(days)} days, and Tailscale has not renewed it.",
                     value=f"{int(days)}d")
    return check("ts.cert", "tailscale", "HTTPS certificate", "ok",
                 f"Valid until {when} ({int(days)} days).", value=f"{int(days)}d")


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
            out.append(check("pc.disk", "pc", "Disk", "fail", "Almost full: " + summary, **meter))
        elif free < 5 * 1024 ** 3 or pct < 10:
            out.append(check("pc.disk", "pc", "Disk", "warn", "Getting full: " + summary, **meter))
        else:
            out.append(check("pc.disk", "pc", "Disk", "ok", summary, **meter))
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
        status = "warn" if up < 30 * 60 else "ok"
        boot = sys.now() - dt.timedelta(seconds=up)
        out.append(check("pc.uptime", "pc", "Running since", status,
                         f"Up {_ago(up)} (booted {boot.strftime('%Y-%m-%d %H:%M')} UTC)."
                         + (" It restarted recently." if status == "warn" else ""), alert=False,
                         value=_short(up)))
    return out


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
    if trouble:
        return check("guards.log", "guards", "Watchdog log", "warn",
                     f"The watchdog could not fix something in the last 24 h: {trouble[-1][21:]}",
                     tail, alert=False, value=f"{len(restarts)}x")
    if restarts:
        counts = {}
        for name in restarts:
            counts[name] = counts.get(name, 0) + 1
        said = ", ".join(f"{n} x{c}" if c > 1 else n for n, c in counts.items())
        return check("guards.log", "guards", "Watchdog log", "ok",
                     f"Restarted in the last 24 h: {said}.", tail, alert=False, value=f"{len(restarts)}x")
    return check("guards.log", "guards", "Watchdog log", "ok",
                 "No restarts in the last 24 h.", tail, alert=False, value="0x")


# --- Google Drive backups ----------------------------------------------------

DRIVE_EXE = "GoogleDriveFS.exe"


def read_drive_folders() -> List[tuple]:
    """[(folder, max days)] from data/drive_backups.txt; [] when it isn't
    there. A malformed day count falls back to the default rather than
    dropping the line - a folder silently left unwatched is the one failure
    this file must not have."""
    try:
        with open(config.DRIVE_BACKUPS_FILE, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        folder, _, days = line.partition("|")
        try:
            limit = float(days.strip()) if days.strip() else config.DRIVE_DEFAULT_DAYS
        except ValueError:
            limit = config.DRIVE_DEFAULT_DAYS
        out.append((folder.strip().strip('"'), limit))
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
        # to ignore this.
        status = "warn" if up is not None and up < 30 * 60 else "fail"
        out.append(check("drive.app", "backups", "Google Drive", status, why))

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
            out.append(check(cid, "backups", name, "fail",
                             f"The newest backup is {_ago(age)} old ({file_name}). A new one is "
                             f"expected at least every {limit:g} days.", [folder], value=_short(age)))
        else:
            out.append(check(cid, "backups", name, "ok",
                             f"Newest backup {_ago(age)} ago: {file_name}.", [folder], value=_short(age)))
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
    out.extend(drive_checks(sys))
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
