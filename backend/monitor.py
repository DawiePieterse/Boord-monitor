"""The check loop: run every check once a minute, remember what changed, and
decide who to wake.

The rule for waking a phone, in one place:
  - only a check marked `alert` can push, and only for status "fail";
  - only once it has failed FAIL_AFTER runs in a row (see config.py for why
    three), so the watchdog gets the chance to fix a crash before anyone is
    told about it;
  - then again every REMIND_EVERY_S while it stays broken;
  - and once more when it recovers, so nobody drives out to the farm at
    05:00 for an outage that ended at 04:10.

What has been alerted is saved to data/state.json. Without it, restarting
the monitor would forget that the phone was already told, and every update
would re-send every standing problem.

Since 2.0 every alert is also an incident (store.py), the way paging tools
work: it opens with the alert, can be acknowledged from the dashboard -
which stops the reminders, nothing else - and resolves itself on recovery,
with a timeline of what happened in between. A fail that never pages (its
check has `alert` false) still opens one, so the record is complete; it just
wakes nobody. A silence (a maintenance window) holds back the push for the
checks it matches; if the check is still failing when the silence ends, the
phone is told then, as Alertmanager does.

Every run's results are also kept in the history (store.record), which is
where the uptime bars, the SLOs and the latency percentiles come from.

Besides the alerts, one summary a day (config.DAILY_AT), so that a silent
phone can be trusted to mean a quiet farm.
"""
import datetime as dt
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import checks
import config
import push
import store
from system import System

log = logging.getLogger("monitor")

STATE_FILE = os.path.join(config.DATA_DIR, "state.json")
MAX_EVENTS = 200
# How long a check may go unreported before its streak and incident are
# closed out as "no longer checked" (see the end of evaluate). A day: long
# enough to outlast any Tailscale outage the watchdog will restart its way
# out of, short enough that a retired tablet is off the badge by tomorrow.
GONE_AFTER_S = 24 * 3600

_lock = threading.Lock()
latest: dict = {}
_streaks: Dict[str, dict] = {}
_events: List[dict] = []
_last_farm: Optional[dict] = None
_last_bundle = 0.0
_daily_last = ""  # the farm PC's local date the morning summary last went out
_run_lock = threading.Lock()

# Counters since this process started, for /metrics. Prometheus works out
# rates from counters itself, so they only ever go up.
stats = {"runs": 0, "run_errors": 0, "last_run_s": 0.0, "last_run_at": 0.0,
         "pushes": 0, "push_failures": 0}
# Per app: cumulative latency histogram counts and sum, Prometheus-style.
latency_hist: Dict[str, dict] = {}

# How late the morning summary may still go out. A monitor that was down at
# 06:00 and back at 09:00 still sends it - "all good" at 09:00 is still
# worth knowing - but not at 15:00, when it would read as news.
DAILY_LATE_S = 6 * 3600


def _load_state() -> None:
    global _streaks, _events, _daily_last
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        _streaks = saved.get("streaks") or {}
        _events = saved.get("events") or []
        _daily_last = saved.get("daily_last") or ""
    except (OSError, ValueError, AttributeError):
        _streaks, _events, _daily_last = {}, [], ""


def _save_state() -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"streaks": _streaks, "events": _events[-MAX_EVENTS:],
                       "daily_last": _daily_last}, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save %s: %s", STATE_FILE, e)


def reset() -> None:
    """Tests only: forget everything held in memory. data/ is left alone,
    so this is also what a restart of the monitor looks like."""
    global latest, _streaks, _events, _last_farm, _last_bundle, _daily_last
    latest, _streaks, _events, _last_farm, _last_bundle = {}, {}, [], None, 0.0
    _daily_last = ""
    for k in stats:
        stats[k] = 0
    latency_hist.clear()


def _stamp(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _place() -> str:
    """The word that starts every notification, so a lock screen full of
    them says where each came from."""
    return "Farm" if config.ROLE == "farm" else "Farm (seen from outside)"


def _alert_url() -> str:
    # A remote alert is about the farm PC being unreachable, so the link must
    # not point at it. MONITOR_SELF_URL is the watcher's own address.
    if config.ROLE == "remote":
        return os.environ.get("MONITOR_SELF_URL", "/")
    return "/"


def _silenced(c: dict, active: List[dict]) -> Optional[dict]:
    return next((x for x in active if store.matches(x["match"], c)), None)


def _keep(fn, *args, **kwargs):
    """One incident write to history.db, which may fail: the disk is full,
    or the file is locked by a backup agent. The alert decisions must not
    fail with it - a full disk is exactly when pc.disk goes red and the
    phone has to hear about it - so a failed write is logged and the
    streak carries on as if there were no incident record. None stands for
    "could not be done" (an incident with no id, a state that can't be
    read)."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - the history is a record, not the alert path
        log.exception("incident record failed (%s)", getattr(fn, "__name__", fn))
        return None


def evaluate(results: List[dict], now: dt.datetime, silences: Optional[List[dict]] = None) -> dict:
    """Update the streaks and incidents from one run's results. Returns what
    to push: {"new": [...], "remind": [...], "recovered": [...]}. Also
    records an event for every status change, alerting or not, for the
    dashboard."""
    new, remind, recovered = [], [], []
    t = store.ts(now)
    silences = silences or []
    for c in results:
        s = _streaks.get(c["id"]) or {"status": None, "fails": 0, "alerted": False, "last_alert": None}
        s.pop("missing_since", None)  # reported this run, so not on its way out
        if s["status"] is not None and s["status"] != c["status"]:
            _events.append({"at": _stamp(now), "id": c["id"], "label": c["label"],
                            "from": s["status"], "to": c["status"], "summary": c["summary"]})
        prev_status = s["status"]
        s["status"] = c["status"]
        if c["status"] == "fail":
            s["fails"] += 1
            if s["fails"] == 1 or not s.get("started"):
                s["started"] = t
            # A streak carried over from before 2.0 has no incident yet: it
            # gets one now, without telling the phone a second time. The
            # alerting below keys off the streak, not off the incident, so
            # a history.db that can't be written (see _keep) still pages.
            if s["fails"] >= config.FAIL_AFTER and not s.get("incident"):
                s["incident"] = _keep(store.open_incident, c, s["started"], t)
            if s["fails"] >= config.FAIL_AFTER:
                inc = s.get("incident")
                if inc and prev_status == "fail" and s.get("summary") and s["summary"] != c["summary"]:
                    _keep(store.add_event, inc, t, "update", c["summary"], summary=c["summary"])
                silence = _silenced(c, silences)
                pageable = c.get("alert", True) and silence is None
                if pageable and not s["alerted"]:
                    s["alerted"], s["last_alert"] = True, _stamp(now)
                    new.append(c)
                    if inc:
                        _keep(store.add_event, inc, t, "notified", "Phones told.")
                elif pageable and s["last_alert"]:
                    last = dt.datetime.fromisoformat(s["last_alert"])
                    if (now - last).total_seconds() >= config.REMIND_EVERY_S:
                        # Acknowledged means someone is on it: no more
                        # reminders, but it stays open until it is fixed. A
                        # state that can't be read (None) still reminds: a
                        # reminder too many beats a broken app nobody hears of.
                        state = _keep(store.incident_state, inc) if inc else None
                        if state in (None, "open"):
                            s["last_alert"] = _stamp(now)
                            remind.append(c)
                            if inc:
                                _keep(store.add_event, inc, t, "notified", "Reminder sent: still failing.")
                elif silence is not None and inc and not s.get("silence_noted"):
                    s["silence_noted"] = True
                    _keep(store.add_event, inc, t, "silenced",
                          f"No push: silenced until {silence['ends_at']} UTC ({silence['reason']}).")
            s["summary"] = c["summary"]
        else:
            if s.get("incident"):
                _keep(store.resolve, s["incident"], t, f"Recovered: {c['summary']}")
            if s["alerted"]:
                recovered.append(c)
            s.update({"fails": 0, "alerted": False, "last_alert": None, "incident": None,
                      "started": None, "summary": None, "silence_noted": False})
        _streaks[c["id"]] = s
    # A check that this run no longer reports - a folder taken out of
    # drive_backups.txt while its backup was overdue, a tablet retired from
    # the tailnet - can never recover on its own: nothing would ever reset
    # its streak, so its incident would stay open and the icon's badge would
    # count it for ever. Close it out here. No push for it: the check was
    # not fixed, it was taken away, and "fixed" would be a lie on the lock
    # screen; the next push or the next open of the dashboard sets the badge
    # right. Dropping the streak also keeps state.json from growing with
    # every device that ever appeared on the tailnet.
    #
    # Not on the first run it is missing, though. A check can be absent for
    # a run without having been removed: when `tailscale status` itself
    # fails, ts.serve and ts.cert are not reported at all, and closing an
    # open ts.serve incident during a Tailscale blip - then paging for it
    # again three runs after Tailscale came back - would turn one problem
    # into a flapping incident log. So a missing check keeps its streak for
    # GONE_AFTER_S first; a retired device or a removed folder is gone for
    # good, so waiting a day costs nothing there.
    seen = {c["id"] for c in results}
    for cid in [k for k in _streaks if k not in seen]:
        s = _streaks[cid]
        if not s.get("missing_since"):
            s["missing_since"] = _stamp(now)
            continue
        if (now - dt.datetime.fromisoformat(s["missing_since"])).total_seconds() < GONE_AFTER_S:
            continue
        gone = _streaks.pop(cid)
        if gone.get("incident"):
            _keep(store.resolve, gone["incident"], t,
                  "No longer checked: the check was removed from the monitor's configuration.")
    del _events[:-MAX_EVENTS]
    return {"new": new, "remind": remind, "recovered": recovered}


def compose(decision: dict) -> List[tuple]:
    """(title, body, tag) for each notification to send. Problems and
    recoveries go separately: one notification saying both "Boord is down"
    and "Kudde is back" reads as neither."""
    out = []
    place = _place()
    problems = decision["new"] + decision["remind"]
    if problems:
        if len(problems) == 1:
            c = problems[0]
            again = " (still)" if decision["remind"] else ""
            out.append((f"{place}: {c['label']} - problem{again}", c["summary"], "problem-" + c["id"]))
        else:
            body = "\n".join(f"{c['label']}: {c['summary']}" for c in problems)
            out.append((f"{place}: {len(problems)} problems", body, "problems"))
    if decision["recovered"]:
        names = ", ".join(c["label"] for c in decision["recovered"])
        out.append((f"{place}: back to normal", f"{names}: fixed.", "recovered"))
    return out


def _daily_due(local: dt.datetime) -> bool:
    """Whether the morning summary should go out now: past DAILY_AT on the
    PC's clock, not yet sent today, and not more than DAILY_LATE_S late."""
    if config.ROLE != "farm" or config.DAILY_AT in ("", "off", "no", "none"):
        return False
    try:
        hour, minute = (int(x) for x in config.DAILY_AT.split(":"))
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        log.warning("MONITOR_DAILY_AT=%r is not HH:MM - no morning summary", config.DAILY_AT)
        return False
    return (_daily_last != local.date().isoformat()
            and 0 <= (local - start).total_seconds() < DAILY_LATE_S)


def compose_daily(results: List[dict], incidents: Optional[List[dict]] = None) -> tuple:
    """(title, body) of the morning summary. Short lines, most important
    first: a lock screen shows three or four of them."""
    fails = [c for c in results if c["status"] == "fail"]
    warns = [c for c in results if c["status"] == "warn"]
    if fails:
        title = f"Farm: {len(fails)} problem{'s' if len(fails) > 1 else ''}"
    elif warns:
        title = f"Farm: all up, {len(warns)} to check"
    else:
        title = "Farm: all good"

    lines = []
    if fails:
        lines.append("Problem: " + ", ".join(c["short"] for c in fails))
    if warns:
        lines.append("Check: " + ", ".join(c["short"] for c in warns))
    apps_ = [c for c in results if c["group"] == "apps" and c["status"] != "off"]
    if apps_:
        up = sum(1 for c in apps_ if c["status"] != "fail")
        lines.append(f"Apps: {up} of {len(apps_)} up")
    devices = [c for c in results if c["group"] == "devices" and c.get("online") is not None]
    if devices:
        line = f"Devices: {sum(1 for c in devices if c['online'])} of {len(devices)} online"
        waiting = [c for c in devices if c.get("queue")]
        if waiting:
            total = sum(c["queue"] for c in waiting)
            line += f", {total} crate{'s' if total > 1 else ''} waiting on " + \
                ", ".join(c["short"] for c in waiting)
        lines.append(line)
    folders = [c for c in results if c["id"].startswith("drive.folder.") and c.get("value")]
    if folders:
        lines.append("Backups: " + ", ".join(f"{c['short']} {c['value']}" for c in folders))
    restarts = next((c for c in results if c["id"] == "guards.log"), None)
    if restarts and restarts.get("value") not in (None, "0x"):
        lines.append(f"Watchdog restarts (24 h): {restarts['value'].rstrip('x')}")
    update = next((c for c in results if c["id"] == "guards.update"), None)
    if update and update["status"] == "warn":
        lines.append("Windows wants to restart the PC")
    if incidents:
        acked = sum(1 for i in incidents if i["state"] == "acknowledged")
        lines.append(f"Open incidents: {len(incidents)}" + (f" ({acked} acknowledged)" if acked else ""))
    return title, "\n".join(lines)


def _refresh_bundle(sys: System) -> None:
    """Remote role: copy the farm's push key and subscription list, hourly,
    while the farm can still be reached. The copy is what lets the watcher
    alert the very phones the farm knows about at the moment the farm is
    gone."""
    global _last_bundle
    if time.time() - _last_bundle < 3600 or not config.FARM_URL:
        return
    token = os.environ.get("MONITOR_REMOTE_TOKEN", "").strip()
    if not token and os.path.exists(config.REMOTE_TOKEN_FILE):
        with open(config.REMOTE_TOKEN_FILE, encoding="ascii") as fh:
            token = fh.read().strip()
    if not token:
        return
    p = sys.http(config.FARM_URL + "/api/remote/bundle", timeout=15, want_json=True,
                 headers={"X-Monitor-Token": token})
    if isinstance(p.body, dict) and p.body.get("key_pem"):
        push.replace_all(p.body["key_pem"], p.body.get("subscriptions") or [])
        _last_bundle = time.time()
    elif p.answered:
        log.warning("farm refused the remote bundle (HTTP %s) - check MONITOR_REMOTE_TOKEN", p.status)


def run_once(sys: System) -> dict:
    """One full pass. Serialised: the "Check now" button and the loop must
    not run the PowerShell queries twice at once."""
    global latest, _last_farm
    with _run_lock:
        started = time.monotonic()
        now = sys.now()
        farm = None
        if config.ROLE == "farm":
            results = checks.farm_checks(sys)
        else:
            results, farm = checks.remote_checks(sys, _last_farm)
            if farm is not None:
                _last_farm = farm
                _refresh_bundle(sys)
        t = store.ts(now)
        try:
            store.record(results, t)
        except Exception:  # noqa: BLE001 - history is a nice-to-have; alerts are not
            log.exception("could not record history")
        _count_latency(results)
        with _lock:
            if not _streaks and not _events:
                _load_state()
            try:
                active_silences = store.silences(t)
            except Exception:  # noqa: BLE001
                active_silences = []
            decision = evaluate(results, now, active_silences)
            # The number on the Home Screen icon: problems a phone has been
            # told about and that are still broken. The same rule as the
            # pushes, so the badge never counts a warning or a fail still
            # inside its FAIL_AFTER grace.
            alerting = sum(1 for s in _streaks.values() if s["alerted"])
            snapshot = {"role": config.ROLE, "checked_at": _stamp(now),
                        "overall": checks.worst(results), "groups": checks.GROUPS[config.ROLE],
                        "alerting": alerting, "checks": results,
                        "events": list(reversed(_events[-50:])),
                        "incidents": _safe(lambda: store.incidents(active_only=True), []),
                        "silences": active_silences,
                        "interval_s": config.CHECK_INTERVAL_S}
            if config.ROLE == "remote":
                snapshot["farm"] = _last_farm
                snapshot["farm_url"] = config.FARM_URL or None
            daily = None
            local = sys.local_now()
            if _daily_due(local):
                global _daily_last
                _daily_last = local.date().isoformat()
                daily = compose_daily(results, snapshot["incidents"])
            latest = snapshot
            _save_state()
        stats["runs"] += 1
        stats["last_run_s"] = time.monotonic() - started
        stats["last_run_at"] = time.time()
    if daily:
        try:
            result = push.send(daily[0], daily[1], url=_alert_url(), tag="daily", badge=alerting)
            log.info("morning summary %r: %s", daily[0], result)
        except Exception as e:  # noqa: BLE001 - never let a push kill the loop
            log.warning("morning summary failed: %s", e)
    for title, body, tag in compose(decision):
        try:
            result = push.send(title, body, url=_alert_url(), tag=tag, badge=alerting)
            stats["pushes"] += result.get("sent", 0)
            stats["push_failures"] += result.get("failed", 0)
            log.info("pushed %r: %s", title, result)
        except Exception as e:  # noqa: BLE001 - never let a push kill the loop
            stats["push_failures"] += 1
            log.warning("push %r failed: %s", title, e)
    return snapshot


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a broken history file must not blank the dashboard
        log.exception("history read failed")
        return default


def _count_latency(results: List[dict]) -> None:
    for c in results:
        if c.get("group") != "apps" or not isinstance(c.get("local_ms"), int):
            continue
        h = latency_hist.setdefault(c["app"], {"buckets": [0] * (len(store.BUCKETS_MS) + 1),
                                               "sum_ms": 0, "count": 0})
        h["buckets"][store._bucket(c["local_ms"])] += 1
        h["sum_ms"] += c["local_ms"]
        h["count"] += 1


def _loop() -> None:
    sys = System()
    while True:
        try:
            run_once(sys)
        except Exception:  # noqa: BLE001 - the loop is the product; it must survive
            stats["run_errors"] += 1
            log.exception("check run failed")
        time.sleep(config.CHECK_INTERVAL_S)


def start() -> None:
    if os.environ.get("MONITOR_NO_LOOP"):
        return
    threading.Thread(target=_loop, name="monitor-loop", daemon=True).start()
