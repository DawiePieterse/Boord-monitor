"""Paths and environment for Boord Monitor.

The same code runs in two places, chosen by MONITOR_ROLE:

  farm    (the default) - on the farm PC, beside the five apps it watches.
          It can see everything local: the ports, the scheduled tasks, the
          watchdog log, the disk, and what `tailscale serve` is publishing.
          It is also the only one that can restart anything. What it cannot
          do is tell anybody that the PC itself is off.

  remote  - on some other always-on machine on the tailnet (the owner's Mac,
          typically). It looks at the farm the way the phones do, over
          Tailscale, and its one job is the alert the farm PC cannot send
          about itself: "the farm PC is not answering at all".

Every path can be overridden by an environment variable so the installer and
the tests can point the service wherever they need.
"""
import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

ROLE = os.environ.get("MONITOR_ROLE", "farm").strip().lower()
if ROLE not in ("farm", "remote"):
    raise RuntimeError(f"MONITOR_ROLE must be 'farm' or 'remote', not {ROLE!r}")

# Holds the push keys, the phones' alert subscriptions, the admin PIN and the
# check history. None of it is farm data, but the subscriptions and the VAPID
# private key are credentials, so the folder is gitignored.
DATA_DIR = os.environ.get("MONITOR_DATA_DIR", os.path.join(REPO_ROOT, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# Clear of Boord 8000, Owner 8010, Notes 8020, Kudde 8030, Labour 8040, Packhouse 8060.
# Bound to 127.0.0.1 by the installer and published by `tailscale serve
# --https=8050`, the same as every sibling.
PORT = int(os.environ.get("MONITOR_PORT", "8050"))

FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")

# Boord's checkout, for its database only: the device tiles read when each
# tablet last sent a crate from Boord's data\boord.db, read-only.
BOORD_DIR = os.path.abspath(os.environ.get(
    "MONITOR_BOORD_DIR", os.path.join(REPO_ROOT, "..", "Boord")))

# The watchdog and the heartbeat: scheduled tasks that run these scripts from
# this repo (Boord's until 2.0). The watchdog's log is the monitor's record
# of what it restarted, and its -Restart is what the Restart button runs.
WATCHDOG_PS1 = os.path.join(REPO_ROOT, "watchdog.ps1")
HEARTBEAT_PS1 = os.path.join(REPO_ROOT, "heartbeat.ps1")
WATCHDOG_LOG = os.path.join(DATA_DIR, "watchdog.log")


def _find_tailscale() -> str:
    """The CLI. On the farm PC this runs as SYSTEM, whose PATH does not
    always include Tailscale's folder, so the default install location is
    tried as well rather than reporting a working tailnet as "not found"."""
    override = os.environ.get("MONITOR_TAILSCALE")
    if override:
        return override
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in (r"C:\Program Files\Tailscale\tailscale.exe",
                      "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                      "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"):
        if os.path.exists(candidate):
            return candidate
    return "tailscale"


TAILSCALE_EXE = _find_tailscale()

# How often the whole set of checks runs. A minute is quick enough that a
# morning outage is noticed before the pack house queue forms, and slow
# enough that the PowerShell calls it makes cost the PC nothing noticeable.
CHECK_INTERVAL_S = int(os.environ.get("MONITOR_CHECK_INTERVAL", "60"))

# A check has to fail this many runs in a row before a phone is woken. The
# watchdog takes up to five minutes plus 45 s of probes to restart a dead
# app; alerting on the very first miss would buzz someone for every outage
# the watchdog was about to fix on its own. Three runs is about three
# minutes - still well before anyone at the door gives up and reaches for
# paper.
FAIL_AFTER = int(os.environ.get("MONITOR_FAIL_AFTER", "3"))

# While something stays broken, remind every few hours rather than once and
# never again - a single 03:00 notification is easily swiped away unread.
REMIND_EVERY_S = int(os.environ.get("MONITOR_REMIND_HOURS", "6")) * 3600

# The morning summary: once a day, at this time on the farm PC's own clock,
# every phone with alerts on gets one "Farm: all good" (or what isn't). Its
# real job is to prove the alert path still works: without it, a quiet phone
# means either that the farm is fine or that alerts died weeks ago, and
# nobody can tell which. "off" turns it off.
DAILY_AT = os.environ.get("MONITOR_DAILY_AT", "06:00").strip().lower()

# --- remote role ----------------------------------------------------------
# The farm monitor's tailnet address, e.g. https://farm-pc.tail1234.ts.net:8050
FARM_URL = os.environ.get("MONITOR_FARM_URL", "").rstrip("/")

# The shared secret that lets the remote watcher fetch the farm's push keys
# and the phones' subscriptions. The farm writes it to data/remote_token.txt
# on first start; it is copied once, by hand, to the remote machine.
REMOTE_TOKEN_FILE = os.path.join(DATA_DIR, "remote_token.txt")

# --- Google Drive backups (farm role) ----------------------------------------
# One folder per line: a folder Google Drive for Desktop uploads, and the most
# days its newest file may be old, after a "|". Lines starting with # are
# ignored; no file means the check is off. Read on every run, so editing it
# needs no restart. See MANUAL.md, "Google Drive backups".
DRIVE_BACKUPS_FILE = os.path.join(DATA_DIR, "drive_backups.txt")
DRIVE_DEFAULT_DAYS = 3

# --- devices on the tailnet (farm role) --------------------------------------
# Optional. Without it every device on the tailnet is shown under its
# Tailscale name. With it, only the devices listed, one per line:
#     <Tailscale machine name> | <name to show> | <Boord device id>
# The last two are optional. See MANUAL.md, "Devices".
DEVICES_FILE = os.path.join(DATA_DIR, "devices.txt")

# Boord's database, read-only, for when each Boord device last sent a crate.
BOORD_DB = os.path.join(BOORD_DIR, "data", "boord.db")

# VAPID's "sub" claim: who to contact about this sender. Apple rejects a push
# whose claim is not a mailto: or https: URL, so it has to look like one.
VAPID_SUB = os.environ.get("MONITOR_VAPID_SUB", "mailto:boord-monitor@example.com")
