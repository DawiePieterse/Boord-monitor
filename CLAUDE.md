# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Boord Monitor watches the farm PC: the six sibling apps (Boord 8000, Owner 8010, Notes 8020, Kudde 8030, Labour 8040, Packhouse 8060), Tailscale (connection, node key expiry, `tailscale serve` mapping, certificate), the PC itself (disk, memory, uptime), and the watchdog and heartbeat tasks - which, since 2.0, run scripts from this repo. It shows a status-page dashboard PWA with uptime history, per-app SLOs and an incident log, sends Web Push alerts, exposes Prometheus `/metrics`, and has a PIN-protected Restart button. `MANUAL.md` is the user and admin manual.

It shares no code with the siblings. It reads Boord's `data/boord.db` read-only for the device tiles (`MONITOR_BOORD_DIR`).

## The Boord suite

This app is one of seven that run together on the farm PC (`bekfontein-server`), each its own repo, process, port and database. They are reached only over Tailscale, and each binds `127.0.0.1` and is published with `tailscale serve`. The same section is in every app's `CLAUDE.md`. When a fact here changes, change it in all seven.

| App | Repo (`DawiePieterse/…`) | What it does | Port | Tailnet address | Mac folder | Farm PC folder |
|---|---|---|---|---|---|---|
| Boord | `Boord` | Harvest capture and picker pay per kg: field tablets, pack-house receiving, admin office. The hub. | 8000 | `https://bekfontein-server.taile32cb0.ts.net/` | `~/Documents/Projects/Boord` | `C:\Boord` |
| Boord Owner | `boordowner` | The owner's view: season vs history, weather, risk, crop estimate, optional AI. | 8010 | `https://bekfontein-server.taile32cb0.ts.net:8443/` | `~/Documents/Projects/BoordOwner` | `C:\BoordOwner` |
| Boord Notes | `boord-notes` | Private farm notebook for Andre and his son, offline-first, optional AI. | 8020 | `https://bekfontein-server.taile32cb0.ts.net:9443/` | `~/Documents/Projects/BoordNotes` | `C:\bekfontein-farm-notebook` |
| Kudde | `Kudde` | Cattle records: animals and their events. | 8030 | `https://bekfontein-server.taile32cb0.ts.net:8030/` | `~/Documents/Projects/Kudde` | `C:\Kudde` |
| Boord Labour | `Boord-labour` | Packhouse time clock: badge scans at the door, hourly pay with overtime. | 8040 | `https://bekfontein-server.taile32cb0.ts.net:8040/` | `~/Documents/Projects/Boord-labour` | `C:\Boord-labour` |
| **Boord Monitor** (this app) | `Boord-monitor` | Watches the PC and the other apps: status page, SLOs, incidents, Web Push alerts, a Restart button; runs the watchdog and heartbeat. | 8050 | `https://bekfontein-server.taile32cb0.ts.net:8050/` | `~/Documents/Projects/Boord-monitor` | `C:\boord-monitor` |
| Boord Packhouse | `boord-packhouse` | After receiving to the truck: sulfur, acid, lines, pre-pack, cold store and PPECB, dispatch. | 8060 | `https://bekfontein-server.taile32cb0.ts.net:8060/` | `~/Documents/Projects/Boord-packhouse` | `C:\Boord-packhouse` |

**How they depend on each other:**
- **Boord is the hub.** Its `data/boord.db` is opened read-only by Owner (harvest, lots, blocks, suppliers), Labour (the worker register, and Boord's `data/photos` for faces), Monitor (tablet station, last sync and outbox) and Packhouse (received lots, blocks, suppliers, workers).
  - Boord is that database's only writer and migrates it at startup.
  - Owner, Labour and Packhouse refuse to boot if a column they read disappears. A Boord migration that drops or renames a column needs those apps updated in step.
- **Labour's badges are Boord's.** The badge QR holds exactly `worker.id` (`Boord/frontend/admin/print-badge.html`), and Labour and Packhouse vendor Boord's `frontend/shared/`.
- **Owner asks Notes.** The "Ask the farm notes" bridge calls Notes' `/api/ai/ask` at `OWNER_NOTES_URL` (`http://127.0.0.1:8020`) and can share Notes' `data/anthropic_key.txt`.
- **Monitor keeps the others running.** Since Monitor 2.0 its `watchdog.ps1` (restarts an app that stops answering) and `heartbeat.ps1` (pings healthchecks.io) cover all seven, as the scheduled tasks "Boord Watchdog" and "Boord Heartbeat". Until then they lived in Boord's repo; Boord's copies are now forwarders to Monitor's.
  - **The app table is Monitor's `apps.json`, the one copy.** Monitor's `backend/apps.py`, `watchdog.ps1` and `heartbeat.ps1` all read it. Adding or moving an app is one line there plus the port table above; the `tailscale serve` ports are also in Boord's MANUAL.md.
  - The watchdog stands down while any app's updater (its `updater` in `apps.json`) or an installer runs, and its `-Restart "<Name>"` is what Monitor's Restart button runs. It exits 2 when it stands down.
- **Monitor watches everything.** It shows each app's up/down state and `/api/version`, Tailscale and the PC, keeps uptime history, SLOs and incidents, and pushes an alert after three failed checks in a row.
  - Every app's `/api/version` keeps the same field names (`tag`, `state`, `frontend_version`), because Monitor and the heartbeat read them all.
  - An off-site copy of the monitor on the owner's Mac alerts when the whole farm PC is unreachable.
- **Kudde and Notes stand alone.** No other app reads their data. They are only watched and restarted.

**What they share by convention, not code:**
- One release key: fingerprint `67C64CFDD584DD140E58AF6E329C9B9DD0562A9D` in each app's `data\release_key.fpr`.
- Releases are signed tags, made from an up-to-date `main` on the Mac and installed by each app's updater on the farm PC.
- `stop_server.ps1` stops each server, never a bare `schtasks /end`. Labour's copy is the reference.
- Each installer adds a Git `safe.directory` exception so SYSTEM's Git can read the repo folder.
- Python 3.9-compatible code, plain JS with no CDNs (the phones are tailnet-only), and `VERSION` plus the service-worker `CACHE` bumped with every frontend change.
- Fixes to the copied deployment files (`install.ps1`, the updaters, `stop_server.ps1`, `release.sh`) flow between the repos.

## Commands

All backend commands run from `backend/`. Keep code Python 3.9-compatible (the farm's minimum): no `X | None`, no `match`.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                                      # all tests, no network
.venv/bin/python -m pytest tests/test_checks.py -k serve -q         # one area
MONITOR_DATA_DIR=/tmp/mon-dev .venv/bin/python -m uvicorn main:app --port 8050   # farm role, http://localhost:8050/
MONITOR_ROLE=remote MONITOR_FARM_URL=http://127.0.0.1:8050 MONITOR_DATA_DIR=/tmp/mon-remote \
  .venv/bin/python -m uvicorn main:app --port 8070                  # remote role against the above
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
- **History** (`store.py`, `data/history.db`, SQLite WAL, stdlib only): every check every run for two days (`sample`), rolled up per hour with a latency histogram for 400 days (`hourly`), plus `incident`, `incident_event` and `silence`. `monitor.run_once` records each run; a failing history write is logged and never stops the alerts. Timestamps are Unix seconds inside, naive-UTC ISO outside.
- **Incidents** follow the push rule exactly: one opens when a check reaches `FAIL_AFTER` fails (paged or not), resolves on the first non-fail, and keeps a timeline. **Acknowledge** (PIN) only stops the `REMIND_EVERY_S` reminders. **Silences** (maintenance windows, PIN, at most a week; `match` is `*`, `group:<key>` or a check id) hold back the push; a check still failing when the silence ends pushes then.
- **SLOs** (`slo.py`): availability SLI = (ok+warn)/(ok+warn+fail) of the apps' checks, default 99.5% over 30 days and p95 < 1000 ms, overridable per app in `data/slo.txt`. Error budget, multiwindow burn rate (1 h, 6 h) and latency percentiles (exact from samples up to 48 h, histogram-interpolated beyond). Display only: SLOs never push.
- **Standard endpoints:** `/metrics` (Prometheus text format, hand-written in `metrics.py`, `boord_` prefix, base units), `/healthz` (liveness) and `/readyz` (loop ran recently and history writable, else 503). `/api/overview` feeds the Status and Services tabs and is cached per check run.
- **No new dependencies.** History, metrics and SLOs use only the standard library, so the updater's import list still equals `requirements.txt`.
- **Up/down follows watchdog.ps1's rule:** any HTTP answer, even 500, means up. A 500 is a warning. `/api/version` is read for info only, never for up/down.
- **Installed = scheduled task registered** (Windows). An app whose task is absent is `off`, not down, and is not expected in the serve table. Off Windows, `query_tasks` returns None and every app is checked.
- **Scheduled tasks are read with `Get-ScheduledTaskInfo` → JSON,** not `schtasks /v`, whose labels follow the PC's display language.
- **One push key, two senders** (`push.py`):
  - The farm makes the VAPID key and stores the phones' subscriptions.
  - The remote copies both hourly from `GET /api/remote/bundle` (`X-Monitor-Token`, the farm's `data/remote_token.txt`), so it can alert the same phones when the farm is down.
  - Subscriptions are tied to the key, not the origin.
- **The watchdog and heartbeat are this repo's** (`watchdog.ps1`, `heartbeat.ps1`, `setup_*.bat`; moved from Boord in 2.0). They stay separate scheduled tasks ("Boord Watchdog", "Boord Heartbeat"), never part of the server process: a crashed monitor must not also stop the restarts, and the watchdog is what restarts the monitor.
  - **`apps.json` is the one app table.** `apps.py`, `watchdog.ps1` and `heartbeat.ps1` read it; `tests/test_apps_table.py` fails if a script grows its own table again. The watchdog's maintenance markers come from each app's `updater`.
  - Their files are in `data\`: `watchdog.log` (read by `guards.log`), `watchdog.pause`, `heartbeat_url.txt` (a credential).
  - `install.ps1` registers both tasks here (the heartbeat only once `data\heartbeat_url.txt` exists), copying Boord's old `heartbeat_url.txt` and `watchdog.log` across once. `_TASKS_PS` reads each task's action, and `guards.watchdog`/`guards.heartbeat` warn while a task still runs another folder's script.
  - Boord keeps forwarders under the old names for farms not yet re-pointed, so this repo's release goes first.
- **Restart never reimplements stopping a server.** `restart.py` runs this repo's `watchdog.ps1 -Restart`. That stands down during updates and installs (exit 2), uses `stop_server.ps1`, and waits for the app to answer.
- **Access:**
  - Reading is open to the tailnet, like every sibling.
  - Restart, push subscribe, push test, incident acknowledge and silences need the admin PIN (`admin_auth.py`, copied from Boord Labour; cookie `monitor_admin`, set only via `set_pin.py`).
  - Unsubscribe needs no PIN.
  - Never bind 0.0.0.0.

## Keep in step with the siblings

- **The app table** is `apps.json` here. The serve ports are also in Boord's MANUAL.md and the port table in every `CLAUDE.md`. Change them together.
- **Frontend changes need two bumps:** `VERSION` in `frontend/api.js` and `CACHE` in `frontend/service-worker.js`.
- **Plain JS and CSS, no build step, no CDNs.** The phones are tailnet-only.
- **The dashboard is phone first, with four tabs** (Status, Services, Incidents, Checks; `#hash` routes in `app.js`). It scrolls; what must stay on screen without scrolling is the Status tab's banner. Charts are inline SVG from `frontend/charts.js`, no library.
  - Each check is still a tile on the Checks tab: its dot, a short name (`short`, from `SHORT` in `checks.py`) and `value`, at most about six characters. A check with `pct` is drawn as a bar. The full sentence, detail and a 24-hour chart open in a sheet.
  - **The smoke test** opens every tab at phone and desktop sizes and fails on page errors, sideways scrolling, or a status banner hidden behind the tab bar, both against the live server and in the `phone-busy` pass (`/api/status` from `scripts/fixtures/busy_farm.json`: two problems and 16 devices).
- **Every push must show a notification** (`service-worker.js`). iOS revokes push for a site that receives a push silently.
- **Copied from Boord Labour:** `install.ps1`, `update_monitor_server.bat` and `scripts/release.sh`. Keep fixes flowing between the copies. The updater's import list must equal `requirements.txt` (a test checks this).
- **`install.ps1` adds the repo folder to Git's machine-wide `safe.directory`.** The monitor runs as SYSTEM, and the folder belongs to the admin account that cloned it, so Git refused it as "dubious ownership" and `/api/version` reported `"tag": null`. Boord Owner's and Boord Notes' installers carry the same block, so change all three together.
- **Releases are signed tags made by the owner, from an up-to-date `main`.** `release.sh` checks that `VERSION` matches the tag. Run `git pull --ff-only origin main` first anyway: a stale clone is how Boord Notes' `v2.16` ended up on a months-old commit.
- **The off-site watcher** runs on the owner's Mac from `~/Documents/Projects/Boord-monitor` (launchd `com.boord.monitor-remote`). Moving that folder breaks both the launchd job and the `.venv`. After a move, delete `backend/.venv` and re-run `remote/install_remote.sh` with the same URL and token.

## Installing and deploying

### Where it lives

| | |
|---|---|
| Your Mac (the only clone to release from) | `~/Documents/Projects/Boord-monitor` |
| Farm PC folder | `C:\boord-monitor` |
| Farm PC scheduled task | "Boord Monitor Server", runs as SYSTEM at boot |
| Port on the farm PC | `127.0.0.1:8050` |
| Address on the tailnet | `https://bekfontein-server.taile32cb0.ts.net:8050/` |
| Off-site watcher | your Mac, from the same folder, launchd job `com.boord.monitor-remote` |

### First install on the farm PC (once)

Only for a new PC or a reinstall. An app that is already running is updated, not reinstalled.

1. Open a Command Prompt and clone the repo into the folder above:
   ```bat
   git clone https://github.com/DawiePieterse/Boord-monitor.git C:\boord-monitor
   ```
2. In `C:\boord-monitor`, double-click **`install.bat`** and accept the administrator prompt. It asks for Boord's folder (for its database), registers the watchdog (and the heartbeat, once `data\heartbeat_url.txt` exists), and asks for the admin PIN (`backend\set_pin.py` changes it later). At the end it prints the remote token for the off-site watcher.
   It installs Python if needed, builds the app's environment, writes the launcher, registers the scheduled task, reserves the port so Windows can't hand it to Hyper-V, WSL or Docker at boot, and starts the server. Re-running it is safe and keeps the data.
3. Tell the updater which release key to trust. In the same folder, type it exactly like this, with the redirect first:
   ```bat
   >data\release_key.fpr echo 67C64CFDD584DD140E58AF6E329C9B9DD0562A9D
   ```
4. Publish it on the tailnet, in a Command Prompt opened as administrator:
   ```bat
   tailscale serve --bg --https=8050 http://localhost:8050
   ```
5. If the installer did not already add it, let SYSTEM's Git read the folder, as administrator (forward slashes):
   ```bat
   git config --system --add safe.directory C:/boord-monitor
   ```
6. Check it from your Mac: `curl -s https://bekfontein-server.taile32cb0.ts.net:8050/api/version` should show the release tag.

The off-site watcher is installed on your Mac, not the farm PC, from the folder above:
```sh
cd ~/Documents/Projects/Boord-monitor
read -s "TOKEN?Remote token (hidden): "; echo   # from data\remote_token.txt on the farm PC
remote/install_remote.sh https://bekfontein-server.taile32cb0.ts.net:8050 "$TOKEN"
```
After moving that folder, delete `backend/.venv` and run `install_remote.sh` again with the same URL and token.

### Deploying a change (every release)

Merging to `main` changes nothing on the farm. A release is a signed tag, and the farm PC installs it when its updater runs.

1. **Merge the PR on GitHub.** A frontend change must already bump `VERSION` in `frontend/api.js` and `CACHE` in `frontend/service-worker.js`.
2. **On your Mac, bring the clone up to date.** This step is what stops a tag landing on an old commit:
   ```sh
   cd ~/Documents/Projects/Boord-monitor && git checkout main && git pull --ff-only origin main
   git log -1 --format='%h %s'                  # the merge you expect
   grep -m1 'VERSION:' frontend/api.js   # the version you are releasing
   ```
3. **Sign and push the tag** (`vX.Y` must equal `VERSION`):
   ```sh
   scripts/release.sh vX.Y && git push origin vX.Y
   ```
   Never move or reuse a released tag. Fix a wrong one with the next version.
4. **On the farm PC**, in `C:\boord-monitor`, double-click **`update_monitor_server.bat`**. It verifies the signature, stops the server with `stop_server.ps1`, installs dependencies, and starts it again. If it stops with an error, nothing has changed. Read the message; never check the tag out by hand.
5. **If the release changed `install.ps1`** (its notes say "run install.bat once more"), double-click `install.bat` as well. The updater does not rewrite the launcher or the task.
6. **Check from your Mac:** `curl -s https://bekfontein-server.taile32cb0.ts.net:8050/api/version` should show `"tag":"vX.Y","state":"release"`.
7. **Close and reopen the dashboard on each phone so it loads the new version.**

## Conventions

Comments are long on purpose. They explain *why*, usually a farm constraint: one person, a phone in an orchard, a PC nobody administers. Keep that style.
