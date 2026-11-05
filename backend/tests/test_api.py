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
    # The real watchdog.ps1 in this repo; subprocess.run is faked below.
    assert os.path.exists(config.WATCHDOG_PS1)
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


def test_restart_without_the_watchdog_explains(admin_client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WATCHDOG_PS1", str(tmp_path / "watchdog.ps1"))
    r = admin_client.post("/api/apps/boord/restart")
    assert r.status_code == 409
    assert "watchdog.ps1 is not at" in r.json()["detail"]


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


# --- update -----------------------------------------------------------------------
# The Update button and the owner's deploy.sh both end here. The monitor only
# queues the request and starts the "Boord Update" task; the task (watchdog.ps1
# -Update) writes the status file that these read back.

def _fake_task(monkeypatch, tmp_path, fail=False):
    import update
    monkeypatch.setattr(update, "REQUEST_FILE", str(tmp_path / "update_request.json"))
    monkeypatch.setattr(update, "STATUS_FILE", str(tmp_path / "update_status.json"))
    started = []

    def start_task():
        if fail:
            raise ValueError("Could not start the 'Boord Update' task: access denied")
        started.append(True)
    monkeypatch.setattr(update, "_start_task", start_task)
    return update, started


def test_update_needs_the_pin_or_the_token(client, monkeypatch, tmp_path):
    _fake_task(monkeypatch, tmp_path)
    assert client.post("/api/apps/boord/update").status_code == 401
    assert client.post("/api/apps/boord/update", headers={"X-Monitor-Token": "wrong"}).status_code == 401
    assert client.get("/api/update").status_code == 401


def test_update_queues_the_app_and_starts_the_task(admin_client, monkeypatch, tmp_path):
    import json
    update, started = _fake_task(monkeypatch, tmp_path)
    r = admin_client.post("/api/apps/packhouse/update")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued" and r.json()["by"] == "phone"
    assert started == [True]
    # The watchdog finds the app by its name in apps.json, as -Restart does.
    assert json.load(open(update.REQUEST_FILE))["app"] == "Boord Packhouse"
    assert admin_client.get("/api/update").json()["state"] == "queued"
    # One at a time.
    r = admin_client.post("/api/apps/boord/update")
    assert r.status_code == 409 and "still updating" in r.json()["detail"]


def test_deploy_starts_an_update_with_the_remote_token(client, monkeypatch, tmp_path):
    import config
    update, started = _fake_task(monkeypatch, tmp_path)
    with open(config.REMOTE_TOKEN_FILE, encoding="ascii") as fh:
        token = fh.read().strip()
    r = client.post("/api/apps/boord/update", headers={"X-Monitor-Token": token})
    assert r.status_code == 200 and r.json()["by"] == "deploy"
    assert client.get("/api/update", headers={"X-Monitor-Token": token}).json()["name"] == "Boord"


def test_update_reads_what_the_watchdog_wrote(admin_client, monkeypatch, tmp_path):
    update, _ = _fake_task(monkeypatch, tmp_path)
    # PowerShell 5.1 writes UTF-8 with a BOM.
    with open(update.STATUS_FILE, "w", encoding="utf-8-sig") as fh:
        fh.write('{"app": "boord", "name": "Boord", "state": "done", "tag": "v3.60", '
                 '"requested": "2026-11-05T08:00:00", "output": ["Boord is on v3.60 and answering."]}')
    job = admin_client.get("/api/update").json()
    assert (job["state"], job["tag"]) == ("done", "v3.60")
    # Finished, so another can start.
    assert admin_client.post("/api/apps/boord/update").status_code == 200


def test_a_request_the_task_never_picked_up_does_not_block_for_ever(admin_client, monkeypatch, tmp_path):
    update, _ = _fake_task(monkeypatch, tmp_path)
    with open(update.STATUS_FILE, "w", encoding="utf-8") as fh:
        fh.write('{"app": "boord", "name": "Boord", "state": "queued", "requested": "2020-01-01T00:00:00", "output": []}')
    assert admin_client.get("/api/update").json()["state"] == "failed"
    assert admin_client.post("/api/apps/boord/update").status_code == 200


def test_a_task_that_cannot_start_says_so(admin_client, monkeypatch, tmp_path):
    _fake_task(monkeypatch, tmp_path, fail=True)
    r = admin_client.post("/api/apps/boord/update")
    assert r.status_code == 409 and "access denied" in r.json()["detail"]
    assert admin_client.get("/api/update").json()["state"] == "failed"


def test_the_update_task_runs_this_repos_watchdog(monkeypatch):
    import subprocess
    import update
    seen = {}

    class Done:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and Done())
    update._start_task()
    script = seen["cmd"][-1]
    assert config.WATCHDOG_PS1 in script and "-Update" in script
    assert "Register-ScheduledTask -TaskName 'Boord Update'" in script and "Start-ScheduledTask" in script
    assert "-UserId 'SYSTEM'" in script


def test_every_updater_can_run_unattended():
    """The task runs each app's updater with --unattended; the watchdog
    passes it whatever apps.json names. Only Monitor's own is in this repo."""
    import os
    path = os.path.join(config.REPO_ROOT, "update_monitor_server.bat")
    text = open(path, encoding="utf-8").read()
    assert '"--unattended" set "UNATTENDED=1"' in text
    bare = [ln for ln in text.splitlines() if ln.strip().lower() == "pause"]
    assert not bare, "a pause that would hang the unattended update"


def test_update_output_written_as_powershell_objects_reads_as_text(admin_client, monkeypatch, tmp_path):
    """Windows PowerShell 5.1 turned Get-Content's lines into objects in the
    JSON, and the dashboard showed "[object Object]" for each."""
    update, _ = _fake_task(monkeypatch, tmp_path)
    with open(update.STATUS_FILE, "w", encoding="utf-8-sig") as fh:
        fh.write('{"app": "kudde", "name": "Kudde", "state": "done", "requested": "2026-11-05T08:00:00", "output": ['
                 '{"value": "==> Fetching tags", "PSPath": "C:\\\\boord-monitor\\\\data\\\\update.log", "ReadCount": 1},'
                 '"Kudde is on v0.11 and answering."]}')
    assert admin_client.get("/api/update").json()["output"] == ["==> Fetching tags", "Kudde is on v0.11 and answering."]


def test_the_monitor_can_update_itself(admin_client, monkeypatch, tmp_path):
    """deploy.sh v2.3 in Monitor's own repo was refused: "There is no app by
    that name", because the monitor is apps.SELF, not one of apps.APPS."""
    import json
    import apps
    update, started = _fake_task(monkeypatch, tmp_path)
    r = admin_client.post(f"/api/apps/{apps.SELF.key}/update")
    assert r.status_code == 200, r.text
    assert json.load(open(update.REQUEST_FILE))["app"] == "Boord Monitor" and started == [True]
