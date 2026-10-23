"""Service level objectives for the five apps: how available and how quick
each one is meant to be, and how it is doing against that.

The vocabulary is the standard one from Google's SRE books, because it is
what every monitoring tool and every article about one uses:

  SLI   - the measurement. Availability: the share of the monitor's
          once-a-minute checks in which the app answered (any HTTP answer,
          watchdog.ps1's rule; "fail" is bad, "off" and "unknown" are left
          out). Latency: the app's answer time on this PC, as percentiles.
  SLO   - the target for the SLI over a rolling window: 99.5% over 30 days
          unless data/slo.txt says otherwise.
  Error budget - what the target allows to go wrong. 99.5% of 30 days is
          3 h 36 min of downtime; "budget remaining" is how much of that is
          still unspent.
  Burn rate - how fast the budget is being spent right now, against the
          pace that would spend exactly all of it in the window. 1x is on
          pace; 14.4x over the last hour is the classic "page now" line
          (two percent of a month's budget in an hour).

None of this pushes. Pages stay what they were: an app that fails three
checks in a row. The SLO view is for the owner looking back - which app
needs work before next season - not for waking anyone.

data/slo.txt, optional, one line per app, read on every request:
    <app key> | <availability %> | <p95 latency ms>
e.g.
    boord  | 99.9 | 500
    kudde  | 99   |
A blank field keeps the default.
"""
import datetime as dt
import os
from typing import Dict, List, Optional

import apps
import checks
import config
import store

DEFAULT_TARGET = float(os.environ.get("MONITOR_SLO_TARGET", "99.5"))
DEFAULT_LATENCY_MS = int(os.environ.get("MONITOR_SLO_LATENCY_MS", "1000"))
WINDOW_DAYS = int(os.environ.get("MONITOR_SLO_WINDOW_DAYS", "30"))
SLO_FILE = os.path.join(config.DATA_DIR, "slo.txt")

# The multiwindow burn-rate lines from the SRE workbook: fast enough to be
# an outage now, or steady enough to spend the month's budget early.
FAST_BURN = 14.4
SLOW_BURN = 6.0


def targets() -> Dict[str, tuple]:
    """{app key: (availability %, p95 ms)}."""
    out = {a.key: (DEFAULT_TARGET, DEFAULT_LATENCY_MS) for a in apps.APPS}
    for f in checks._config_lines(SLO_FILE) or []:
        key = f[0].lower()
        if key not in out:
            continue
        target, lat = out[key]
        try:
            if len(f) > 1 and f[1]:
                target = min(99.999, max(50.0, float(f[1].rstrip("%"))))
        except ValueError:
            pass
        try:
            if len(f) > 2 and f[2]:
                lat = max(1, int(float(f[2])))
        except ValueError:
            pass
        out[key] = (target, lat)
    return out


def _burn(check_id: str, now: int, seconds: int, target: float) -> Optional[float]:
    a = store.availability(store.counts(check_id, now - seconds, now))
    if a is None:
        return None
    allowed = 1 - target / 100
    return (1 - a) / allowed if allowed > 0 else None


def app_slo(app: apps.App, now: dt.datetime, target: float, latency_ms: int) -> dict:
    cid = "app." + app.key
    t = store.ts(now)
    window = WINDOW_DAYS * 86400
    c30 = store.counts(cid, t - window, t)
    a30 = store.availability(c30)
    allowed = 1 - target / 100
    counted = c30["ok"] + c30["warn"] + c30["fail"]
    # Budget in runs: of the counted runs, how many may fail. Remaining is
    # the share not yet used; it goes negative once the SLO is missed, and
    # the dashboard shows that as "over budget" rather than clamping it.
    budget_runs = counted * allowed
    remaining = (1 - c30["fail"] / budget_runs) if budget_runs else None
    # Coverage: how much of the window the monitor actually watched. A
    # monitor that was off for a week has a 30-day number built on 23 days,
    # and whoever reads it should know.
    expected = window / max(1, config.CHECK_INTERVAL_S)
    lat24 = store.latency(cid, t - 86400, t)
    lat7 = store.latency(cid, t - 7 * 86400, t)
    return {
        "app": app.key, "check_id": cid, "name": app.name,
        "target": target, "window_days": WINDOW_DAYS,
        "availability": {
            "24h": store.availability(store.counts(cid, t - 86400, t)),
            "7d": store.availability(store.counts(cid, t - 7 * 86400, t)),
            "30d": a30,
        },
        # In time, over the time actually watched, so the three numbers
        # agree with each other: remaining = 1 - spent / allowed. Over a
        # full window that watched every minute, allowed is the familiar
        # 3 h 36 min of a 99.5% month.
        "error_budget": {
            "allowed_s": budget_runs * config.CHECK_INTERVAL_S,
            "spent_s": c30["fail"] * config.CHECK_INTERVAL_S,
            "remaining": remaining,
        },
        "burn_rate": {"1h": _burn(cid, t, 3600, target), "6h": _burn(cid, t, 6 * 3600, target)},
        "latency": {"objective_ms": latency_ms, "24h": lat24, "7d": lat7,
                    "met": (lat7["p95"] <= latency_ms) if lat7["p95"] is not None else None},
        "coverage": min(1.0, (counted + c30["other"]) / expected) if expected else None,
        "samples": counted,
    }


def overview(now: dt.datetime, installed: Optional[List[str]] = None) -> List[dict]:
    """Every installed app's SLO. `installed` is the app keys the latest run
    did not find "off"; None means all."""
    tgt = targets()
    return [app_slo(a, now, *tgt[a.key]) for a in apps.APPS if installed is None or a.key in installed]


def status_of(s: dict) -> str:
    """One word for an SLO, for the colour of its card: met, at risk
    (burning fast, or under a quarter of the budget left) or missed."""
    a = s["availability"]["30d"]
    if a is None:
        return "nodata"
    if a * 100 < s["target"]:
        return "missed"
    rem = s["error_budget"]["remaining"]
    burn = s["burn_rate"]["1h"]
    if (rem is not None and rem < 0.25) or (burn is not None and burn >= FAST_BURN):
        return "at_risk"
    return "met"
