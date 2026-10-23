"""History, SLOs, incidents, silences and the standard endpoints (2.0)."""
import datetime as dt

import config
import monitor
import push
import slo
import store

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "p", "auth": "a"}}


def subscribe():
    push.public_key()
    push.add_sub(SUB, "iPhone")


def run(fake, n=1):
    for _ in range(n):
        monitor.run_once(fake)
        fake.now_value += dt.timedelta(minutes=1)


def titles(pushed):
    return [p[1]["title"] for p in pushed]


# --- the store ------------------------------------------------------------------

def test_every_run_is_kept(fake):
    run(fake, 5)
    t = store.ts(fake.now_value)
    c = store.counts("app.boord", t - 3600, t)
    assert c == {"ok": 5, "warn": 0, "fail": 0, "other": 0}
    assert store.availability(c) == 1.0


def test_availability_counts_warn_as_up_and_off_as_neither(fake):
    run(fake, 6)
    fake.down.add(8000)
    run(fake, 2)
    t = store.ts(fake.now_value)
    assert store.availability(store.counts("app.boord", t - 3600, t)) == 6 / 8
    # Hourly rollup agrees with the raw samples.
    hourly = store.counts("app.boord", t - 30 * 86400, t)
    assert hourly["ok"] == 6 and hourly["fail"] == 2


def test_latency_percentiles_exact_and_from_the_histogram():
    assert store._quantile_exact([10, 20, 30, 40], .5) == 20
    assert store._quantile_exact([], .5) is None
    # 10 answers in the 25-50 ms bucket: the median interpolates inside it.
    b = [0] * (len(store.BUCKETS_MS) + 1)
    b[1] = 10
    assert store._quantile_hist(b, .5) == 37.5
    b[-1] = 90  # mostly over 10 s
    assert store._quantile_hist(b, .95) == 10000.0


def test_series_and_history_endpoint(client, fake):
    run(fake, 3)
    fake.now_value -= dt.timedelta(minutes=1)
    r = client.get("/api/history/app.boord?hours=1").json()
    assert r["points"] and r["points"][-1]["status"] == "ok"
    assert r["points"][-1]["ms"] == 15
    assert r["latency"]["p50"] == 15


def test_daily_bars_follow_the_farm_clock(fake):
    run(fake, 3)
    bars = store.daily(["app.boord"], 3, store.ts(fake.now_value), offset_s=2 * 3600)["app.boord"]
    assert [b["day"] for b in bars] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert bars[-1]["availability"] == 1.0 and bars[0]["availability"] is None


# --- incidents ------------------------------------------------------------------

def test_an_alert_opens_an_incident_and_recovery_resolves_it(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    active = monitor.latest["incidents"]
    assert [(i["check_id"], i["state"], i["paged"]) for i in active] == [("app.boord", "open", True)]
    fake.down.clear()
    run(fake)
    inc = store.get_incident(active[0]["id"])
    assert inc["state"] == "resolved"
    assert [e["kind"] for e in inc["timeline"]] == ["detected", "opened", "notified", "resolved"]
    # From the first failed check to the first good one.
    assert inc["duration_s"] == config.FAIL_AFTER * 60


def test_a_blip_opens_no_incident(fake):
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER - 1)
    fake.down.clear()
    run(fake)
    assert store.incidents() == []


def test_acknowledging_stops_reminders_but_not_recovery(admin_client, fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    iid = monitor.latest["incidents"][0]["id"]
    r = admin_client.post(f"/api/incidents/{iid}/ack", json={"by": "Andre"})
    assert r.status_code == 200 and r.json()["state"] == "acknowledged"
    assert admin_client.post(f"/api/incidents/{iid}/ack", json={}).status_code == 409
    fake.now_value += dt.timedelta(seconds=config.REMIND_EVERY_S)
    run(fake)
    assert titles(pushed) == ["Farm: Boord - problem"]
    fake.down.clear()
    run(fake)
    assert titles(pushed) == ["Farm: Boord - problem", "Farm: back to normal"]
    assert store.get_incident(iid)["acked_by"] == "Andre"


def test_ack_needs_the_pin(client, fake):
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    iid = monitor.latest["incidents"][0]["id"]
    assert client.post(f"/api/incidents/{iid}/ack", json={}).status_code == 401


def test_a_non_alerting_fail_is_recorded_but_never_pushed(fake, pushed):
    subscribe()
    config_folders = "C:\\Backups\\Boord"
    with open(config.DRIVE_BACKUPS_FILE, "w") as fh:
        fh.write(config_folders + "\n")
    fake.drive_running = False
    fake.uptime = 60  # just booted: Drive's alert waits
    fake.folders[config_folders] = ("x.zip", fake.now_value)
    run(fake, config.FAIL_AFTER)
    assert pushed == []
    inc = [i for i in monitor.latest["incidents"] if i["check_id"] == "drive.app"]
    assert inc and inc[0]["paged"] is False


def test_incident_stats(fake):
    fake.down.add(8000)
    run(fake, 4)
    fake.down.clear()
    run(fake, 2)
    t = store.ts(fake.now_value)
    s = store.incident_stats(t - 86400, t)
    assert s["count"] == 1 and s["open"] == 0 and s["mttr_s"] == 240


# --- silences -------------------------------------------------------------------

def test_a_silence_holds_the_push_until_it_ends(admin_client, fake, pushed):
    subscribe()
    r = admin_client.post("/api/silences", json={"match": "group:apps", "minutes": 10, "reason": "updating"})
    assert r.status_code == 200
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER + 2)
    assert pushed == []
    assert monitor.latest["incidents"][0]["paged"] is False
    assert monitor.latest["silences"][0]["reason"] == "updating"
    run(fake, 6)  # past the ten minutes, still down
    assert titles(pushed) == ["Farm: Boord - problem"]
    kinds = [e["kind"] for e in store.get_incident(monitor.latest["incidents"][0]["id"])["timeline"]]
    assert "silenced" in kinds and kinds[-1] == "notified"


def test_a_silence_can_be_ended_early(admin_client, fake):
    sid = admin_client.post("/api/silences", json={"match": "*", "minutes": 60}).json()["id"]
    assert admin_client.post(f"/api/silences/{sid}/end").status_code == 200
    assert admin_client.post(f"/api/silences/{sid}/end").status_code == 409
    assert store.silences(store.ts(fake.now_value)) == []


def test_silences_need_the_pin_and_sane_lengths(client, admin_client):
    admin_client.post("/api/admin/logout")
    assert client.post("/api/silences", json={"match": "*", "minutes": 10}).status_code == 401


def test_silence_limits(admin_client):
    assert admin_client.post("/api/silences", json={"match": "*", "minutes": 0}).status_code == 422
    assert admin_client.post("/api/silences", json={"match": "*", "minutes": 8 * 24 * 60}).status_code == 422
    assert admin_client.post("/api/silences", json={"match": " ", "minutes": 5}).status_code == 422


def test_silence_matching():
    c = {"id": "app.boord", "group": "apps"}
    assert store.matches("*", c) and store.matches("group:apps", c) and store.matches("app.boord", c)
    assert not store.matches("group:pc", c) and not store.matches("app.owner", c)


# --- SLOs -----------------------------------------------------------------------

def test_slo_budget_and_burn(fake):
    run(fake, 98)
    fake.down.add(8000)
    run(fake, 2)
    fake.down.clear()
    s = slo.app_slo(__import__("apps").BY_KEY["boord"], fake.now_value, 99.0, 1000)
    assert abs(s["availability"]["30d"] - 0.98) < 1e-9
    # 100 runs at 99% allow one failed run; two failed is double the budget.
    assert abs(s["error_budget"]["remaining"] - (-1.0)) < 1e-9
    assert s["burn_rate"]["1h"] > 1
    assert slo.status_of(s) == "missed"
    assert s["latency"]["met"] is True


def test_slo_targets_file(fake):
    with open(slo.SLO_FILE, "w") as fh:
        fh.write("# comment\nboord | 99.9 | 300\nkudde | 98\nnope | 1\n")
    t = slo.targets()
    assert t["boord"] == (99.9, 300)
    assert t["kudde"] == (98.0, slo.DEFAULT_LATENCY_MS)
    assert "nope" not in t


def test_overview(client, fake):
    run(fake, 3)
    fake.now_value -= dt.timedelta(minutes=1)
    o = client.get("/api/overview?days=7").json()
    assert {s["app"] for s in o["slo"]} == {"boord", "owner", "notes", "kudde", "labour", "packhouse"}
    assert o["slo"][0]["status"] == "met"
    ids = {c["id"] for c in o["components"]}
    assert "app.boord" in ids and "ts.state" in ids
    assert not any(i.startswith("dev.") for i in ids)
    assert len(o["components"][0]["days"]) == 7
    assert o["kpi"]["availability_30d"] == 1.0 and o["kpi"]["incidents_open"] == 0


# --- standard endpoints ---------------------------------------------------------

def test_metrics_are_prometheus_text(client, fake):
    run(fake, 2)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert "# TYPE boord_app_up gauge" in body
    assert 'boord_app_up{app="boord",port="8000"} 1' in body
    assert 'boord_check_state{check="app.boord",state="ok"} 1' in body
    assert 'boord_app_request_duration_seconds_bucket{app="boord",le="+Inf"} 2' in body
    assert "boord_disk_free_bytes " in body
    assert 'boord_slo_target_ratio{app="boord"} 0.995' in body
    # Every sample line belongs to a declared family.
    families = {line.split()[2] for line in body.splitlines() if line.startswith("# TYPE")}
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name = line.split("{")[0].split(" ")[0]
            assert any(name == f or name.startswith(f + "_") for f in families), line


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["history"] == "ok"


def test_status_carries_incidents_and_silences(client, fake):
    body = client.get("/api/status").json()
    assert body["incidents"] == [] and body["silences"] == []
    assert body["interval_s"] == config.CHECK_INTERVAL_S


def test_a_broken_history_never_stops_the_alerts(fake, pushed, monkeypatch):
    subscribe()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "record", boom)
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    assert titles(pushed) == ["Farm: Boord - problem"]
