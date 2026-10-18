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
   only need it for the off-site watcher (chapter 6). It is also in
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
  under the banner.

Below the banner, every check is listed by group. Tap a check with **▸**
to see its detail. For example, Published ports shows the PC's live
mapping, and the watchdog log shows its last lines. Each app has **Open**,
which takes you to the app itself.

The page refreshes every 30 seconds. The monitor itself checks every
minute. **Check now** runs the checks immediately.

If your phone loses contact with the monitor, the last picture stays on
screen with a note saying how old it is.

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
| *Google Drive: Not running* | Nothing is being uploaded. Log in on the farm PC and start Google Drive if it doesn't start itself. |
| *The newest backup is N days old* | Backups have stopped reaching that folder. Check the app's own backup card, then the folder. |

## 3. Alerts on your phone

Alerts are **push notifications** from the farm PC to your phone.

**On an iPhone (iOS 16.4 or later)**, this only works from the Home Screen:

1. Open the monitor's address in Safari.
2. Tap **Share**, then **Add to Home Screen**.
3. Open **Monitor** from the Home Screen icon. Not from Safari.
4. Scroll down to **Alerts on this phone** and tap **Turn on alerts**.
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

Amber warnings never send an alert. They wait on the dashboard.

To stop alerts on a phone, tap **Turn off**. That needs no PIN. Removing
the app from the Home Screen also stops them, and the monitor forgets that
phone the next time it tries to alert it.

The farm PC needs internet access to send alerts. Apple's push service is
on the internet, not on the tailnet. If the farm's internet is down,
alerts can't get out. The off-site watcher then notices the farm has gone
quiet, and it alerts you instead.

## 4. Restarting an app

Sign in, then tap **Restart** on the app's card. Everyone using that app
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
`data\watchdog.log`.

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

## 6. The off-site watcher

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

## 7. Updating

On the farm PC, double-click **`update_monitor_server.bat`**. As with
Boord, it installs only releases signed with the release key, then
restarts the monitor.

On the off-site watcher, run:

```sh
git fetch --tags
git checkout <newest tag>
remote/install_remote.sh <same URL> <same token>
```

## 8. Files

| File | What it is |
| --- | --- |
| `data\admin_pin.txt` | The admin PIN (hashed). |
| `data\vapid_private.pem` | The key that signs alerts. Anyone holding it can send notifications to your phones. |
| `data\subscriptions.json` | The phones that turned on alerts. |
| `data\remote_token.txt` | Lets the off-site watcher copy the two files above. |
| `data\drive_backups.txt` | The Google Drive backup folders to check (chapter 5). Not a credential. |
| `data\state.json` | What has been alerted, so a restart of the monitor doesn't re-send standing problems. |

None of these is committed to git. If you lose `data\`, everything still
works: set the PIN again, turn alerts on again on each phone, and give the
off-site watcher the new token.
