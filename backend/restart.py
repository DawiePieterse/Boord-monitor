"""The restart button.

It does not restart anything itself. It runs Boord's own watchdog.ps1 with
`-Restart "<app>"`, which goes through exactly the steps the watchdog's
automatic restarts do:
  - stands down while update_server.bat, an installer or Alembic is running
    (a server started on top of a half-migrated database is worse than one
    that is down);
  - stops the old uvicorn with stop_server.ps1 and proves the port came free;
  - refuses to kill a process it cannot prove belongs to that app;
  - waits for the app to answer before calling it a success.
Writing a second copy of that here would mean two restarts on one PC that
could disagree about whether an update is in progress.

One restart at a time. The output is kept so the dashboard can show what
happened, in the watchdog's own words.
"""
import datetime as dt
import os
import subprocess
import threading
from typing import Optional

import apps
import config

_lock = threading.Lock()
job: Optional[dict] = None
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def watchdog_path() -> str:
    return os.path.join(config.BOORD_DIR, "watchdog.ps1")


def _run(app: apps.App) -> None:
    global job
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", watchdog_path(), "-Restart", app.name]
    try:
        # Two minutes: stop_server.ps1 waits up to 20 s for the port, and the
        # watchdog then gives a cold start a full minute to run migrations.
        p = subprocess.run(cmd, capture_output=True, timeout=150, creationflags=_NO_WINDOW,
                           cwd=config.BOORD_DIR)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        state = {0: "done", 2: "refused"}.get(p.returncode, "failed")
    except subprocess.TimeoutExpired:
        out, state = "The watchdog did not finish within 150 seconds.", "failed"
    except OSError as e:
        out, state = f"Could not run the watchdog: {e}", "failed"
    with _lock:
        job.update({"state": state, "output": out.strip().splitlines()[-40:],
                    "finished": dt.datetime.utcnow().isoformat(timespec="seconds")})


def start(app_key: str) -> dict:
    """Raises ValueError with a sentence for the person if it can't start."""
    global job
    app = apps.BY_KEY.get(app_key)
    if app is None:
        raise ValueError("There is no app by that name")
    if config.ROLE != "farm":
        raise ValueError("Restarts can only be done by the monitor on the farm PC")
    if not os.path.exists(watchdog_path()):
        raise ValueError(f"Boord's watchdog.ps1 is not at {watchdog_path()} - "
                         "set MONITOR_BOORD_DIR to Boord's folder")
    with _lock:
        if job and job.get("state") == "running":
            raise ValueError(f"{job['name']} is still restarting - wait for it to finish")
        job = {"app": app.key, "name": app.name, "state": "running", "output": [],
               "started": dt.datetime.utcnow().isoformat(timespec="seconds"), "finished": None}
        snapshot = dict(job)
    threading.Thread(target=_run, args=(app,), name="restart", daemon=True).start()
    return snapshot


def current() -> Optional[dict]:
    with _lock:
        return dict(job) if job else None
