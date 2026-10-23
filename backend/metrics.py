"""GET /metrics: everything the monitor knows, in the Prometheus text
exposition format (version 0.0.4), the de facto standard every monitoring
tool reads - Prometheus itself, Grafana Agent/Alloy, VictoriaMetrics,
Uptime Kuma, Netdata, Datadog's OpenMetrics check.

Nothing on the farm scrapes it today. It is there so that the day someone
wants Grafana dashboards, or the farm's figures in a tool the owner already
uses, it is one line of scrape config and not a project. Written by hand
rather than with prometheus_client: it is a page of text, and every new
package is one more thing update_monitor_server.bat has to install on a PC
nobody administers.

Naming follows Prometheus' conventions: a `boord_` prefix, base units
(seconds, bytes, ratios 0-1), `_total` on counters, and labels rather than
names for what varies (app, check, group).

Open to the tailnet like every other read; it says nothing the dashboard
does not.
"""
import time
from typing import Dict, List, Optional

import apps
import config
import monitor
import slo
import store

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_STATUS_CODE = {"ok": 0, "warn": 1, "fail": 2, "off": -1, "unknown": -2}


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Out:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self._seen = set()

    def family(self, name: str, kind: str, help_: str) -> None:
        if name in self._seen:
            return
        self._seen.add(name)
        self.lines.append(f"# HELP {name} {help_}")
        self.lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value: Optional[float], labels: Optional[Dict[str, object]] = None) -> None:
        if value is None:
            return
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, float):
            text = "+Inf" if value == float("inf") else repr(round(value, 6))
        else:
            text = str(value)
        self.lines.append(f"{name}{lab} {text}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render() -> str:
    o = _Out()
    snap = monitor.latest or {}
    results = snap.get("checks") or []
    now = time.time()

    o.family("boord_monitor_info", "gauge", "Build and role of this monitor; always 1.")
    o.sample("boord_monitor_info", 1, {"role": config.ROLE})

    o.family("boord_monitor_runs_total", "counter", "Check runs completed since the monitor started.")
    o.sample("boord_monitor_runs_total", monitor.stats["runs"])
    o.family("boord_monitor_run_errors_total", "counter", "Check runs that raised an error.")
    o.sample("boord_monitor_run_errors_total", monitor.stats["run_errors"])
    o.family("boord_monitor_run_duration_seconds", "gauge", "How long the last check run took.")
    o.sample("boord_monitor_run_duration_seconds", float(monitor.stats["last_run_s"]))
    o.family("boord_monitor_last_run_timestamp_seconds", "gauge", "Unix time the last check run finished.")
    o.sample("boord_monitor_last_run_timestamp_seconds", float(monitor.stats["last_run_at"]) or None)
    o.family("boord_monitor_check_interval_seconds", "gauge", "Seconds between check runs.")
    o.sample("boord_monitor_check_interval_seconds", config.CHECK_INTERVAL_S)
    o.family("boord_push_sent_total", "counter", "Push notifications delivered to a push service.")
    o.sample("boord_push_sent_total", monitor.stats["pushes"])
    o.family("boord_push_failed_total", "counter", "Push notifications a push service refused.")
    o.sample("boord_push_failed_total", monitor.stats["push_failures"])

    # Every check, as a status code and a one-hot state set: the code is
    # easy to graph, the state set is easy to alert on (== 1).
    o.family("boord_check_status", "gauge", "Check status: 0 ok, 1 warn, 2 fail, -1 off, -2 unknown.")
    for c in results:
        o.sample("boord_check_status", _STATUS_CODE.get(c["status"], -2),
                 {"check": c["id"], "group": c.get("group", ""), "label": c.get("label", "")})
    o.family("boord_check_state", "gauge", "1 for the check's current state, 0 for the others.")
    for c in results:
        for st in ("ok", "warn", "fail", "off", "unknown"):
            o.sample("boord_check_state", 1 if c["status"] == st else 0, {"check": c["id"], "state": st})

    # The apps: up, answer time, HTTP code - the RED method's Errors and
    # Duration (Rate is the monitor's own, one a minute).
    o.family("boord_app_up", "gauge", "1 if the app answered on this PC at the last check (any HTTP status).")
    o.family("boord_app_response_seconds", "gauge", "The app's answer time on this PC at the last check.")
    o.family("boord_app_http_status", "gauge", "The HTTP status the app answered with.")
    o.family("boord_app_tailnet_reachable", "gauge", "1 if the app answered over Tailscale.")
    for c in results:
        if c.get("group") != "apps" or c["status"] == "off":
            continue
        lab = {"app": c.get("app", c["id"]), "port": c.get("port", "")}
        o.sample("boord_app_up", 1 if c.get("local_status") else 0, lab)
        if c.get("local_ms") is not None:
            o.sample("boord_app_response_seconds", c["local_ms"] / 1000.0, lab)
        o.sample("boord_app_http_status", c.get("local_status"), lab)
        if c.get("tailnet") is not None:
            o.sample("boord_app_tailnet_reachable", 1 if c["tailnet"] else 0, lab)

    o.family("boord_app_request_duration_seconds", "histogram",
             "The app's answer times since the monitor started.")
    for app_key, h in sorted(monitor.latency_hist.items()):
        cum = 0
        for i, upper in enumerate(store.BUCKETS_MS):
            cum += h["buckets"][i]
            o.sample("boord_app_request_duration_seconds_bucket", cum, {"app": app_key, "le": upper / 1000.0})
        o.sample("boord_app_request_duration_seconds_bucket", h["count"], {"app": app_key, "le": "+Inf"})
        o.sample("boord_app_request_duration_seconds_sum", h["sum_ms"] / 1000.0, {"app": app_key})
        o.sample("boord_app_request_duration_seconds_count", h["count"], {"app": app_key})

    # The PC: the USE method's Utilisation and Saturation for what matters
    # here - disk (SQLite and backups need room) and memory.
    by_id = {c["id"]: c for c in results}
    disk = by_id.get("pc.disk") or {}
    o.family("boord_disk_free_bytes", "gauge", "Free space on the apps' drive.")
    o.sample("boord_disk_free_bytes", disk.get("bytes_free"))
    o.family("boord_disk_size_bytes", "gauge", "Size of the apps' drive.")
    o.sample("boord_disk_size_bytes", disk.get("bytes_total"))
    mem = by_id.get("pc.memory") or {}
    o.family("boord_memory_available_bytes", "gauge", "Physical memory available.")
    o.sample("boord_memory_available_bytes", mem.get("bytes_free"))
    o.family("boord_memory_total_bytes", "gauge", "Physical memory installed.")
    o.sample("boord_memory_total_bytes", mem.get("bytes_total"))
    up = by_id.get("pc.uptime") or {}
    o.family("boord_host_uptime_seconds", "gauge", "Seconds since the PC booted.")
    o.sample("boord_host_uptime_seconds", up.get("uptime_s"))

    o.family("boord_tailscale_key_expiry_days", "gauge", "Days until this PC's Tailscale node key expires.")
    o.sample("boord_tailscale_key_expiry_days", (by_id.get("ts.key") or {}).get("days_left"))
    o.family("boord_tls_cert_expiry_days", "gauge", "Days until the tailnet HTTPS certificate expires.")
    o.sample("boord_tls_cert_expiry_days", (by_id.get("ts.cert") or {}).get("days_left"))
    o.family("boord_backup_age_seconds", "gauge", "Age of the newest file in each watched backup folder.")
    for c in results:
        if c["id"].startswith("drive.folder.") and c.get("age_s") is not None:
            o.sample("boord_backup_age_seconds", c["age_s"], {"folder": c.get("label", c["id"])})
    o.family("boord_task_last_run_age_seconds", "gauge", "Seconds since a safety-net scheduled task last ran.")
    for cid, task in (("guards.watchdog", apps.WATCHDOG_TASK), ("guards.heartbeat", apps.HEARTBEAT_TASK)):
        o.sample("boord_task_last_run_age_seconds", (by_id.get(cid) or {}).get("age_s"), {"task": task})

    o.family("boord_device_online", "gauge", "1 if the tailnet device is online.")
    o.family("boord_device_sync_queue", "gauge", "Records a Boord tablet reports it still holds unsent.")
    for c in results:
        if c.get("group") == "devices" and c.get("online") is not None:
            o.sample("boord_device_online", 1 if c["online"] else 0, {"device": c.get("label", c["id"])})
            o.sample("boord_device_sync_queue", c.get("queue") or 0, {"device": c.get("label", c["id"])})

    # Incidents and SLOs, from the history.
    try:
        active = store.incidents(active_only=True)
        o.family("boord_incidents_open", "gauge", "Incidents not yet resolved, by state.")
        for st in ("open", "acknowledged"):
            o.sample("boord_incidents_open", sum(1 for i in active if i["state"] == st), {"state": st})
        o.family("boord_silences_active", "gauge", "Maintenance windows in force.")
        o.sample("boord_silences_active", len(store.silences(int(now))))
        installed = [c["app"] for c in results if c.get("group") == "apps" and c["status"] != "off"]
        now_dt = store.utc(int(now))
        o.family("boord_slo_target_ratio", "gauge", "The availability objective, 0-1.")
        o.family("boord_slo_availability_ratio", "gauge", "Measured availability over a window, 0-1.")
        o.family("boord_slo_error_budget_remaining_ratio", "gauge",
                 "Share of the window's error budget not yet spent; negative once the SLO is missed.")
        o.family("boord_slo_burn_rate", "gauge", "Error budget burn rate over a window (1 = on pace).")
        o.family("boord_slo_latency_p95_seconds", "gauge", "95th percentile answer time over a window.")
        for s in slo.overview(now_dt, installed if results else None):
            lab = {"app": s["app"]}
            o.sample("boord_slo_target_ratio", s["target"] / 100.0, lab)
            for w, v in s["availability"].items():
                o.sample("boord_slo_availability_ratio", v, {**lab, "window": w})
            o.sample("boord_slo_error_budget_remaining_ratio", s["error_budget"]["remaining"], lab)
            for w, v in s["burn_rate"].items():
                o.sample("boord_slo_burn_rate", v, {**lab, "window": w})
            for w in ("24h", "7d"):
                p95 = s["latency"][w]["p95"]
                o.sample("boord_slo_latency_p95_seconds", p95 / 1000.0 if p95 is not None else None,
                         {**lab, "window": w})
    except Exception:  # noqa: BLE001 - the live figures above are still worth serving
        monitor.log.exception("metrics: history unavailable")
    return o.text()
