# Boord Monitor - Manual

Boord Monitor answers "is the farm all right?", sends a phone alert when it
isn't, and keeps the history that shows how well the apps have run over the
season: uptime per day, availability against a target (SLO), answer-time
percentiles and a log of every incident. It watches:

- **The apps**: Boord, Boord Owner, Boord Notes, Kudde, Boord Labour and
  Boord Packhouse - every app in `apps.json`. For
  each one it checks that the app answers on the farm PC, that it can be
  reached over Tailscale, and which version it runs. Boord also reports
  unapplied database migrations, its last backup and any waiting update.
- **Tailscale**: whether the farm PC is connected and signed in, whether its
  machine key is about to expire, and whether `tailscale serve` publishes
  every app on the port the phones expect, with Funnel off. It also checks
  that the HTTPS certificate is still valid.
- **The farm PC**: free disk space, memory, and when it last restarted.
- **The safety nets**: whether the watchdog and heartbeat tasks (chapter
  11, run from this folder) are set up and running, and what the watchdog
  restarted in the last day.
- **Google Drive backups**, if you set them up (chapter 5): whether a fresh
  backup has reached each folder Google Drive uploads, and whether Google
  Drive is running to upload it.
- **The devices** on the tailnet (chapter 6): which phones, tablets and
  computers are online now, and when each was last seen. For Boord's
  tablets it also shows when each last sent in a crate.

It runs in two places:

| Where | What it is for |
| --- | --- |
| **The farm PC** (`install.bat`) | Sees everything above, sends the alerts, and has the Restart button. |
| **An off-site watcher** (`remote/install_remote.sh`, on a Mac that stays on) | Looks at the farm the way a phone does. Its one job is the alert the farm PC can't send about itself: *nothing on the farm answers*. |

The farm PC install is the important one. The off-site watcher is
optional, but without it nobody is told when the PC is switched off, has
lost power, or is off the internet. The heartbeat's emails
(healthchecks.io, chapter 11) cover that case too. Keep them: they work
even when the owner's Mac is asleep.

## 1. Installing on the farm PC

Install Boord first, if this PC runs it: the device tiles read Boord's
database.

1. Clone this repo beside Boord, for example `C:\boord-monitor` next to `C:\Boord`.
2. Double-click **`install.bat`** and accept the administrator prompt. It:
   - installs Python if needed and sets up the virtual environment;
   - finds Boord's folder;
   - asks you to choose the **admin PIN**;
   - registers the "Boord Monitor Server" task, which starts at boot as SYSTEM;
   - starts the monitor on port **8050**, on this PC only (`127.0.0.1`);
   - registers the **watchdog**, and the **heartbeat** once
     `data\heartbeat_url.txt` exists (chapter 11). On a PC set up before
     Boord Monitor 2.0, this is what moves both from Boord's folder to
     this one - re-run `install.bat` once after updating.
3. Publish it on the tailnet. Use `serve`, never `funnel`:
   ```
   tailscale serve --bg --https=8050 http://localhost:8050
   ```
   It then answers at `https://<farm-pc>.<tailnet>.ts.net:8050/`.
4. Write down the **remote token** that `install.bat` prints at the end. You
   only need it for the off-site watcher (chapter 7). It is also in
   `data\remote_token.txt`.
5. Set the release key fingerprint, exactly as the installer says, so
   `update_monitor_server.bat` can install signed updates. If Boord runs on
   this PC, it is the same key as in Boord's `data\release_key.fpr`.

Also update Boord to the release that includes Boord Monitor. Its watchdog
then also restarts the monitor if it stops, and has the `-Restart` switch
the Restart button needs.

**The admin PIN** guards two things: the Restart button, and turning on
alerts for a phone. Looking needs no PIN. Anyone on the tailnet can open
the dashboard, as with every Boord app. To change the PIN, run this in the
`backend` folder:

```
.venv\Scripts\python set_pin.py
```

Changing it signs every phone out.

## 2. Reading the screen

The dashboard has four tabs along the bottom, like an iPhone app:

| Tab | What it answers |
| --- | --- |
| **Status** | Is the farm all right now, and how has it been? |
| **Services** | How well is each app doing against its target? |
| **Incidents** | What broke, for how long, and how quickly was it fixed? |
| **Checks** | Every single check, as a small tile. |

The two buttons at the top right are:

- the **circular arrow**: check now. Pulling the screen down from the top
  does the same.
- the **"..."** menu, with a small green dot when alerts are on for this
  phone. It holds **Alerts on this phone** (chapter 3), **Maintenance
  window** (below), **Sign in as admin** / **Sign out**, **Metrics** (for
  other tools, chapter 10) and, at the bottom, the monitor's version.

The page refreshes every 30 seconds, and the monitor itself checks every
minute.

### Status

The **banner** at the top is the answer, in the words status pages use:

- **Green, "All systems operational"**: nothing to do.
- **Amber, "Degraded · N to check"**: everything works, but something needs
  a look soon. Examples: the Tailscale key expires in a fortnight, the disk
  is getting full, the PC rebooted half an hour ago, or an app is answering
  with errors.
- **Red, "Partial outage"** (some apps down), **"Major outage"** (every app
  down) or **"N problems"** (the apps work, but something else is broken,
  such as a backup or a certificate). Each problem is listed under the
  banner, one line each. Tap a line to read the rest.

Under the banner it says when the last check ran. If that turns orange
("monitor stalled?"), the monitor itself has stopped checking.

Below it:

- **Active incidents**: anything broken long enough to open an incident
  (see Incidents). Tap one for its timeline.
- **Maintenance**: a blue note while a maintenance window is on.
- Four **headline numbers**: the apps' availability over 30 days, how many
  incidents are open, the mean time to resolve them (MTTR), and the error
  budget left on the app that has used most of its own (see Services).
- **Uptime bars**, 90 days per component: one thin bar per day, green when
  nothing failed that day, amber for a short outage (up at least 99% of the
  day), red for a longer one, grey for a day with no record. The percentage
  is the 90-day availability. Tap a bar to see that day; tap the name for
  the check itself.

Devices have no uptime bars: tablets are switched off every night, and that
is not an outage.

### Services

One card per app:

- **SLO 99.5% · 30 days** and the big number: the app's target and its
  actual **availability**, the share of the monitor's once-a-minute checks
  it answered. Any answer counts, even an error, the same rule as Boord's
  watchdog. The chip says **Met**, **At risk** or **Missed**.
- **Error budget**: the downtime the target allows. 99.5% over 30 days is
  3 h 36 min. The bar shows how much is left. *At risk* means under a
  quarter is left, or it is being used up very fast.
- **24 h** and **7 days**: availability over shorter windows.
- **Burn 1 h** and **Burn 6 h**: how fast the budget is being spent now. 1×
  spends exactly the whole budget in 30 days; 14× or more (red) is an
  outage happening now.
- **p50, p95, p99**: answer times over the last day. p95 is the time 95 out
  of 100 answers beat, so it shows slowness the average hides. The target
  is p95 under 1 second; **p95 7 d** turns amber when the week misses it.
- The **chart**: answer time over the last 24 hours, with a strip under it
  coloured by status, so an outage shows red even though a down app has no
  answer time. A dashed line marks the target when the line comes near it.
- **Open**, **Restart** (chapter 4) and **Details**.

When the monitor has not been running for the whole 30 days (just
installed, or the PC was off), the card says how much of the window it
watched.

To change an app's targets, see *data\slo.txt* in chapter 9.

### Incidents

An **incident** opens when a check has failed three times in a row - the
moment a phone is woken - and closes by itself on the first good check.
Each has a **timeline**: first failed check, opened, phones told,
acknowledged, reminders, resolved.

- **Acknowledge** (admin PIN) says "I've seen it, I'm on it". It stops the
  reminders that otherwise come every 6 hours. It does not close the
  incident; only the app recovering does. The phone asks your name once, so
  the incident says who is on it.
- A check that is set never to wake anyone (Google Drive in the first half
  hour after a reboot, for example) still opens an incident, marked *not
  paged*, so the record is complete.
- A check that is taken away rather than fixed (a folder removed from
  `drive_backups.txt` while its backup was overdue) has its incident closed
  as "no longer checked" once it has been missing for a day, with no "back
  to normal" push. A day, so a Tailscale blip that hides a check for a few
  runs does not close and later re-open its incident.

At the top, for the last 7, 30 or 90 days:

| Number | Meaning |
| --- | --- |
| **Incidents** | How many started. |
| **MTTR** | Mean time to resolve: first failed check to first good one. How long the farm actually had the problem. |
| **MTTA** | Mean time to acknowledge: from the phones being told to someone tapping Acknowledge. |
| **MTBF** | Mean time between failures. |
| **Downtime** | All resolved incidents added up. |

**Maintenance windows**: before working on the PC (installing updates,
moving an app), tap **New**, choose what (everything, a group or one app),
for how long and why. While it lasts the dashboard still shows red, but no
phone is woken for it. If it is still broken when the window ends, the
phones are told then. **End** finishes a window early. Both need the admin
PIN.

**Recent changes**, at the bottom, lists every change of state, including
blips the watchdog fixed before an incident opened.

### Checks

Every check is a small **tile**, grouped by what it watches:

- The **dot** is the check's state: green is fine, amber needs a look, red
  is broken and grey means not installed or can't be checked here.
- The **name** is short (Owner for Boord Owner, Ports for Published ports,
  Cert for the HTTPS certificate).
- The **number** under it is the one worth knowing:

  | Tile | Number |
  | --- | --- |
  | An app | how fast it answered, or *down* |
  | Key, Cert | days until they expire |
  | Ports | apps published correctly, e.g. 5/6 |
  | Uptime, Watchdog, Heartbeat, backup folders | how long since boot, the last run, or the newest backup |
  | Restarts | how many times the watchdog restarted an app in the last day |
  | A device | *on* when online, otherwise how long since it was last seen |

- **Disk** and **Memory** are bars: the fuller the bar, the fuller the disk
  or memory.

**Tap any tile** for the whole explanation, its last 24 hours as a chart,
and what to do about it. For example, Ports shows the PC's live mapping, and
Restarts shows the watchdog's last log lines. An app's sheet has **Open
app** and **Restart**.

If your phone loses contact with the monitor, the last picture stays on
screen with a note saying how old it is. "Can't reach the monitor - check
Tailscale" means the phone got no answer within 8 seconds: check that
Tailscale is on on the phone, then that the farm PC is on.

### What each problem means

| Problem | What to do |
| --- | --- |
| *Not answering on port 8000* | The app has stopped. The watchdog normally restarts it within 5 minutes. If it doesn't, use **Restart**, then read the watchdog log card. |
| *Running on this PC, but not reachable over Tailscale* | The app is fine, but the phones can't get to it. Check the **Published ports** card. |
| *:443 should be Boord but goes to ...* | `tailscale serve` points a port at the wrong app. The card gives the exact command that fixes it. Boord's MANUAL ("Tailscale serve") explains why. |
| *FUNNEL IS ON* | An app is published on the open internet. Turn it off at once with the command shown. |
| *Tailscale on this PC is signed out* | Sign in from the Tailscale icon by the clock on the farm PC. |
| *Tailscale key expires ...* | In the Tailscale admin console, open the farm PC's menu and choose **Disable key expiry**. A lapsed key drops the PC off the tailnet, and every phone loses every app at once. |
| *Database migrations were not applied* | An update of Boord didn't finish. Run Boord's `update_server.bat` again. |
| *Disk almost full* | Free space on the farm PC. SQLite can't write without room. |
| *Watchdog / Heartbeat: Not set up* | Run `install.bat` in this folder (the heartbeat also needs `data\heartbeat_url.txt`, chapter 11). |
| *Watchdog / Heartbeat: runs C:\Boord\...ps1 instead* | The task still runs Boord's old copy, which only forwards here now. Run `install.bat` in this folder once. |
| *Update: restart* | Windows has installed updates and will restart the PC by itself outside its active hours, taking every app down for a few minutes. Restart it yourself at a quiet time, or set active hours to cover the pack house in Settings > Windows Update > Advanced options. |
| *Dev 03: 14 wait* (amber) | That tablet has held crates unsent for over an hour, and they exist nowhere else. Bring it into signal, and check its sync status on its screen. |
| *Google Drive: Not running* | Nothing is being uploaded. Log in on the farm PC and start Google Drive if it doesn't start itself. |
| *The newest backup is N days old* | Backups have stopped reaching that folder. Check the app's own backup card, then the folder. |

## 3. Alerts on your phone

Alerts are **push notifications** from the farm PC to your phone.

**On an iPhone (iOS 16.4 or later)**, this only works from the Home Screen:

1. Open the monitor's address in Safari.
2. Tap **Share**, then **Add to Home Screen**.
3. Open **Monitor** from the Home Screen icon. Not from Safari.
4. Tap **"..."** at the top right, then **Alerts on this phone**, then
   **Turn on alerts**.
   Enter the PIN if asked, then tap **Turn on alerts** again, and allow
   notifications.
5. Tap **Send a test**. A notification should arrive within a few seconds.

On Android, Chrome can do this from an ordinary tab.

**When an alert is sent:**

- When a problem has lasted **three checks in a row** (about three
  minutes). The watchdog fixes most crashes on its own within five minutes,
  so an outage that fixes itself only appears in *Recent changes* on the
  dashboard, without waking anyone.
- Again **every 6 hours** while the problem lasts.
- Once more when it is **fixed** ("back to normal").

Amber warnings never send an alert. They wait on the dashboard, and in the
morning summary.

**The morning summary.** Every morning at **06:00** (the farm PC's clock),
each phone with alerts on gets one notification. It says either *Farm: all
good* or what isn't, for example:

```
Farm: all up, 2 to check
Check: Disk, Update
Apps: 5 of 5 up
Devices: 7 of 9 online, 14 crates waiting on Dev 03
Backups: Boord 6h, Kudde 2d
Windows wants to restart the PC
```

Its real job is to prove the alerts still work. Without it, a quiet phone
means either that the farm is fine or that alerts stopped reaching you weeks
ago, and you can't tell which. **If a morning passes with no summary, check
the alerts:** tap the bell and send a test.

If the monitor was off at 06:00, the summary comes when it starts, up to
midday. After midday it waits for the next morning. To change the time, or
turn the summary off, set `MONITOR_DAILY_AT` in `start_monitor_server.bat`,
for example `set "MONITOR_DAILY_AT=05:30"` or `set "MONITOR_DAILY_AT=off"`.
Then restart the monitor.

To stop alerts on a phone, tap **Turn off**. That needs no PIN. Removing
the app from the Home Screen also stops them, and the monitor forgets that
phone the next time it tries to alert it.

The farm PC needs internet access to send alerts. Apple's push service is
on the internet, not on the tailnet. If the farm's internet is down,
alerts can't get out. The off-site watcher then notices the farm has gone
quiet, and it alerts you instead.

## 4. Restarting an app

Tap the app's tile, then **Restart**, and enter the PIN if asked. Everyone using that app
loses it for up to a minute. Phones keep what they scanned and send it once
the app is back.

The monitor doesn't restart anything itself. It asks the watchdog
(`watchdog.ps1 -Restart`, chapter 11), so a restart from your phone takes
the same care as an automatic one:

- It **refuses while an update or install is running**. Starting a server
  on top of a half-finished database migration is worse than leaving it
  down.
- It refuses to kill a process it can't prove belongs to that app.
- It reports success only once the app answers again.

The watchdog's own words appear in the restart window and in
`data\watchdog.log`. The window follows the restart for up to 5 minutes,
and stops following it if you close the window. Closing it doesn't stop
the restart: look at the app's tile afterwards.

## 5. Google Drive backups

If Google Drive for Desktop uploads backups from the farm PC, the monitor
can check that it is still happening. It checks two things:

- **That Google Drive is running.** Google Drive for Desktop is not a
  service. It runs only while someone is logged in to Windows. If the PC
  restarts for an update at night and sits at the login screen, the apps
  come back and keep making backups, but nothing uploads them. If Drive is
  not running half an hour after the PC started, you get an alert.
- **That each folder has a fresh backup.** The newest file anywhere in the
  folder, including its subfolders, must be newer than a limit you set.
  An older one sends an alert.

Turn it on by creating `data\drive_backups.txt` in the monitor's folder,
with one folder per line. Optionally add `|` and the most days the newest
backup may be old. The default is 3.

```
# Folders Google Drive uploads. See MANUAL.md chapter 5.
C:\Users\Andre\My Drive\Farm backups\Boord
C:\Users\Andre\My Drive\Farm backups\Kudde | 7
```

The file is read on every check, so changes need no restart. Each folder
appears on the dashboard under **Google Drive backups**, named after its
last part.

**Name the folder on C:, not on Google Drive's G: drive.** The monitor runs
as SYSTEM. Google Drive mounts its G: drive only for the person logged in,
so SYSTEM cannot see it. Use the real folder on C: that Drive uploads:

- **Mirror files:** with Drive set to *Mirror files*, that is under
  `C:\Users\<you>\My Drive`.
- **Folders from your computer:** if Drive backs up a folder from your
  computer, use that folder itself.

A folder the monitor can't open shows amber with this explanation. It never
sends an alert.

**What this proves and what it doesn't.** A green folder means a new
backup arrived where Drive picks it up, and Drive was running. It can't see
inside Drive, so it can't prove the upload finished. If Drive is signed
out, out of storage or stuck, check the Drive icon by the clock. Open
drive.google.com now and then to see the files are really there.

**Quiet seasons.** Boord skips the nightly backup when nothing changed, so
out of season a week-old newest backup can be normal. Give that folder a
longer limit (`| 30`) instead of muting the alerts.

> Boord's manual (chapter 11) warns against copying its backups to a
> personal cloud account. The archives are not encrypted, and they hold
> every worker's name, WhatsApp number and photograph. This check watches
> whatever you set up. It doesn't make that choice safer.

## 6. Devices

Every phone, tablet and computer on the farm's tailnet appears under
**Devices**, without any setup:

- **Green dot, *on*:** online now.
- **Grey dot with a time (*2h*, *3d*):** offline, and how long ago it was
  last seen.

**Offline is not a problem**, so it never turns the banner amber and never
sends an alert. Tablets are switched off at night and lose signal in the
orchard, so an alert every evening would teach everyone to ignore the
monitor. Glance at the grey dots instead. A tablet that should be picking
and has been grey for an hour is worth a phone call.

**The one warning is amber, for a device whose Tailscale key expires within
two weeks.** When it lapses, that device drops off the tailnet and loses
every app. To prevent it, open the Tailscale admin console, open the
device's menu and choose **Disable key expiry**.

**Boord's tablets show more.** If a device's Tailscale name is its Boord
device id (`device-03`), its sheet also says:

- its station and role in Boord;
- when Boord last received a crate from it. This uses the farm PC's clock,
  so a tablet with the wrong date can't make it look current;
- **how many crates it is still holding unsent** (from Boord 3.17). The
  tablet reports this after each sync attempt that reaches the farm PC, and
  the tile then shows, for example, *14 wait*. Until they sync, those crates
  exist only on that tablet.

  **The tile turns amber when the oldest unsent crate is over an hour old**,
  even if the tablet is offline. Pickers at the far end of a block are out
  of signal for a while, and their crates sync on the walk back. After an
  hour, though, the tablet is cut off or its syncs are failing, and a flat
  battery or a fall would lose those crates.

  The count is the tablet's last report: an offline tablet's count is from
  when it last had signal. Like everything under Devices, it never sends an
  alert, but the morning summary names any tablet with crates waiting.

The tile shows Boord's ids short, as *Dev 03*. The monitor reads Boord's
database only to look; it never changes it.

**Choosing and naming the devices.** By default every device on the tailnet
is shown under its Tailscale name. To show only some, in your own order and
with your own names, create `data\devices.txt`:

```
# Tailscale name | name to show | Boord device id
ipad-7f3a        | Field 1      | device-01
ipad-91c2        | Field 2      | device-02
galaxy-tab       | Bin tip      | device-06
andre-iphone     | Andre
```

The Tailscale name is the machine's name in the Tailscale admin console.
The other two columns are optional. Keep names short: a tile has room for
about six letters. If a listed name is no longer on the tailnet (renamed,
or removed), its tile turns amber and says so.

**Hiding a device.** Put `-` in front of its Tailscale name. A file with
only such lines still shows every other device, so hiding one old tablet
doesn't mean listing everything else:

```
-old-tablet
-office-laptop
```

## 7. The off-site watcher

Run this on a machine that stays on, stays awake and is on the tailnet. The
owner's Mac is the usual choice. It needs Python 3.9 or newer.

```sh
git clone <this repo> ~/boord-monitor
cd ~/boord-monitor
remote/install_remote.sh https://<farm-pc>.<tailnet>.ts.net:8050 <remote token>
```

The script:

- sets the watcher up as a launchd agent that starts at login and restarts
  if it exits;
- checks it can reach the farm with that token;
- tells you how to publish its own dashboard with `tailscale serve`, if you
  want to open it from a phone.

Every minute the watcher asks the farm monitor how it is, and checks each
app over Tailscale the way a phone does. Once an hour, while it can reach
the farm, it copies the farm's alert key and its list of phones. So when
the farm goes quiet, it can alert those same phones, with nothing to set up
on the phones.

It sends one kind of alert: **"Farm PC - problem"**. The message tells you
which case it is:

- *Nothing on the farm PC answers over Tailscale*: the PC is off, asleep,
  has lost its internet, or Tailscale has stopped on it.
- *The monitor on the farm PC is not answering, but the apps are*: the PC
  is on and the apps are fine. Only the monitor has stopped, and the
  watchdog should bring it back.

If the watcher's own Tailscale is down, it says so on its dashboard and
**doesn't** alert. It can't tell anything about the farm then, and paging
the farm about the owner's Mac being offline would be a false alarm.

To change the farm address or token, run the script again.

## 8. Updating

On the farm PC, double-click **`update_monitor_server.bat`**. As with
Boord, it installs only releases signed with the release key, then
restarts the monitor.

**Installed before version 1.5.1?** Run **`install.bat`** once more on the
farm PC (it keeps your PIN, alerts and settings). Older installs registered
the "Boord Monitor Server" task with Windows' default 72-hour time limit,
so Windows stopped the monitor three days after every restart of the PC,
and it never started again on its own after a crash. The updater doesn't
change the task, so updating alone doesn't fix this. After re-running it,
the task's settings show *Stop the task if it runs longer than* unticked,
and *If the task fails, restart every 1 minute*.

**Installed before version 1.8?** Run **`install.bat`** once more. The
launcher it writes (`start_monitor_server.bat`) now starts the monitor
again 15 seconds after it stops for any reason, and writes everything the
monitor prints to `data\server.log`. Look there first when the monitor
has been down: a crash prints its error just before the "Server exited"
line. The installer also reserves port 8050 so Windows (Hyper-V, WSL or
Docker) cannot hand it to something else at boot. Updating alone changes
neither.

On the off-site watcher, run:

```sh
git fetch --tags
git checkout <newest tag>
remote/install_remote.sh <same URL> <same token>
```

## 9. Files

| File | What it is |
| --- | --- |
| `data\admin_pin.txt` | The admin PIN (hashed). |
| `data\vapid_private.pem` | The key that signs alerts. Anyone holding it can send notifications to your phones. |
| `data\subscriptions.json` | The phones that turned on alerts. |
| `data\remote_token.txt` | Lets the off-site watcher copy the two files above. |
| `data\drive_backups.txt` | The Google Drive backup folders to check (chapter 5). Not a credential. |
| `data\devices.txt` | Which devices to show, and their names (chapter 6). Optional. |
| `data\state.json` | What has been alerted, so a restart of the monitor doesn't re-send standing problems. |
| `data\heartbeat_url.txt` | The healthchecks.io ping URL for each app (chapter 11). A credential. |
| `data\watchdog.log` | What the watchdog found and restarted (chapter 11). |
| `data\watchdog.pause` | Optional, hand-made: tells the watchdog to leave the servers alone (chapter 11). |
| `data\history.db` | Every check's results (each minute for two days, hourly for 400 days), the incidents and the maintenance windows. Deleting it only loses the history. |
| `data\slo.txt` | Optional: each app's targets, one line per app, `app \| availability % \| p95 ms`, e.g. `boord \| 99.9 \| 500`. App names are boord, owner, notes, kudde, labour. A blank field keeps the default (99.5% and 1000 ms). Read on every refresh. |
| `data\server.log` | Everything the monitor prints, with each start and exit stamped. The previous one is `server.log.1`. |

None of these is committed to git. If you lose `data\`, everything still
works: set the PIN again, turn alerts on again on each phone, and give the
off-site watcher the new token.

## 10. Connecting other tools

The monitor speaks the standard formats, so the farm can be added to
Grafana, Uptime Kuma or any Prometheus-compatible tool later without
changing it. All three are open to the tailnet, like the dashboard:

| Address | What it is |
| --- | --- |
| `/metrics` | Every figure in Prometheus' text format: each check's status, the apps' answer times (with a histogram), disk, memory, key and certificate expiry, backup ages, devices, open incidents and each app's SLO. |
| `/healthz` | Answers `{"status": "ok"}` while the monitor is running. |
| `/readyz` | 200 when the monitor has checked within the last few minutes and can write its history; 503 otherwise, so an outside probe can tell a stuck monitor from a working one. |

For example, Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: boord
    scheme: https
    metrics_path: /metrics
    static_configs:
      - targets: ["bekfontein-server.taile32cb0.ts.net:8050"]
```

## 11. Keeping the apps running: the watchdog and the heartbeat

Two scheduled tasks keep the farm's apps running, and both run from this
folder. Until Boord Monitor 2.0 they lived in Boord's folder; `install.bat`
moves an existing farm across (it re-points both tasks here and copies
`heartbeat_url.txt` and the watchdog's log).

| Task | Every | Does |
| --- | --- | --- |
| **Boord Watchdog** | 5 min | Restarts any app on this PC that has stopped answering. |
| **Boord Heartbeat** | 10 min | Pings healthchecks.io for each app that answers, so you are emailed when this PC is off or off the internet. |

**Register both - they do opposite halves of the job.** The watchdog runs
on this PC, so it can fix things but can tell you nothing when the PC is
off, asleep or off the internet. The heartbeat can't fix anything, but it
is the only one that reaches you when the PC itself is the problem. They
are separate tasks, not part of the monitor's server, so a crashed
monitor still has its restarts and its heartbeat - and the watchdog is
what restarts the monitor.

The **Safety nets** tiles show both tasks. They turn amber when a task is
missing, has stopped running, or still runs Boord's old copy (run
`install.bat` here once).

**Adding an app to the farm** is one line in `apps.json` (name, port,
`tailscale serve` port, task, health path, updater), released as a Boord
Monitor update. The watchdog, the heartbeat and the dashboard all pick it
up; nothing else needs editing.

### The heartbeat (email if the PC goes down)

Gets you an email if a server is unreachable for more than an hour.
This can't be done by anything running only on the server PC itself - if
that PC loses power or its internet entirely, nothing on it can send you
anything. Instead it uses a **"dead man's switch"**: the server pings an
outside monitoring service every 10 minutes to say "still alive," and
that outside service is the one that notices when the pings stop and
emails you - it's watching for silence, not waiting to be told about a
problem.

**Step 1 - Create a healthchecks.io check for each application.**
1. Sign up free at [healthchecks.io](https://healthchecks.io).
2. Create **one check per application you want watched** - every app in
   the table below can have its own. Name them so you
   can tell them apart in an email at six in the morning ("Boord",
   "Kudde", and so on). Watching only Boord is a perfectly reasonable
   choice; the others are then simply not reported on.
3. On each check, click **Edit** and set **Period** to **10 minutes** and
   **Grace Time** to **1 hour** - an isolated missed ping (a brief
   network blip) is tolerated, but if pings stop entirely for over an
   hour, it emails the account's address.
4. Copy the ping URL from each check's page (each starts with
   `https://hc-ping.com/...`). They are different URLs; keep track of
   which belongs to which.

**Step 2 - Save the ping URLs on the server.**
Create a text file at `data\heartbeat_url.txt` in Boord Monitor's
folder, naming one application per line:

```
boord = https://hc-ping.com/aaaaaaaa-...
owner = https://hc-ping.com/bbbbbbbb-...
notes = https://hc-ping.com/cccccccc-...
kudde = https://hc-ping.com/dddddddd-...
labour = https://hc-ping.com/eeeeeeee-...
packhouse = https://hc-ping.com/99999999-...
monitor = https://hc-ping.com/ffffffff-...
```

The names on the left are `boord`, `owner`, `notes`, `kudde`, `labour`,
`packhouse` and `monitor` (the `key` of each app in `apps.json`); the
app's full name works too. Case
doesn't matter and neither do spaces or hyphens, so `Boord Owner` and
`boord-owner` both work. Blank lines are ignored, and a line starting
with `#` is a comment. Leave out any application you don't want watched.

If your file already contains a single URL on its own with no name in
front of it - which is what installs before Boord v3.8 were told to create -
**leave it exactly as it is.** A bare URL means Boord, so that file keeps
working untouched, and you can add the others as named lines beneath
it.

> **One line may be bare, and it means Boord.** Adding a second
> application by pasting its URL on a line of its own does not work, and
> the setup script says so rather than letting it silently take over
> Boord's alerting. Give every URL after the first a name.

This file is deliberately **not** committed to git - like a password,
each URL is account-specific and shouldn't live in version control
(`data\` is excluded). A PC set up before the heartbeat moved here had
this file in Boord's folder; `install.bat` copies it across. Anyone holding one of these URLs can
post to your check.

**Step 3 - Register the heartbeat task.**
Run **`install.bat`** again, or double-click **`setup_heartbeat.bat`** in
Boord Monitor's folder. It registers a Scheduled Task
("Boord Heartbeat") that runs `heartbeat.ps1` every 10 minutes. For each
application you listed, it pings that application's check only when the
app itself actually answers on its own port - so a crashed or hung
server (not just a powered-off PC) also triggers the alert, since the
ping is contingent on the app working, not just the PC being on.

The script finishes by reading `data\heartbeat_url.txt` back and printing
which applications it will report on. **Read that list.** A mistyped name
is otherwise completely silent: the line does nothing, that application
is never reported on, and the absence of alerts looks exactly like an
application that has been fine all along. To see the same list again at
any time:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File heartbeat.ps1 -DryRun
```

That reads the file and pings nothing.

That's it - as long as a check on healthchecks.io keeps receiving pings,
nothing happens. If that application goes down and stays down past the
1-hour grace period, healthchecks.io emails the account used to sign up,
naming the check - which is why it is worth naming them properly in step
1. The applications are independent: Boord going down does not silence
Kudde's check or vice versa.

Note that an application listed in `data\heartbeat_url.txt` but **not
installed** on this PC will be reported as down, because it genuinely
isn't answering. If you remove an application from the PC, remove its
line from the file too.

**What the ping carries.** Each successful ping sends a short line of text
with it, visible in the check's log on healthchecks.io. It is there so the
release a site is running can be read from the monitoring account instead
of phoning someone and asking them to read a screen. Every app with an
`/api/version` sends it; one without simply pings with nothing attached.
It contains exactly:

- the release tag and whether it is a release, a checkout between releases,
  or unknown
- the version the app screens display
- the database schema revision the code expects, and the one the database
  is actually at - so a farm that never ran its migrations is visible
- the date of the last backup, and how many are kept

**It contains nothing about any person** - no worker names, WhatsApp
numbers, photographs or counts, no supplier or block names, no
crate or lot figures, no coordinates, no file paths and no usernames. The
pack house's own name is deliberately left out too: the check already
carries whatever name you gave it in your own account.

> **healthchecks.io is a third party.** Anyone with access to that account
> can read these lines. The ping URL in `data\heartbeat_url.txt` is a password
> in every practical sense - anyone holding it can post to your check.

### The watchdog (restart an app that stops)

**But "leave it running" is not a plan on its own.** Each server on this
PC is registered to start when Windows starts, and after that nothing
looks at it again. If the app stops at eleven in the morning - an
unhandled error, a bad update, Windows reclaiming memory under pressure -
the scheduled task simply ends, and nothing brings it back until somebody
reboots the PC. On a pack house floor that is a morning of paper.

**The watchdog fixes that.** `install.bat` registers it: a Scheduled Task
("Boord Watchdog") that runs `watchdog.ps1` from this folder every 5
minutes and, for each of the servers installed on this PC, checks that
the port actually answers. Anything that has stopped answering is
stopped properly and started again. (`setup_watchdog.bat` registers it on
its own, and shows what it finds.) It covers every application in
`apps.json` - the one table of the farm's apps, which the monitor and the
heartbeat read too:

| Application | Port | Scheduled task |
| --- | --- | --- |
| Boord | 8000 | Boord Server |
| Boord Owner | 8010 | Boord Owner Server |
| Boord Notes | 8020 | Boord Notes Server |
| Kudde | 8030 | Kudde Server |
| Boord Labour | 8040 | Boord Labour Server |
| Boord Monitor | 8050 | Boord Monitor Server |
| Boord Packhouse | 8060 | Boord Packhouse Server |

An application that isn't installed here is skipped, not reported as
down, so the same file suits a PC running two of them and a PC running
all of them. What it found and what it did is written to
`data\watchdog.log`, which the **Watchdog log** tile reads.

**Register the watchdog *and* the heartbeat above - they do opposite
halves of the job.** The watchdog runs on the server PC, so it can fix
things but can tell you nothing when that PC is off, asleep or off the
internet. The heartbeat can't fix anything, but it is the only one of the
two that reaches you when the PC itself is the problem. Neither replaces
the other.

**What it deliberately won't do.**

- **It won't restart a server over a slow moment.** A single missed check
  is not an outage - a virus scan can stall a request past ten seconds.
  It probes three times, fifteen seconds apart, and only acts if all
  three fail.
- **It won't restart anything for the first five minutes after a
  reboot**, while the servers are still starting.
- **It won't restart anything while an update or an install is
  running.** An app's updater (`update_server.bat` and the like, each
  named in `apps.json`) stops the server *on purpose* so the
  database migration runs with nothing writing to it; a watchdog that
  helpfully started it again would put a live server on top of a
  half-migrated database. It notices those scripts running and stands
  down.
- **It won't kill a process it can't prove is ours.** If something else
  on the PC has taken one of these ports, it refuses to restart and says
  so in the log, rather than killing an unrelated program with system
  rights.
- **It won't restart a server that is merely erroring.** Any HTTP
  answer - including a 500 - counts as up. Only a refused connection or
  a timeout counts as down, because a restart cannot fix a bug and taking
  a working pack house offline to try is the worse trade.

**Pausing it.** If you're working on a server by hand and want it to stay
down, create an empty file at `data\watchdog.pause` in Boord Monitor's
folder. (A maintenance window, chapter 2, is different: it stops the
*alerts*, while this stops the *restarts*. Working on a server by hand
usually wants both.)
The watchdog leaves everything alone while that file exists. Delete it
when you're done - and if you forget, it's ignored after 12 hours and
normal restarts resume, so a forgotten pause file can't quietly disable
the thing for a season.

**Checking on it.** To see the current state of every app without waiting
for the next run, open a terminal in Boord Monitor's folder and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File watchdog.ps1 -CheckOnly
```

That reports what is up and what is down and changes nothing.

**Restarting one app on purpose.** The Restart button (chapter 4) runs
the watchdog with `-Restart`, so a restart asked for from a phone takes
the same care as an automatic one: it stands down while an update is
running, and refuses to kill a process it can't prove is that app's. The
same works from a terminal in Boord Monitor's folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File watchdog.ps1 -Restart "Boord Owner"
```

The name is the one in the table above. It exits 0 once the app answers
again, 2 if it refused because an update or install is running, and 1
otherwise. Either way it says what it did in `data\watchdog.log`.
