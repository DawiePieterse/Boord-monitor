"""The monitor's memory: every check's result over time, the incidents it
opened, and the maintenance windows (silences) someone set. One SQLite file,
data/history.db.

Before 2.0 the monitor only knew the current minute and the last 200 status
changes. That answers "is it broken now?" but not the questions that keep
the apps running smoothly over a season: how often does Boord Labour fall
over, is Owner getting slower week by week, how long does it take us to fix
things, did last night's outage come back. Those are the standard SRE
measures - availability against an objective (SLO), latency percentiles,
incidents with MTTR - and they all need history.

What is kept:
  sample    - every check, every run, for RAW_KEEP_S (two days). Exact
              percentiles and the minute-by-minute charts come from here.
  hourly    - the same rolled up per check per hour, for HOURLY_KEEP_S
              (400 days, so this season can be held against last season).
              Latency is kept as a histogram (Prometheus' buckets idea),
              which is how 7- and 30-day percentiles are worked out without
              keeping every sample.
  incident  - one row per outage, opened when a check has failed FAIL_AFTER
              runs in a row and resolved when it stops failing, with a
              timeline in incident_event.
  silence   - "I am working on the PC until 14:00": checks it matches
              still show red, but no phone is woken.

Size: about 40 checks a minute is ~115k raw rows (two days) and ~380k hourly
rows (400 days) - a few tens of MB at most, on a PC with hundreds of GB.

Timestamps in the database are Unix seconds (UTC). Everything this module
hands out is naive-UTC ISO text, like every other timestamp in the suite,
because the dashboard's parseServerDate expects exactly that.

A connection per call, not one shared: the check loop, the web requests and
the restart thread all come through here, sqlite3 connections do not like
being shared between threads, and opening one costs well under a
millisecond. Writes are serialised with a lock; WAL mode lets the
dashboard read while the loop writes.
"""
import calendar
import datetime as dt
import math
import os
import sqlite3
import threading
from typing import Dict, Iterable, List, Optional

import config

DB_FILE = os.path.join(config.DATA_DIR, "history.db")

RAW_KEEP_S = 2 * 86400
HOURLY_KEEP_S = 400 * 86400

# Upper bounds of the latency histogram's buckets, in ms. The last bucket
# is everything above 10 s. Chosen around what matters on the farm: under
# 100 ms is a healthy local app, a second is slow, five seconds is the
# point app_check already calls a warning.
BUCKETS_MS = [25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
_NB = len(BUCKETS_MS) + 1
_BCOLS = ", ".join(f"b{i}" for i in range(_NB))

_lock = threading.Lock()

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sample (
  check_id TEXT NOT NULL, at INTEGER NOT NULL, status TEXT NOT NULL,
  ms INTEGER, num REAL);
CREATE INDEX IF NOT EXISTS sample_check_at ON sample (check_id, at);
CREATE INDEX IF NOT EXISTS sample_at ON sample (at);

CREATE TABLE IF NOT EXISTS hourly (
  check_id TEXT NOT NULL, hour INTEGER NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0, warn INTEGER NOT NULL DEFAULT 0,
  fail INTEGER NOT NULL DEFAULT 0, other INTEGER NOT NULL DEFAULT 0,
  ms_count INTEGER NOT NULL DEFAULT 0, ms_sum INTEGER NOT NULL DEFAULT 0,
  {", ".join(f"b{i} INTEGER NOT NULL DEFAULT 0" for i in range(_NB))},
  PRIMARY KEY (check_id, hour));

CREATE TABLE IF NOT EXISTS incident (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id TEXT NOT NULL, grp TEXT NOT NULL, label TEXT NOT NULL,
  severity TEXT NOT NULL, state TEXT NOT NULL,
  started_at INTEGER NOT NULL, opened_at INTEGER NOT NULL,
  acked_at INTEGER, acked_by TEXT, resolved_at INTEGER,
  summary TEXT NOT NULL, paged INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS incident_state ON incident (state);
CREATE INDEX IF NOT EXISTS incident_started ON incident (started_at);

CREATE TABLE IF NOT EXISTS incident_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL, at INTEGER NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incident_event_incident ON incident_event (incident_id);

CREATE TABLE IF NOT EXISTS silence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
  reason TEXT NOT NULL, created_at INTEGER NOT NULL);
"""


# --- time ---------------------------------------------------------------------

def ts(t: dt.datetime) -> int:
    """Naive UTC datetime -> Unix seconds."""
    return calendar.timegm(t.timetuple())


_EPOCH = dt.datetime(1970, 1, 1)


def utc(seconds: int) -> dt.datetime:
    """Unix seconds -> naive UTC datetime (utcfromtimestamp without its
    deprecation warning on newer Pythons)."""
    return _EPOCH + dt.timedelta(seconds=int(seconds))


def iso(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    return utc(seconds).isoformat(timespec="seconds")


# --- connection ---------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """A connection to the history file, creating the tables when they are
    not there. Asked on every connection rather than once, because the
    tests clear the data folder between tests - and it is one lookup in
    sqlite_master."""
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='silence'").fetchone() is None:
        with _lock:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            con.commit()
    return con


def writable() -> bool:
    """For /readyz: can the monitor still write its history? A full disk
    or a locked file shows up here before anywhere else."""
    try:
        con = _connect()
        try:
            with _lock:
                con.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
                con.execute("DELETE FROM probe")
                con.commit()
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


# --- recording a run ----------------------------------------------------------

def _bucket(ms: int) -> int:
    for i, upper in enumerate(BUCKETS_MS):
        if ms <= upper:
            return i
    return _NB - 1


def _sample_of(c: dict) -> tuple:
    """(status, ms, num) to keep for one check result. `ms` is an app's own
    answer time on this PC - the latency the SLOs are about. `num` is the
    one number worth drawing for anything else: how full the disk or the
    memory is."""
    ms = c.get("local_ms") if c.get("status") not in ("off", "unknown") else None
    if ms is None and isinstance(c.get("ms"), int):
        ms = c["ms"]
    num = c.get("pct")
    return c["status"], (int(ms) if isinstance(ms, (int, float)) else None), \
        (float(num) if isinstance(num, (int, float)) else None)


_last_prune = 0


def record(results: Iterable[dict], at: int) -> None:
    """Keep one run's results: raw samples, and the hourly rollup updated in
    the same transaction so the two can never disagree."""
    global _last_prune
    hour = at - at % 3600
    rows, hourly = [], []
    for c in results:
        status, ms, num = _sample_of(c)
        rows.append((c["id"], at, status, ms, num))
        b = [0] * _NB
        if ms is not None:
            b[_bucket(ms)] = 1
        hourly.append((c["id"], hour,
                       1 if status == "ok" else 0, 1 if status == "warn" else 0,
                       1 if status == "fail" else 0, 1 if status in ("off", "unknown") else 0,
                       1 if ms is not None else 0, ms or 0, *b))
    adds = ", ".join(f"b{i} = b{i} + excluded.b{i}" for i in range(_NB))
    con = _connect()
    try:
        with _lock:
            con.executemany("INSERT INTO sample (check_id, at, status, ms, num) VALUES (?,?,?,?,?)", rows)
            con.executemany(
                f"INSERT INTO hourly (check_id, hour, ok, warn, fail, other, ms_count, ms_sum, {_BCOLS}) "
                f"VALUES ({', '.join('?' * (8 + _NB))}) "
                "ON CONFLICT (check_id, hour) DO UPDATE SET ok = ok + excluded.ok, "
                "warn = warn + excluded.warn, fail = fail + excluded.fail, other = other + excluded.other, "
                "ms_count = ms_count + excluded.ms_count, ms_sum = ms_sum + excluded.ms_sum, " + adds,
                hourly)
            # Pruning once an hour is plenty, and keeps the DELETE off most runs.
            if at - _last_prune >= 3600:
                con.execute("DELETE FROM sample WHERE at < ?", (at - RAW_KEEP_S,))
                con.execute("DELETE FROM hourly WHERE hour < ?", (at - HOURLY_KEEP_S,))
                con.execute("DELETE FROM silence WHERE ends_at < ?", (at - 30 * 86400,))
                _last_prune = at
            con.commit()
    finally:
        con.close()


# --- reading history ----------------------------------------------------------

def counts(check_id: str, since: int, until: int) -> dict:
    """{ok, warn, fail, other} between two times: from the raw samples when
    the window is short enough to still have them, otherwise from the
    hourly rollup (to the hour)."""
    con = _connect()
    try:
        if until - since <= RAW_KEEP_S:
            rows = con.execute("SELECT status, COUNT(*) n FROM sample WHERE check_id = ? AND at >= ? "
                               "AND at <= ? GROUP BY status", (check_id, since, until)).fetchall()
            out = {"ok": 0, "warn": 0, "fail": 0, "other": 0}
            for r in rows:
                out[r["status"] if r["status"] in out else "other"] += r["n"]
            return out
        r = con.execute("SELECT COALESCE(SUM(ok),0) ok, COALESCE(SUM(warn),0) warn, "
                        "COALESCE(SUM(fail),0) fail, COALESCE(SUM(other),0) other FROM hourly "
                        "WHERE check_id = ? AND hour >= ? AND hour <= ?",
                        (check_id, since - since % 3600, until)).fetchone()
        return dict(r)
    finally:
        con.close()


def availability(c: dict) -> Optional[float]:
    """Good runs over counted runs, as a fraction; None with nothing counted.
    A warning counts as up - a slow or 500-ing app is still answering, the
    same rule as watchdog.ps1 - and "off" or "unknown" counts as neither,
    because a check that could not run says nothing either way."""
    counted = c["ok"] + c["warn"] + c["fail"]
    return (c["ok"] + c["warn"]) / counted if counted else None


def _quantile_exact(values: List[int], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    # Nearest rank, the definition most monitoring tools document.
    return float(values[max(0, math.ceil(q * len(values)) - 1)])


def _quantile_hist(buckets: List[int], q: float) -> Optional[float]:
    """Prometheus' histogram_quantile: find the bucket the q-th observation
    falls in and interpolate linearly inside it. The open-ended last bucket
    answers with the highest finite bound, as Prometheus does."""
    total = sum(buckets)
    if not total:
        return None
    rank = q * total
    seen = 0
    for i, n in enumerate(buckets):
        if seen + n >= rank and n:
            if i == _NB - 1:
                return float(BUCKETS_MS[-1])
            lower = BUCKETS_MS[i - 1] if i else 0
            upper = BUCKETS_MS[i]
            return lower + (upper - lower) * (rank - seen) / n
        seen += n
    return float(BUCKETS_MS[-1])


def latency(check_id: str, since: int, until: int) -> dict:
    """{p50, p95, p99, avg, n} in ms over a window, None for each when the
    check recorded no answer times."""
    con = _connect()
    try:
        if until - since <= RAW_KEEP_S:
            ms = [r[0] for r in con.execute(
                "SELECT ms FROM sample WHERE check_id = ? AND at >= ? AND at <= ? AND ms IS NOT NULL",
                (check_id, since, until))]
            return {"p50": _quantile_exact(ms, .5), "p95": _quantile_exact(ms, .95),
                    "p99": _quantile_exact(ms, .99), "avg": (sum(ms) / len(ms)) if ms else None,
                    "n": len(ms), "exact": True}
        r = con.execute("SELECT COALESCE(SUM(ms_count),0) n, COALESCE(SUM(ms_sum),0) s, "
                        + ", ".join(f"COALESCE(SUM(b{i}),0) b{i}" for i in range(_NB))
                        + " FROM hourly WHERE check_id = ? AND hour >= ? AND hour <= ?",
                        (check_id, since - since % 3600, until)).fetchone()
        b = [r[f"b{i}"] for i in range(_NB)]
        return {"p50": _quantile_hist(b, .5), "p95": _quantile_hist(b, .95), "p99": _quantile_hist(b, .99),
                "avg": (r["s"] / r["n"]) if r["n"] else None, "n": r["n"], "exact": False}
    finally:
        con.close()


_RANK = {"fail": 3, "warn": 2, "ok": 1}


def series(check_id: str, since: int, until: int, step: int) -> List[dict]:
    """The check bucketed into `step`-second slots, oldest first, for a
    chart: each slot's worst status, mean answer time and mean value. A slot
    with no samples is left out; the chart shows the gap."""
    con = _connect()
    try:
        rows = con.execute("SELECT at, status, ms, num FROM sample WHERE check_id = ? AND at >= ? "
                           "AND at <= ? ORDER BY at", (check_id, since, until)).fetchall()
    finally:
        con.close()
    slots: Dict[int, dict] = {}
    for r in rows:
        k = r["at"] - (r["at"] - since) % step
        s = slots.setdefault(k, {"worst": None, "ms": [], "num": []})
        if _RANK.get(r["status"], 0) > _RANK.get(s["worst"], 0):
            s["worst"] = r["status"]
        elif s["worst"] is None:
            s["worst"] = r["status"]
        if r["ms"] is not None:
            s["ms"].append(r["ms"])
        if r["num"] is not None:
            s["num"].append(r["num"])
    return [{"at": iso(k), "status": s["worst"],
             "ms": round(sum(s["ms"]) / len(s["ms"])) if s["ms"] else None,
             "num": round(sum(s["num"]) / len(s["num"]), 1) if s["num"] else None}
            for k, s in sorted(slots.items())]


def daily(check_ids: List[str], days: int, until: int, offset_s: int = 0) -> Dict[str, List[dict]]:
    """{check id: [{day, availability, fail, counted}, ...]} for the last
    `days` days, oldest first - the status page's uptime bars. Days follow
    the farm PC's own calendar (offset_s from UTC), because "Tuesday's
    outage" means the farm's Tuesday. A day with nothing recorded has
    availability None and is drawn grey: no data is not the same as up."""
    local_until = until + offset_s
    first_day = local_until - local_until % 86400 - (days - 1) * 86400  # local midnight, as if UTC
    since = first_day - offset_s
    out: Dict[str, List[dict]] = {}
    if not check_ids:
        return out
    con = _connect()
    try:
        marks = ",".join("?" * len(check_ids))
        rows = con.execute(f"SELECT check_id, hour, ok, warn, fail FROM hourly WHERE check_id IN ({marks}) "
                           "AND hour >= ? AND hour <= ?", (*check_ids, since - since % 3600, until)).fetchall()
    finally:
        con.close()
    acc: Dict[str, Dict[int, list]] = {cid: {} for cid in check_ids}
    for r in rows:
        d = (r["hour"] + offset_s - first_day) // 86400
        if 0 <= d < days:
            a = acc[r["check_id"]].setdefault(d, [0, 0])
            a[0] += r["ok"] + r["warn"]
            a[1] += r["fail"]
    for cid in check_ids:
        bars = []
        for d in range(days):
            good, bad = acc[cid].get(d, [0, 0])
            n = good + bad
            day = utc(first_day + d * 86400).strftime("%Y-%m-%d")
            bars.append({"day": day, "availability": (good / n) if n else None, "fail": bad, "counted": n})
        out[cid] = bars
    return out


# --- incidents ----------------------------------------------------------------

def _incident_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "check_id": r["check_id"], "group": r["grp"], "label": r["label"],
            "severity": r["severity"], "state": r["state"], "summary": r["summary"],
            "paged": bool(r["paged"]), "started_at": iso(r["started_at"]),
            "opened_at": iso(r["opened_at"]), "acked_at": iso(r["acked_at"]),
            "acked_by": r["acked_by"], "resolved_at": iso(r["resolved_at"]),
            "duration_s": (r["resolved_at"] - r["started_at"]) if r["resolved_at"] else None}


def open_incident(c: dict, started: int, opened: int, severity: str = "critical") -> int:
    con = _connect()
    try:
        with _lock:
            cur = con.execute(
                "INSERT INTO incident (check_id, grp, label, severity, state, started_at, opened_at, summary) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (c["id"], c.get("group") or "", c.get("label") or c["id"], severity, "open",
                 started, opened, c.get("summary") or ""))
            iid = cur.lastrowid
            con.execute("INSERT INTO incident_event (incident_id, at, kind, text) VALUES (?,?,?,?)",
                        (iid, started, "detected", "First failed check: " + (c.get("summary") or "")))
            con.execute("INSERT INTO incident_event (incident_id, at, kind, text) VALUES (?,?,?,?)",
                        (iid, opened, "opened", f"Still failing after {config.FAIL_AFTER} checks in a row."))
            con.commit()
        return iid
    finally:
        con.close()


def add_event(incident_id: int, at: int, kind: str, text: str, summary: Optional[str] = None) -> None:
    con = _connect()
    try:
        with _lock:
            con.execute("INSERT INTO incident_event (incident_id, at, kind, text) VALUES (?,?,?,?)",
                        (incident_id, at, kind, text))
            if kind == "notified":
                con.execute("UPDATE incident SET paged = 1 WHERE id = ?", (incident_id,))
            if summary is not None:
                con.execute("UPDATE incident SET summary = ? WHERE id = ?", (summary, incident_id))
            con.commit()
    finally:
        con.close()


def acknowledge(incident_id: int, at: int, by: str) -> Optional[dict]:
    """Someone has seen it and is on it: stops the reminders, as in every
    paging tool. Returns the incident, or None if it is not open."""
    con = _connect()
    try:
        with _lock:
            n = con.execute("UPDATE incident SET state = 'acknowledged', acked_at = ?, acked_by = ? "
                            "WHERE id = ? AND state = 'open'", (at, by, incident_id)).rowcount
            if n:
                con.execute("INSERT INTO incident_event (incident_id, at, kind, text) VALUES (?,?,?,?)",
                            (incident_id, at, "acknowledged", f"Acknowledged by {by}. Reminders stop."))
            con.commit()
        return get_incident(incident_id) if n else None
    finally:
        con.close()


def resolve(incident_id: int, at: int, text: str) -> None:
    con = _connect()
    try:
        with _lock:
            con.execute("UPDATE incident SET state = 'resolved', resolved_at = ? WHERE id = ? "
                        "AND state != 'resolved'", (at, incident_id))
            con.execute("INSERT INTO incident_event (incident_id, at, kind, text) VALUES (?,?,?,?)",
                        (incident_id, at, "resolved", text))
            con.commit()
    finally:
        con.close()


def incident_state(incident_id: int) -> Optional[str]:
    con = _connect()
    try:
        r = con.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()
        return r["state"] if r else None
    finally:
        con.close()


def get_incident(incident_id: int) -> Optional[dict]:
    con = _connect()
    try:
        r = con.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
        if r is None:
            return None
        out = _incident_row(r)
        out["timeline"] = [{"at": iso(e["at"]), "kind": e["kind"], "text": e["text"]} for e in con.execute(
            "SELECT at, kind, text FROM incident_event WHERE incident_id = ? ORDER BY at, id", (incident_id,))]
        return out
    finally:
        con.close()


def incidents(active_only: bool = False, limit: int = 50) -> List[dict]:
    """Newest first. Active ones (open or acknowledged) always come back in
    full; `limit` only cuts the resolved."""
    con = _connect()
    try:
        active = [_incident_row(r) for r in con.execute(
            "SELECT * FROM incident WHERE state != 'resolved' ORDER BY started_at DESC")]
        if active_only:
            return active
        done = [_incident_row(r) for r in con.execute(
            "SELECT * FROM incident WHERE state = 'resolved' ORDER BY started_at DESC LIMIT ?", (limit,))]
        return active + done
    finally:
        con.close()


def incident_stats(since: int, until: int) -> dict:
    """The standard incident measures over a window, from incidents that
    started in it:
      MTTA - mean time to acknowledge, from opening (when a phone was
             first told) to someone tapping Acknowledge;
      MTTR - mean time to resolve, from the first failed check to the
             first good one: how long the farm actually had the problem;
      MTBF - mean time between failures: the window divided by how many
             there were, the way most tools quote it for small numbers.
    Only critical incidents count; there are no others today, but a
    warning-level kind added later must not water these down."""
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM incident WHERE severity = 'critical' AND started_at >= ? "
                           "AND started_at <= ?", (since, until)).fetchall()
    finally:
        con.close()
    resolved = [r["resolved_at"] - r["started_at"] for r in rows if r["resolved_at"]]
    acked = [r["acked_at"] - r["opened_at"] for r in rows if r["acked_at"]]
    return {"count": len(rows), "open": sum(1 for r in rows if r["state"] != "resolved"),
            "mttr_s": (sum(resolved) / len(resolved)) if resolved else None,
            "mtta_s": (sum(acked) / len(acked)) if acked else None,
            "mtbf_s": ((until - since) / len(rows)) if rows else None,
            "downtime_s": sum(resolved)}


# --- silences -----------------------------------------------------------------

def _silence_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "match": r["match"], "starts_at": iso(r["starts_at"]),
            "ends_at": iso(r["ends_at"]), "reason": r["reason"], "created_at": iso(r["created_at"])}


def add_silence(match: str, starts: int, ends: int, reason: str, now: int) -> dict:
    con = _connect()
    try:
        with _lock:
            cur = con.execute("INSERT INTO silence (match, starts_at, ends_at, reason, created_at) "
                              "VALUES (?,?,?,?,?)", (match, starts, ends, reason, now))
            con.commit()
            r = con.execute("SELECT * FROM silence WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _silence_row(r)
    finally:
        con.close()


def end_silence(silence_id: int, now: int) -> bool:
    con = _connect()
    try:
        with _lock:
            n = con.execute("UPDATE silence SET ends_at = ? WHERE id = ? AND ends_at > ?",
                            (now, silence_id, now)).rowcount
            con.commit()
        return bool(n)
    finally:
        con.close()


def silences(now: int, active_only: bool = True) -> List[dict]:
    con = _connect()
    try:
        if active_only:
            rows = con.execute("SELECT * FROM silence WHERE starts_at <= ? AND ends_at > ? ORDER BY ends_at",
                               (now, now)).fetchall()
        else:
            rows = con.execute("SELECT * FROM silence WHERE ends_at > ? ORDER BY starts_at", (now,)).fetchall()
        return [_silence_row(r) for r in rows]
    finally:
        con.close()


def matches(match: str, c: dict) -> bool:
    """A silence's `match`: "*" for everything, "group:<key>" for a whole
    group (group:apps while updating every app), or one check's id."""
    if match == "*":
        return True
    if match.startswith("group:"):
        return c.get("group") == match[6:]
    return c.get("id") == match
