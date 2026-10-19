# Boord Monitor - Manual

Boord Monitor is one screen that answers "is the farm all right?", and a
phone alert when it isn't. It watches:

- **The apps**: Boord, Boord Owner, Boord Notes, Kudde and Boord Labour. For
  each one it checks that the app answers on the farm PC, that it can be
  reached over Tailscale, and which version it runs. Boord also reports
  unapplied database migrations, its last backup and any waiting update.
- **Tailscale**: whether the farm PC is connected and signed in, whether its
  machine key is about to expire, and whether `tailscale serve` publishes
  every app on the port the phones expect, with Funnel off. It also checks
  that the HTTPS certificate is still valid.
- **The farm PC**: free disk space, memory, and when it last restarted.
- **The safety nets**: whether Boord's watchdog and heartbeat tasks are set
  up and running, and what the watchdog restarted in the last day.
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
lost power, or is off the internet. Boord's heartbeat emails
(healthchecks.io, Boord MANUAL chapter 2) cover that case too. Keep them:
they work even when the owner's Mac is asleep.

## 1. Installing on the farm PC

Install Boord first: the Restart button uses Boord's `watchdog.ps1`.

1. Clone this repo beside Boord, for example `C:\boord-monitor` next to `C:\Boord`.
2. Double-click **`install.bat`** and accept the administrator prompt. It:
   - installs Python if needed and sets up the virtual environment;
   - finds Boord's folder;
   - asks you to choose the **admin PIN**;
   - registers the "Boord Monitor Server" task, which starts at boot as SYSTEM;
   - starts the monitor on port **8050**, on this PC only (`127.0.0.1`).
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

The **banner** at the top is the answer:

- **Green, "All good"**: nothing to do.
- **Amber, "All up · N to check"**: everything works, but something needs a
  look soon. Examples: the Tailscale key expires in a fortnight, the disk is
  getting full, the PC rebooted half an hour ago, or an app is answering
  with errors.
- **Red, "N problems"**: something is broken now. Each problem is listed
  under the banner, one line each. Tap a line to read the rest.

Below the banner, every check is a small **tile**, grouped by what it
watches, and the whole farm fits on one phone screen:

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

On a farm with many devices the screen tightens itself to stay on one
screen. First the spacing shrinks. If that isn't enough, the devices become
small dots with names, and a device's number shows only when it needs a
look. On a small phone, such as an iPhone SE, a long device list can still
need a short scroll.

**Tap any tile** for the whole explanation and what to do about it. For
example, Ports shows the PC's live mapping, and Restarts shows the
watchdog's last log lines. An app's sheet has **Open app** and **Restart**.

The two buttons at the top right are:

- the **circular arrow**: check now. Pulling the screen down does the same.
- the **"..."** menu, with a small green dot when alerts are on for this
  phone. It holds:
  - **Recent changes**: what broke and was fixed, newest first;
  - **Alerts on this phone**: turn alerts on or off, or send a test
    (chapter 3);
  - **Sign in as admin** / **Sign out**;
  - at the bottom, the monitor's version.

The page refreshes every 30 seconds, and the monitor itself checks every
minute. Under the banner it says when the last check ran. If that turns
orange ("monitor stalled?"), the monitor itself has stopped checking.

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
| *Watchdog / Heartbeat: Not set up* | Run Boord's `setup_watchdog.bat` / `setup_heartbeat.bat`. |
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

The monitor doesn't restart anything itself. It asks Boord's watchdog
(`watchdog.ps1 -Restart`), so a restart from your phone takes the same care
as an automatic one:

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

None of these is committed to git. If you lose `data\`, everything still
works: set the PIN again, turn alerts on again on each phone, and give the
off-site watcher the new token.
