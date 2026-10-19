"""The HTTP surface: what is open, what needs the PIN, what needs the token."""
import config
import restart

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "p", "auth": "a"}}


def test_status_is_open(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "farm"
    assert body["overall"] == "ok"
    assert {c["id"] for c in body["checks"]} >= {"app.boord", "ts.state", "ts.serve", "pc.disk"}


def test_check_now_is_rate_limited(client, fake):
    client.post("/api/check")
    n = len(fake.calls)
    client.post("/api/check")
    assert len(fake.calls) == n


def test_session_before_and_after_sign_in(client):
    s = client.get("/api/session").json()
    assert s == {**s, "role": "farm", "admin": False, "pin_set": False}
    assert len(s["push_key"]) == 87  # 65-byte P-256 point, base64url, no padding


def test_login_needs_a_pin_set_first(client):
    assert client.post("/api/admin/login", json={"pin": "1234"}).status_code == 409


def test_wrong_pin(client):
    import admin_auth
    admin_auth.set_pin("4321")
    assert client.post("/api/admin/login", json={"pin": "0000"}).status_code == 401


def test_acting_needs_the_pin(client):
    assert client.post("/api/apps/boord/restart").status_code == 401
    assert client.post("/api/push/subscribe", json={"subscription": SUB}).status_code == 401
    assert client.post("/api/push/test").status_code == 401


def test_subscribe_and_unsubscribe(admin_client):
    r = admin_client.post("/api/push/subscribe", json={"subscription": SUB, "label": "Andre"})
    assert r.json() == {"ok": True, "subscribers": 1}
    assert admin_client.get("/api/session").json()["subscribers"] == 1
    admin_client.post("/api/admin/logout")
    # Turning alerts off needs no PIN.
    assert admin_client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json()["subscribers"] == 0


def test_incomplete_subscription_is_refused(admin_client):
    r = admin_client.post("/api/push/subscribe", json={"subscription": {"endpoint": "x"}})
    assert r.status_code == 422


def test_restart_runs_boords_watchdog(admin_client, monkeypatch):
    import os
    import subprocess
    with open(os.path.join(config.BOORD_DIR, "watchdog.ps1"), "w") as fh:
        fh.write("# stub")
    seen = {}

    class Done:
        returncode = 0
        stdout = b"RECOVERED: Boord is answering on port 8000 again.\n"
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(restart.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})())
    r = admin_client.post("/api/apps/boord/restart")
    assert r.status_code == 200
    assert seen["cmd"][-2:] == ["-Restart", "Boord"]
    assert seen["cmd"][-3].endswith("watchdog.ps1")
    job = admin_client.get("/api/restart").json()
    assert job["state"] == "done"
    assert "RECOVERED" in job["output"][0]


def test_restart_without_boords_watchdog_explains(admin_client):
    import os
    path = os.path.join(config.BOORD_DIR, "watchdog.ps1")
    if os.path.exists(path):
        os.remove(path)
    r = admin_client.post("/api/apps/boord/restart")
    assert r.status_code == 409
    assert "MONITOR_BOORD_DIR" in r.json()["detail"]


def test_unknown_app(admin_client):
    assert admin_client.post("/api/apps/nope/restart").status_code == 409


def test_remote_bundle_needs_the_token(client):
    assert client.get("/api/remote/bundle").status_code == 403
    assert client.get("/api/remote/bundle", headers={"X-Monitor-Token": "wrong"}).status_code == 403
    with open(config.REMOTE_TOKEN_FILE) as fh:
        token = fh.read().strip()
    r = client.get("/api/remote/bundle", headers={"X-Monitor-Token": token})
    assert r.status_code == 200
    assert r.json()["key_pem"].startswith("-----BEGIN")


def test_version_and_frontend(client):
    v = client.get("/api/version").json()
    assert v["app"] == "Boord Monitor"
    assert v["frontend_version"]
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert "Boord Monitor" in page.text


def test_version_ahead_of_tag_reports_describe(client, monkeypatch):
    """Between releases the tag field carries git describe's string, not null,
    so the heartbeat says what is actually running."""
    import main

    def fake_git(args):
        if "--exact-match" in args:
            return ""  # no tag on this commit
        return "v1.5-3-gabc1234"
    monkeypatch.setattr(main, "_git", fake_git)
    monkeypatch.setattr(main, "_version_cache", {})
    v = client.get("/api/version").json()
    assert v["tag"] == "v1.5-3-gabc1234"
    assert v["state"] == "ahead"


def test_version_on_a_release_tag(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "_git", lambda args: "v1.5")
    monkeypatch.setattr(main, "_version_cache", {})
    v = client.get("/api/version").json()
    assert v["tag"] == "v1.5"
    assert v["state"] == "release"


def test_version_without_git(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "_git", lambda args: "")
    monkeypatch.setattr(main, "_version_cache", {})
    v = client.get("/api/version").json()
    assert v["tag"] is None
    assert v["state"] == "unknown"


def test_versions_agree():
    """api.js VERSION and the service worker's CACHE move together."""
    import os
    import re
    with open(os.path.join(config.FRONTEND_DIR, "api.js")) as fh:
        version = re.search(r'^  VERSION: "(.*)",$', fh.read(), re.M).group(1)
    with open(os.path.join(config.FRONTEND_DIR, "service-worker.js")) as fh:
        cache = re.search(r'const CACHE = "boord-monitor-v(\d+)"', fh.read()).group(1)
    assert version and cache


def test_updater_dependency_check_matches_requirements():
    """update_monitor_server.bat refuses to restart unless these import; a
    name missing there is a dependency an update can silently half-install."""
    import os
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(here, "..", "..")
    with open(os.path.join(here, "..", "requirements.txt")) as fh:
        reqs = {re.split(r"[\[=<>]", line.strip())[0].lower()
                for line in fh if line.strip() and not line.startswith("#")}
    with open(os.path.join(root, "update_monitor_server.bat")) as fh:
        bat = fh.read()
    imported = set(re.search(r'-c "import ([^"]+)"', bat).group(1).replace(" ", "").split(","))
    assert imported == reqs
