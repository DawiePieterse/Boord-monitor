"""apps.json is the one app table: the monitor, the watchdog and the
heartbeat all read it, and none of them may grow a private copy again."""
import json
import os
import re

import apps
import config

ROOT = config.REPO_ROOT


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def test_the_table_is_consistent():
    table = json.loads(_read("apps.json"))["apps"]
    for field in ("key", "name", "port", "task"):
        values = [a[field] for a in table]
        assert len(values) == len(set(values)), f"duplicate {field} in apps.json"
    ts_ports = [a["ts_port"] for a in table]
    assert len(ts_ports) == len(set(ts_ports)), "two apps published on the same tailnet port"
    assert sum(1 for a in table if a.get("self")) == 1
    for a in table:
        assert a["health"].startswith("/") and a["updater"].endswith(".bat"), a["key"]


def test_apps_py_reads_it():
    names = [a["name"] for a in json.loads(_read("apps.json"))["apps"]]
    assert [a.name for a in apps.APPS] + [apps.SELF.name] == names
    assert apps.SELF.port == config.PORT
    assert "packhouse" in apps.BY_KEY


def test_the_scripts_read_it_and_keep_no_table_of_their_own():
    for script in ("watchdog.ps1", "heartbeat.ps1"):
        text = _read(script)
        assert '"apps.json"' in text, f"{script} must read apps.json"
        # The pre-2.0 tables were hashtable literals with a Port per app.
        assert not re.search(r"Port\s*=\s*\d{4}", text), f"{script} has a hard-coded app table again"


def test_the_watchdog_stands_down_for_every_updater():
    text = _read("watchdog.ps1")
    assert "$_.updater" in text


def test_the_heartbeat_keeps_its_urls_in_data():
    assert 'Join-Path $here "data"' in _read("heartbeat.ps1")
    assert "data\\heartbeat_url.txt" in _read("setup_heartbeat.bat")


def test_the_installer_registers_both_tasks_here():
    text = _read("install.ps1")
    for task, script in ((apps.WATCHDOG_TASK, "watchdog.ps1"), (apps.HEARTBEAT_TASK, "heartbeat.ps1")):
        assert f'Register-ScriptTask "{task}" (Join-Path $RepoRoot "{script}")' in text
    for bat, task in (("setup_watchdog.bat", apps.WATCHDOG_TASK), ("setup_heartbeat.bat", apps.HEARTBEAT_TASK)):
        assert f'/tn "{task}"' in _read(bat)
