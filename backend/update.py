"""The update button: install an app's newest signed release on the farm PC.

The last step of every release used to be a person at the farm PC (or on
AnyDesk) double-clicking that app's update_*.bat. This does it from here: the
dashboard's Update button (behind the PIN), and the owner's Mac at the end of
scripts/deploy.sh (behind the remote token, which that Mac's off-site watcher
already holds).

It never runs the updater itself, for two reasons:
  - The updater stops the server it updates. Run as a child of this
    process, updating Boord Monitor would stop the very process waiting on
    it, halfway through a checkout.
  - It needs administrator rights. This server runs as SYSTEM, but a task of
    its own also outlives any restart of this one.
So it writes which app to update to data/update_request.json and starts the
"Boord Update" scheduled task, which runs this repo's watchdog.ps1 -Update as
SYSTEM. The watchdog finds that app's folder and updater from its scheduled
task and apps.json, stands down if another update or an install is running,
runs the updater with --unattended (no pauses, no UAC prompt), and writes
data/update_status.json, which current() reads back. The task is registered
on first use, so a farm needs no install.bat run for it.

What can be installed is still only what the updater accepts: the newest tag
signed by the release key in that app's data\\release_key.fpr. So the token
that starts one can at worst install the release the owner signed last.

One update at a time.
"""
import datetime as dt
import json
import os
import subprocess
import threading
from typing import Optional

import apps
import config

TASK = "Boord Update"
REQUEST_FILE = os.path.join(config.DATA_DIR, "update_request.json")
STATUS_FILE = os.path.join(config.DATA_DIR, "update_status.json")
# A request the task never picked up (the task could not start, the PC
# rebooted) must not block the button for ever.
STALE_QUEUED_S = 120
# The updaters reinstall dependencies and migrate; ten minutes is far past a
# real one, and the watchdog gives up on the updater at that point too.
STALE_RUNNING_S = 20 * 60
_lock = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def _age_s(stamp: Optional[str]) -> float:
    try:
        return (dt.datetime.utcnow() - dt.datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def current() -> Optional[dict]:
    """The last update asked for, as the watchdog last wrote it. PowerShell
    5.1 writes UTF-8 with a BOM, hence utf-8-sig."""
    try:
        with open(STATUS_FILE, encoding="utf-8-sig") as fh:
            job = json.load(fh)
    except (OSError, ValueError):
        return None
    # Monitor 2.2's watchdog wrote each line as Windows PowerShell's string
    # object ({"value": ..., "PSPath": ...}); keep reading those as the text.
    job["output"] = [ln.get("value", "") if isinstance(ln, dict) else str(ln) for ln in (job.get("output") or [])]
    if job.get("state") == "queued" and _age_s(job.get("requested")) > STALE_QUEUED_S:
        job["state"] = "failed"
        job.setdefault("output", []).append(
            f"The '{TASK}' task did not pick this up within {STALE_QUEUED_S} seconds.")
    elif job.get("state") == "running" and _age_s(job.get("started")) > STALE_RUNNING_S:
        job["state"] = "failed"
        job.setdefault("output", []).append("Still not finished after 20 minutes - see data\\update.log on the farm PC.")
    return job


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _start_task() -> None:
    """(Re-)register the task, pointing at this folder's watchdog.ps1, and
    start it. Registered every time, so a monitor moved to another folder
    never starts a stale copy. No trigger: it only ever runs when asked."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        + _ps_quote(f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{config.WATCHDOG_PS1}" -Update')
        + "; $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30); "
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK)} -Action $a -Principal $p -Settings $s -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName {_ps_quote(TASK)}"
    )
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, timeout=60, creationflags=_NO_WINDOW)
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"Could not start the '{TASK}' task: {err[-1] if err else 'exit ' + str(p.returncode)}")


def start(app_key: str, by: str = "") -> dict:
    """Raises ValueError with a sentence for the person if it can't start."""
    app = apps.BY_KEY.get(app_key)
    if app is None:
        raise ValueError("There is no app by that name")
    if config.ROLE != "farm":
        raise ValueError("Updates can only be done by the monitor on the farm PC")
    if not app.updater:
        raise ValueError(f"{app.name} has no updater in apps.json")
    if not os.path.exists(config.WATCHDOG_PS1):
        raise ValueError(f"watchdog.ps1 is not at {config.WATCHDOG_PS1}")
    with _lock:
        job = current()
        if job and job.get("state") in ("queued", "running"):
            raise ValueError(f"{job.get('name', 'An app')} is still updating - wait for it to finish")
        job = {"app": app.key, "name": app.name, "state": "queued", "by": by, "requested": _now(),
               "started": None, "finished": None, "tag": None, "output": []}
        _write(REQUEST_FILE, {"app": app.name, "requested": job["requested"]})
        _write(STATUS_FILE, job)
        try:
            _start_task()
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            job.update(state="failed", finished=_now(), output=[str(e)])
            _write(STATUS_FILE, job)
            raise ValueError(str(e))
    return job


# --- the nightly update --------------------------------------------------------
# Every night (00:00 unless data/nightly_update.txt says another HH:MM, or
# "off") the "Boord Nightly Update" task runs watchdog.ps1 -Nightly: each
# installed app with a newer signed release on GitHub is updated, this monitor
# last. Most nights there is nothing new and nothing restarts: the watchdog
# compares tags first, because every updater restarts its app even when it
# finds nothing to install. The monitor registers the task itself when it
# starts, so turning it on, moving it or turning it off needs only the file and
# a restart of the monitor (its Restart button), never install.bat.
NIGHTLY_TASK = "Boord Nightly Update"
NIGHTLY_FILE = os.path.join(config.DATA_DIR, "nightly_update.txt")
NIGHTLY_RESULT = os.path.join(config.DATA_DIR, "update_nightly.json")
NIGHTLY_DEFAULT = "00:00"


def parse_nightly(text: Optional[str]) -> Optional[str]:
    """"HH:MM" from data/nightly_update.txt's text, or None when it says
    off. No file is the default; a line that isn't a time is ignored rather
    than guessed at, so a typo leaves the default too."""
    if text is None:
        return NIGHTLY_DEFAULT
    lines = [ln.strip().lstrip("\ufeff") for ln in text.splitlines()]
    for ln in [ln for ln in lines if ln and not ln.startswith("#")]:
        if ln.lower() == "off":
            return None
        try:
            return dt.datetime.strptime(ln, "%H:%M").strftime("%H:%M")
        except ValueError:
            continue
    return NIGHTLY_DEFAULT


def nightly_at() -> Optional[str]:
    try:
        with open(NIGHTLY_FILE, encoding="utf-8-sig") as fh:
            return parse_nightly(fh.read())
    except OSError:
        return NIGHTLY_DEFAULT


def _nightly_script(at: Optional[str]) -> str:
    if at is None:
        return (f"Unregister-ScheduledTask -TaskName {_ps_quote(NIGHTLY_TASK)} -Confirm:$false "
                "-ErrorAction SilentlyContinue")
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        + _ps_quote(f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{config.WATCHDOG_PS1}" -Nightly')
        + f"; $t = New-ScheduledTaskTrigger -Daily -At {_ps_quote(at)}; "
        "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        # A PC that was off at midnight catches up when it starts, rather
        # than waiting a whole day.
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 3); "
        f"Register-ScheduledTask -TaskName {_ps_quote(NIGHTLY_TASK)} -Action $a -Trigger $t -Principal $p "
        "-Settings $s -Force | Out-Null"
    )


def ensure_nightly() -> None:
    """Called once at start-up on the farm PC, in a thread: registering a
    task takes a few seconds of PowerShell. A failure is logged, and the
    dashboard's Nightly update tile says the task is missing."""
    import logging
    if config.ROLE != "farm" or os.name != "nt":
        return
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", _nightly_script(nightly_at())],
                           capture_output=True, timeout=120, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            logging.getLogger("update").warning("could not register %s: %s", NIGHTLY_TASK,
                                                (p.stderr or p.stdout).decode("utf-8", "replace").strip()[-300:])
    except (OSError, subprocess.TimeoutExpired) as e:
        logging.getLogger("update").warning("could not register %s: %s", NIGHTLY_TASK, e)

