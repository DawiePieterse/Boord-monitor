"""The nightly update: its setting, its task and its tile.

watchdog.ps1 -Nightly does the work on the farm PC and writes
data/update_nightly.json; these test what the monitor makes of the setting
and of that file. FakeSystem.files stands in for both files."""
import datetime as dt
import json

import checks
import config
import update
from tests.conftest import NOW, by_id


def tile(fake):
    return by_id(checks.farm_checks(fake))["guards.nightly"]


def result(fake, ran, results):
    fake.files[update.NIGHTLY_RESULT] = json.dumps({"ran": ran.isoformat(timespec="seconds"), "results": results})


def test_the_setting_is_midnight_unless_it_says_otherwise():
    assert update.parse_nightly(None) == "00:00"
    assert update.parse_nightly("# when\n02:30\n") == "02:30"
    assert update.parse_nightly("﻿off") is None
    assert update.parse_nightly("half past two") == "00:00", "a typo is not guessed at"


def test_registering_points_the_task_at_this_watchdog():
    script = update._nightly_script("00:00")
    assert config.WATCHDOG_PS1 in script and "-Nightly" in script
    assert "-Daily -At '00:00'" in script and "-StartWhenAvailable" in script
    assert "Register-ScheduledTask -TaskName 'Boord Nightly Update'" in script
    assert "Unregister-ScheduledTask -TaskName 'Boord Nightly Update'" in update._nightly_script(None)


def test_not_run_yet(fake):
    c = tile(fake)
    assert c["status"] == "ok" and "has not run yet" in c["summary"] and not c["alert"]


def test_off_is_off_not_a_warning(fake):
    fake.files[update.NIGHTLY_FILE] = "off\n"
    assert tile(fake)["status"] == "off"


def test_a_missing_task_warns(fake):
    del fake.tasks["Boord Nightly Update"]
    c = tile(fake)
    assert c["status"] == "warn" and "not registered" in c["summary"]


def test_a_quiet_night(fake):
    result(fake, NOW - dt.timedelta(hours=8), [{"name": "Boord", "state": "current", "from": "v3.60", "to": "v3.60"}])
    c = tile(fake)
    assert c["status"] == "ok" and "already on its newest" in c["summary"]


def test_an_install_is_named(fake):
    result(fake, NOW - dt.timedelta(hours=8), [{"name": "Boord", "state": "done", "from": "v3.59", "to": "v3.60"},
                                                {"name": "Kudde", "state": "current", "from": "v0.11", "to": "v0.11"}])
    c = tile(fake)
    assert c["status"] == "ok" and c["summary"] == "Last night installed Boord v3.60."
    assert "Boord: v3.59 -> v3.60" in c["detail"]


def test_a_failure_warns_but_never_pushes(fake):
    result(fake, NOW - dt.timedelta(hours=8), [
        {"name": "Boord Labour", "state": "failed", "from": "v1.0.10", "to": None,
         "note": "Could not ask GitHub for its releases (no internet, or a private repo SYSTEM has no GitHub login for)."}])
    c = tile(fake)
    assert c["status"] == "warn" and "Boord Labour" in c["summary"] and c["alert"] is False


def test_a_night_that_never_ran_warns(fake):
    result(fake, NOW - dt.timedelta(hours=50), [])
    c = tile(fake)
    assert c["status"] == "warn" and "Has not run for" in c["summary"]
