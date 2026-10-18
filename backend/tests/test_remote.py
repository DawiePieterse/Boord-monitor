"""The off-site watcher: telling "PC off" from "monitor stopped", and never
paging anyone about its own connection."""
import config
import monitor
import push
from system import Probe
from tests.conftest import DNS

FARM = f"https://{DNS}:8050"


def remote(monkeypatch):
    monkeypatch.setattr(config, "ROLE", "remote")
    monkeypatch.setattr(config, "FARM_URL", FARM)


def farm_report(fake):
    """What the farm's own /api/status says, as of now."""
    return {"role": "farm", "checked_at": fake.now().isoformat(), "overall": "ok",
            "checks": [{"id": "app.boord", "app": "boord", "status": "ok", "group": "apps", "label": "Boord"},
                       {"id": "app.kudde", "app": "kudde", "status": "off", "group": "apps", "label": "Kudde"}]}


def by_id(snapshot):
    return {c["id"]: c for c in snapshot["checks"]}


def test_farm_reachable(fake, monkeypatch):
    remote(monkeypatch)
    fake.http_override[FARM + "/api/status"] = Probe(True, 200, 40, body=farm_report(fake))
    snap = monitor.run_once(fake)
    checks = by_id(snap)
    assert checks["farm.monitor"]["status"] == "ok"
    # Only the apps the farm said it has are probed - Kudde is "off" there.
    assert "remote.boord" in checks and "remote.kudde" not in checks
    assert snap["farm"]["overall"] == "ok"


def test_pc_off(fake, monkeypatch, pushed):
    remote(monkeypatch)
    push.replace_all(_key(), [{"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}])
    fake.serve = {}  # nothing on the farm answers over the tailnet
    fake.http_override[FARM + "/api/status"] = Probe(False, error="no answer within 15 s")
    for _ in range(config.FAIL_AFTER):
        snap = monitor.run_once(fake)
    c = by_id(snap)["farm.monitor"]
    assert c["status"] == "fail"
    assert "Nothing on the farm PC answers" in c["summary"]
    assert [p[1]["title"] for p in pushed] == ["Farm (seen from outside): Farm PC - problem"]


def test_monitor_stopped_but_pc_on(fake, monkeypatch):
    remote(monkeypatch)
    fake.http_override[FARM + "/api/status"] = Probe(False, error="connection refused")
    c = by_id(monitor.run_once(fake))["farm.monitor"]
    assert c["status"] == "fail"
    assert "only the monitor has stopped" in c["summary"]


def test_watcher_offline_does_not_page(fake, monkeypatch, pushed):
    from tests.conftest import ts_status
    remote(monkeypatch)
    push.replace_all(_key(), [{"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}])
    fake.ts = ts_status(state="Stopped")
    fake.serve = {}
    fake.http_override[FARM + "/api/status"] = Probe(False, error="no route")
    for _ in range(config.FAIL_AFTER + 2):
        snap = monitor.run_once(fake)
    assert by_id(snap)["farm.monitor"]["status"] == "unknown"
    assert pushed == []


def test_stuck_farm_checks_warn(fake, monkeypatch):
    import datetime as dt
    remote(monkeypatch)
    report = farm_report(fake)
    report["checked_at"] = (fake.now() - dt.timedelta(minutes=30)).isoformat()
    fake.http_override[FARM + "/api/status"] = Probe(True, 200, 40, body=report)
    assert by_id(monitor.run_once(fake))["farm.monitor"]["status"] == "warn"


def test_bundle_is_fetched_with_the_token(fake, monkeypatch):
    remote(monkeypatch)
    monkeypatch.setenv("MONITOR_REMOTE_TOKEN", "sekrit")
    sub = {"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}
    fake.http_override[FARM + "/api/status"] = Probe(True, 200, 40, body=farm_report(fake))
    seen = {}
    real_http = fake.http

    def http(url, timeout=10, want_json=False, headers=None):
        if url.endswith("/api/remote/bundle"):
            seen["headers"] = headers
            return Probe(True, 200, 30, body={"key_pem": _key(), "subscriptions": [sub]})
        return real_http(url, timeout, want_json, headers)

    fake.http = http
    monitor.run_once(fake)
    assert seen["headers"] == {"X-Monitor-Token": "sekrit"}
    assert push.load_subs() == [sub]


def _key():
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    v = Vapid()
    v.generate_keys()
    return v.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode()
