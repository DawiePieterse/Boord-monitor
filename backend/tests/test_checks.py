"""The checks, one farm-PC failure at a time."""
import datetime as dt

import checks
import config
from tests.conftest import DNS, GOOD_SERVE, NOW, by_id, serve_for, ts_status


def test_a_healthy_farm_is_all_ok(fake):
    results = checks.farm_checks(fake)
    bad = [(c["id"], c["status"], c["summary"]) for c in results if c["status"] not in ("ok", "unknown")]
    assert bad == []
    assert checks.worst(results) == "ok"
    boord = by_id(results)["app.boord"]
    assert boord["version"] == "3.15"
    assert boord["tailnet"] is True
    assert boord["url"] == f"https://{DNS}/"
    assert by_id(results)["app.notes"]["url"] == f"https://{DNS}:9443/app/"


def test_a_dead_app_fails(fake):
    fake.down.add(8000)
    c = by_id(checks.farm_checks(fake))["app.boord"]
    assert c["status"] == "fail"
    assert "port 8000" in c["summary"]


def test_an_app_without_its_task_is_off_not_down(fake):
    del fake.tasks["Kudde Server"]
    fake.down.add(8030)
    fake.serve = serve_for({k: v for k, v in GOOD_SERVE.items() if k != 8030})
    results = by_id(checks.farm_checks(fake))
    assert results["app.kudde"]["status"] == "off"
    # ...and the serve check no longer expects it to be published.
    assert results["ts.serve"]["status"] == "ok"


def test_off_windows_every_app_is_checked(fake):
    fake.windows = False
    results = by_id(checks.farm_checks(fake))
    assert results["guards.watchdog"]["status"] == "unknown"
    fake.down.add(8020)
    assert by_id(checks.farm_checks(fake))["app.notes"]["status"] == "fail"


def test_up_locally_but_not_over_tailscale_fails(fake):
    fake.tailnet_down.add(8040)
    c = by_id(checks.farm_checks(fake))["app.labour"]
    assert c["status"] == "fail"
    assert "not reachable over Tailscale" in c["summary"]


def test_http_500_is_a_warning_not_an_outage(fake):
    from system import Probe
    fake.http_override["http://127.0.0.1:8010/"] = Probe(True, 500, 30)
    c = by_id(checks.farm_checks(fake))["app.owner"]
    assert c["status"] == "warn"
    assert "500" in c["summary"]


def test_boord_with_unapplied_migrations_fails(fake):
    fake.versions[8000]["migrations_applied"] = False
    c = by_id(checks.farm_checks(fake))["app.boord"]
    assert c["status"] == "fail"
    assert "migrations" in c["summary"]


def test_update_available_is_shown_not_alarmed(fake):
    fake.versions[8000]["update"] = {"available": True, "latest": "v3.16", "current": "v3.15"}
    c = by_id(checks.farm_checks(fake))["app.boord"]
    assert c["status"] == "ok"
    assert any("v3.16" in d for d in c["detail"])


# --- Tailscale ---------------------------------------------------------------

def test_tailscale_signed_out(fake):
    fake.ts = ts_status(state="NeedsLogin")
    results = by_id(checks.farm_checks(fake))
    assert results["ts.state"]["status"] == "fail"
    assert "signed out" in results["ts.state"]["summary"]
    # No tailnet address, so no tailnet probes and no serve check.
    assert results["app.boord"]["tailnet"] is None
    assert "ts.serve" not in results


def test_tailscale_missing(fake):
    fake.ts_rc = 127
    c = by_id(checks.farm_checks(fake))["ts.state"]
    assert c["status"] == "fail"
    assert "not found" in c["summary"]


def test_key_expiry(fake):
    fake.ts = ts_status(key_expiry=(NOW + dt.timedelta(days=10)).isoformat() + "Z")
    assert by_id(checks.farm_checks(fake))["ts.key"]["status"] == "warn"
    fake.ts = ts_status(key_expiry=(NOW + dt.timedelta(hours=20)).isoformat() + "Z")
    assert by_id(checks.farm_checks(fake))["ts.key"]["status"] == "fail"
    fake.ts = ts_status(key_expiry="2027-06-01T00:00:00.123456789Z")
    assert by_id(checks.farm_checks(fake))["ts.key"]["status"] == "ok"
    fake.ts = ts_status()
    assert "turned off" in by_id(checks.farm_checks(fake))["ts.key"]["summary"]


def test_serve_pointing_at_the_wrong_app(fake):
    # The 2026-09-04 incident: :443 pointed at Boord Owner.
    fake.serve = serve_for({**GOOD_SERVE, 443: 8010})
    c = by_id(checks.farm_checks(fake))["ts.serve"]
    assert c["status"] == "fail"
    assert ":443 should be Boord" in c["summary"]
    assert "Boord Owner" in c["summary"]
    assert "tailscale serve --bg --https=443 http://localhost:8000" in c["summary"]


def test_serve_missing_an_app(fake):
    fake.serve = serve_for({k: v for k, v in GOOD_SERVE.items() if k != 9443})
    c = by_id(checks.farm_checks(fake))["ts.serve"]
    assert c["status"] == "fail"
    assert "Boord Notes is not published" in c["summary"]


def test_funnel_is_always_a_failure(fake):
    fake.serve = serve_for(GOOD_SERVE, funnel=[8040])
    c = by_id(checks.farm_checks(fake))["ts.serve"]
    assert c["status"] == "fail"
    assert "FUNNEL" in c["summary"]


def test_extra_mapping_is_a_warning(fake):
    fake.serve = serve_for({**GOOD_SERVE, 9999: 3000})
    assert by_id(checks.farm_checks(fake))["ts.serve"]["status"] == "warn"


def test_cert(fake):
    fake.cert_expiry = NOW + dt.timedelta(days=4)
    assert by_id(checks.farm_checks(fake))["ts.cert"]["status"] == "warn"
    fake.cert_expiry = NOW - dt.timedelta(days=1)
    assert by_id(checks.farm_checks(fake))["ts.cert"]["status"] == "fail"


# --- the PC and the safety nets -----------------------------------------------

def test_disk(fake):
    fake.disk_free = 3 * 1024 ** 3
    assert by_id(checks.farm_checks(fake))["pc.disk"]["status"] == "warn"
    fake.disk_free = 1 * 1024 ** 3
    assert by_id(checks.farm_checks(fake))["pc.disk"]["status"] == "fail"


def test_recent_reboot_warns_without_alerting(fake):
    fake.uptime = 600
    c = by_id(checks.farm_checks(fake))["pc.uptime"]
    assert c["status"] == "warn"
    assert c["alert"] is False


def test_watchdog_missing_or_stale(fake):
    del fake.tasks["Boord Watchdog"]
    c = by_id(checks.farm_checks(fake))["guards.watchdog"]
    assert c["status"] == "warn"
    assert "install.bat" in c["summary"]
    fake.tasks["Boord Watchdog"] = {"Name": "Boord Watchdog", "State": "Ready",
                                    "LastRun": (NOW - dt.timedelta(hours=2)).isoformat() + "Z", "LastResult": 0}
    c = by_id(checks.farm_checks(fake))["guards.watchdog"]
    assert c["status"] == "warn"
    assert "Has not run" in c["summary"]


def test_a_watchdog_still_running_boords_copy_warns(fake):
    """After 2.0 the task must run this repo's watchdog.ps1. One still
    pointing at Boord's folder says so, and how to fix it."""
    fake.tasks["Boord Watchdog"]["Action"] = (
        'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\Boord\\watchdog.ps1"')
    c = by_id(checks.farm_checks(fake))["guards.watchdog"]
    assert c["status"] == "warn"
    assert "C:\\Boord\\watchdog.ps1" in c["summary"] and "install.bat" in c["summary"]
    fake.tasks["Boord Watchdog"]["Action"] = (
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{config.WATCHDOG_PS1}"')
    assert by_id(checks.farm_checks(fake))["guards.watchdog"]["status"] == "ok"


def test_watchdog_log(fake):
    now = dt.datetime.now()
    stamp = lambda m: (now - dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    fake.log_text = "\n".join([
        f"{(now - dt.timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')}  Restarting Kudde...",
        f"{stamp(50)}  Boord: DOWN - no answer on port 8000 after 3 probes.",
        f"{stamp(50)}  Restarting Boord...",
        f"{stamp(49)}    RECOVERED: Boord is answering on port 8000 again.",
        f"{stamp(20)}  Restarting Boord...",
    ])
    c = by_id(checks.farm_checks(fake))["guards.log"]
    assert c["status"] == "ok"
    assert c["summary"] == "Restarted in the last 24 h: Boord x2."
    fake.log_text += f"\n{stamp(5)}    REFUSING to restart Boord - port 8000 is still held by something that is not ours."
    c = by_id(checks.farm_checks(fake))["guards.log"]
    assert c["status"] == "warn"
    assert "REFUSING" in c["summary"]
    assert c["alert"] is False


def test_every_tile_has_something_to_show(fake):
    """The dashboard's tiles show `value`, or fall back to the status. The
    numbers people glance at must be there."""
    fake.ts = ts_status(key_expiry=(NOW + dt.timedelta(days=62)).isoformat() + "Z")
    results = by_id(checks.farm_checks(fake))
    assert results["app.boord"]["value"] == "15ms"
    assert results["ts.key"]["value"] == "62d" and results["ts.key"]["short"] == "Key"
    assert results["ts.serve"]["value"] == "7/7"
    assert results["ts.cert"]["value"] == "60d"
    assert results["pc.disk"]["value"] == "120 GB free" and results["pc.disk"]["pct"] == 76
    assert results["pc.memory"]["value"] == "50% used" and results["pc.memory"]["pct"] == 50
    assert results["pc.uptime"]["value"] == "3d"
    assert results["guards.watchdog"]["value"] == "2m"
    fake.down.add(8000)
    fake.serve = serve_for({**GOOD_SERVE, 443: 8010})
    results = by_id(checks.farm_checks(fake))
    assert results["app.boord"]["value"] == "down"
    assert results["ts.serve"]["value"] == "6/7"


def test_short_durations():
    assert [checks._short(s) for s in (40, 61, 3 * 3600, 86400, 3 * 86400)] == ["40s", "1m", "3h", "24h", "3d"]


def test_windows_update(fake):
    c = by_id(checks.farm_checks(fake))["guards.update"]
    assert c["status"] == "ok" and "06:00-18:00" in c["summary"]
    fake.update = {"reboot_pending": True, "reasons": ["Windows Update"], "active_hours": [6, 18],
                   "scheduled": "2026-10-19T01:00:00.0000000Z"}
    import system
    system._cache.clear()
    c = by_id(checks.farm_checks(fake))["guards.update"]
    assert c["status"] == "warn" and c["alert"] is False
    assert c["value"] == "restart" and c["short"] == "Update"
    assert "plans to restart at 2026-10-19" in c["summary"]
    assert checks.worst(checks.farm_checks(fake)) == "warn"
    fake.windows = False
    assert "guards.update" not in by_id(checks.farm_checks(fake))
