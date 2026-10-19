# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Boord Monitor watches the farm PC: the five sibling apps (Boord 8000, Owner 8010, Notes 8020, Kudde 8030, Labour 8040), Tailscale (connection, node key expiry, `tailscale serve` mapping, certificate), the PC itself (disk, memory, uptime), and Boord's watchdog and heartbeat tasks. It shows a dashboard PWA, sends Web Push alerts, and has a PIN-protected Restart button. `MANUAL.md` is the user and admin manual.

It shares no database or code with the siblings. It does call into Boord's repo: it reads Boord's `data/watchdog.log` and runs Boord's `watchdog.ps1 -Restart "<Name>"` (`MONITOR_BOORD_DIR`).

## Commands

All backend commands run from `backend/`. Keep code Python 3.9-compatible (the farm's minimum): no `X | None`, no `match`.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                                      # all tests, no network
.venv/bin/python -m pytest tests/test_checks.py -k serve -q         # one area
MONITOR_DATA_DIR=/tmp/mon-dev .venv/bin/python -m uvicorn main:app --port 8050   # farm role, http://localhost:8050/
MONITOR_ROLE=remote MONITOR_FARM_URL=http://127.0.0.1:8050 MONITOR_DATA_DIR=/tmp/mon-remote \
  .venv/bin/python -m uvicorn main:app --port 8060                  # remote role against the above
node ../scripts/smoke_test.js                                       # headless Chromium, server must be running
../scripts/release.sh v1.1                                          # signed tag; VERSION in frontend/api.js must match
```

CI (`.github/workflows/ci.yml`) runs pytest on 3.9 and 3.12, and the smoke test.

## Architecture

- **Two roles, one codebase** (`config.ROLE`):
  - **`farm`** runs on the farm PC as SYSTEM, bound to 127.0.0.1:8050, published by `tailscale serve --https=8050`.
  - **`remote`** runs on an always-on tailnet machine (`remote/install_remote.sh`, launchd on a Mac). Its only alerting check is `farm.monitor`: the farm PC is unreachable. Its own Tailscale problems never push.
- **Checks are pure functions over a `System`** (`system.py`). `checks.py` never touches subprocess, httpx, ssl or ctypes directly. The tests' `FakeSystem` (`tests/conftest.py`) plays a whole farm PC, so add new machine access to `System` and the fake together.
- **Check result shape:**
  - Every check is `{id, group, label, short, status, summary, detail, alert, ...}`.
  - Groups, in screen order and with titles, are `checks.GROUPS`, sent with every status.
  - `status` is `ok|warn|fail|off|unknown`.
  - **Only `fail` with `alert` true ever pushes. Warnings never do.** That is deliberate: a phone that buzzes for warnings gets muted.
- **Alert rules live in `monitor.evaluate`:**
  - push after `FAIL_AFTER` (3) consecutive fails, so the watchdog gets its 5 minutes;
  - remind every `REMIND_EVERY_S` while it stays broken;
  - one "back to normal" on recovery.
  - Streaks persist in `data/state.json`, so restarting the monitor does not re-alert.
- **Google Drive backups** (`checks.drive_checks`, farm role, off unless `data/drive_backups.txt` lists folders):
  - whether `GoogleDriveFS.exe` is running, because Drive for Desktop runs only while someone is logged in;
  - the newest file's age in each folder.

  Folders are Windows paths, so use `ntpath`, not `os.path`. A folder on Drive's `G:` mount is invisible to SYSTEM; the check explains that rather than reporting the backups missing.
- **Devices** (`checks.device_checks`): one tile per tailnet peer, from the same `tailscale status --json` the farm already reads. Optionally filtered and named by `data/devices.txt`.
  - Offline is status `off` with `word` "Offline". It never warns or alerts, because tablets are off at night.
  - Only a peer key expiring within 14 days warns.
  - Boord detail (station, last crate's `synced_at`) comes from Boord's `boord.db`, opened `mode=ro`, via `System.boord_devices`. A schema it can't read just drops the detail.
- **Tablet outbox** (Boord 3.17+): field tablets post their unsent count to Boord (`POST /api/devices/{id}/queue`, stored on Boord's `device` row). The monitor reads it from `boord.db` with the rest of the device detail. Warns past `QUEUE_WARN_S` (an hour), never alerts. An older Boord without the columns just shows no queue.
- **Windows Update** (`checks.update_check`, the `guards.update` tile): a pending restart, read from the registry (`winreg`), plus the Update Orchestrator's next Reboot task. It warns and never alerts.
- **Morning summary** (`monitor._daily_due` / `compose_daily`): one push a day at `MONITOR_DAILY_AT` (default 06:00, PC local time, `System.local_now`), at most `DAILY_LATE_S` late, farm role only. `daily_last` in `state.json` stops a restart from sending it twice. The tests turn it off (conftest), and `test_daily.py` turns it on.
- **Up/down follows watchdog.ps1's rule:** any HTTP answer, even 500, means up. A 500 is a warning. `/api/version` is read for info only, never for up/down.
- **Installed = scheduled task registered** (Windows). An app whose task is absent is `off`, not down, and is not expected in the serve table. Off Windows, `query_tasks` returns None and every app is checked.
- **Scheduled tasks are read with `Get-ScheduledTaskInfo` → JSON,** not `schtasks /v`, whose labels follow the PC's display language.
- **One push key, two senders** (`push.py`):
  - The farm makes the VAPID key and stores the phones' subscriptions.
  - The remote copies both hourly from `GET /api/remote/bundle` (`X-Monitor-Token`, the farm's `data/remote_token.txt`), so it can alert the same phones when the farm is down.
  - Subscriptions are tied to the key, not the origin.
- **Restart never reimplements stopping a server.** `restart.py` runs Boord's `watchdog.ps1 -Restart`. That stands down during updates and installs (exit 2), uses `stop_server.ps1`, and waits for the app to answer.
- **Access:**
  - Reading is open to the tailnet, like every sibling.
  - Restart, push subscribe and push test need the admin PIN (`admin_auth.py`, copied from Boord Labour; cookie `monitor_admin`, set only via `set_pin.py`).
  - Unsubscribe needs no PIN.
  - Never bind 0.0.0.0.

## Keep in step with the siblings

- **The app table** (`apps.py`) is also in Boord's `watchdog.ps1` and `heartbeat.ps1`, and the serve ports are in Boord's MANUAL.md. Change all of them together.
- **Frontend changes need two bumps:** `VERSION` in `frontend/api.js` and `CACHE` in `frontend/service-worker.js`.
- **Plain JS and CSS, no build step, no CDNs.** The phones are tailnet-only.
- **The dashboard is one phone screen** (390 x 844). Each check is a tile showing its dot, a short name (`short`, from `SHORT` in `checks.py`) and `value`, an optional field of at most about six characters. A check with `pct` is drawn as a bar. The full sentence and detail open in a sheet.
  - **Fitting is measured, not hand-tuned:** `fitScreen()` in `app.js` runs after each draw and when the header or status card changes size. It adds `body.dense`, then `body.densest` (devices as dots), until the page fits. Don't let anything change height after `render()` without being watched; that is why `.nav-sub` reserves its line.
  - **The smoke test** fails if the phone layout needs scrolling, both against the live server and in the `phone-busy` pass. That pass answers `/api/status` from `scripts/fixtures/busy_farm.json`: two problems and 16 devices.
- **Every push must show a notification** (`service-worker.js`). iOS revokes push for a site that receives a push silently.
- **Copied from Boord Labour:** `install.ps1`, `update_monitor_server.bat` and `scripts/release.sh`. Keep fixes flowing between the copies. The updater's import list must equal `requirements.txt` (a test checks this).

## Conventions

Comments are long on purpose. They explain *why*, usually a farm constraint: one person, a phone in an orchard, a PC nobody administers. Keep that style.
