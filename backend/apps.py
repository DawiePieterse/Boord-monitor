"""The apps on the farm PC, and where each one is published on the tailnet.

This is the third copy of this table on the farm PC: Boord's watchdog.ps1
and heartbeat.ps1 carry their own. Keep all three in step when an app is
added or moves port.

`ts_port` is the `tailscale serve --https=` port from Boord's MANUAL.md. The
serve check compares the PC's live mapping against it, because the failure it
catches is silent: on 2026-09-04 :443 pointed at Boord Owner, so Boord's
address loaded and answered {"detail":"Not Found"} for /admin/ - a healthy
server saying it has no such page - and the Field QR scanner could not have
been working at all.

`health` is the path that proves the process is up. It is "/" for most, but
Kudde refuses every request that did not arrive over Tailscale, including
from this PC's own console, and answers only /healthz. Boord Notes' "/"
redirects to /app/, so it is probed at its own /api/health instead.

`version` is the app's /api/version, where it has one. Probed only from this
PC, never relied on for up/down: it touches the database, and a slow query
must not turn a healthy farm red.
"""
from typing import List, Optional


class App:
    def __init__(self, key: str, name: str, port: int, ts_port: int, task: str,
                 health: str = "/", version: Optional[str] = None,
                 path: str = "/") -> None:
        self.key = key
        self.name = name
        self.port = port
        self.ts_port = ts_port
        self.task = task
        self.health = health
        self.version = version
        # Where a person opens it - the link on the dashboard card.
        self.path = path


APPS: List[App] = [
    App("boord", "Boord", 8000, 443, "Boord Server", version="/api/version"),
    App("owner", "Boord Owner", 8010, 8443, "Boord Owner Server"),
    App("notes", "Boord Notes", 8020, 9443, "Boord Notes Server",
        health="/api/health", path="/app/"),
    App("kudde", "Kudde", 8030, 8030, "Kudde Server", health="/healthz"),
    App("labour", "Boord Labour", 8040, 8040, "Boord Labour Server",
        version="/api/version"),
]

# This app. Not probed as an app - if it is answering the dashboard it is up -
# but its serve mapping is checked like the others', since a phone that cannot
# reach it gets no dashboard.
SELF = App("monitor", "Boord Monitor", 8050, 8050, "Boord Monitor Server",
           health="/api/version", version="/api/version")

BY_KEY = {a.key: a for a in APPS}

# The other scheduled tasks that keep the PC honest. Both live in Boord's
# repo. A farm without the watchdog has nothing that restarts a crashed app;
# a farm without the heartbeat has nobody told when the PC itself is off -
# the monitor reports either missing, because both failures are silence.
WATCHDOG_TASK = "Boord Watchdog"
HEARTBEAT_TASK = "Boord Heartbeat"
