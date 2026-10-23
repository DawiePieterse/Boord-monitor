"""The apps on the farm PC, and where each one is published on the tailnet.

Read from apps.json at the top of this repo - the one copy of this table.
watchdog.ps1 and heartbeat.ps1, beside it, read the same file. Until 2.0
there were three hand-kept copies (this module, and Boord's watchdog.ps1 and
heartbeat.ps1), and an app added to one and not the others was watched by
some and silently not by the rest.

`ts_port` is the `tailscale serve --https=` port. The serve check compares
the PC's live mapping against it, because the failure it catches is silent:
on 2026-09-04 :443 pointed at Boord Owner, so Boord's address loaded and
answered {"detail":"Not Found"} for /admin/ - a healthy server saying it has
no such page - and the Field QR scanner could not have been working at all.

`health` is the path that proves the process is up. It is "/" for most, but
Kudde refuses every request that did not arrive over Tailscale, including
from this PC's own console, and answers only /healthz. Boord Notes' "/"
redirects to /app/, so it is probed at its own /api/health instead.

`version` is the app's /api/version, where it has one. Probed only from this
PC, never relied on for up/down: it touches the database, and a slow query
must not turn a healthy farm red.
"""
import json
import os
from typing import List, Optional

APPS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps.json")


class App:
    def __init__(self, key: str, name: str, port: int, ts_port: int, task: str,
                 health: str = "/", version: Optional[str] = None,
                 path: str = "/", updater: str = "", aliases: Optional[List[str]] = None) -> None:
        self.key = key
        self.name = name
        self.port = port
        self.ts_port = ts_port
        self.task = task
        self.health = health
        self.version = version
        # Where a person opens it - the link on the dashboard card.
        self.path = path
        self.updater = updater
        self.aliases = aliases or []


def _load() -> tuple:
    with open(APPS_FILE, encoding="utf-8") as fh:
        table = json.load(fh)
    apps, me = [], None
    for row in table["apps"]:
        a = App(row["key"], row["name"], int(row["port"]), int(row["ts_port"]), row["task"],
                health=row.get("health") or "/", version=row.get("version"), path=row.get("path") or "/",
                updater=row.get("updater") or "", aliases=row.get("aliases") or [])
        if row.get("self"):
            me = a
        else:
            apps.append(a)
    if me is None:
        raise RuntimeError(f"{APPS_FILE} has no entry marked \"self\" for the monitor")
    return apps, me, table["watchdog_task"], table["heartbeat_task"]


APPS, SELF, WATCHDOG_TASK, HEARTBEAT_TASK = _load()
# APPS is what the monitor probes as apps. SELF is this app: not probed as an
# app - if it is answering the dashboard it is up - but its serve mapping is
# checked like the others', since a phone that cannot reach it gets no
# dashboard. The watchdog and heartbeat cover it like any other.

BY_KEY = {a.key: a for a in APPS}

# The two scheduled tasks that keep the PC honest. Both run scripts from
# this repo (since 2.0; from Boord's before). A farm without the watchdog
# has nothing that restarts a crashed app; a farm without the heartbeat has
# nobody told when the PC itself is off - the monitor reports either
# missing, because both failures are silence.
