"""Google Drive backups: a fresh file has reached each folder, and Drive is
running to upload it."""
import datetime as dt

import checks
import config
import monitor
import push
from tests.conftest import NOW

FOLDER = r"C:\Users\Andre\My Drive\Boord backups"


def configure(*lines):
    with open(config.DRIVE_BACKUPS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def by_id(results):
    return {c["id"]: c for c in results}


def fresh(fake, hours=5, folder=FOLDER):
    fake.folders[folder] = (folder + r"\backup_20261018_020000.zip", NOW - dt.timedelta(hours=hours))


def test_off_without_the_file(fake):
    assert [c for c in checks.farm_checks(fake) if c["group"] == "backups"] == []


def test_fresh_backup_and_drive_running(fake):
    configure("# where Drive uploads from", FOLDER)
    fresh(fake)
    results = by_id(checks.farm_checks(fake))
    assert results["drive.app"]["status"] == "ok"
    folder = results["drive.folder.0"]
    assert folder["status"] == "ok"
    assert folder["label"] == "Boord backups"
    assert "backup_20261018_020000.zip" in folder["summary"]


def test_stale_backup_fails_after_the_default_limit(fake):
    configure(FOLDER)
    fresh(fake, hours=config.DRIVE_DEFAULT_DAYS * 24 + 1)
    c = by_id(checks.farm_checks(fake))["drive.folder.0"]
    assert c["status"] == "fail"
    assert "every 3 days" in c["summary"]


def test_per_folder_limit(fake):
    # Off-season: Boord skips nights with nothing to back up.
    configure(f"{FOLDER} | 30")
    fresh(fake, hours=10 * 24)
    assert by_id(checks.farm_checks(fake))["drive.folder.0"]["status"] == "ok"
    configure(f"{FOLDER} | nonsense")
    assert by_id(checks.farm_checks(fake))["drive.folder.0"]["status"] == "fail"


def test_drive_not_running_fails_but_not_straight_after_a_boot(fake):
    configure(FOLDER)
    fresh(fake)
    fake.drive_running = False
    c = by_id(checks.farm_checks(fake))["drive.app"]
    assert c["status"] == "fail"
    assert "logged in" in c["summary"]
    fake.uptime = 10 * 60
    assert by_id(checks.farm_checks(fake))["drive.app"]["status"] == "warn"


def test_the_g_drive_is_explained_not_reported_missing(fake):
    configure(r"G:\My Drive\Boord backups")
    c = by_id(checks.farm_checks(fake))["drive.folder.0"]
    assert c["status"] == "warn"
    assert c["alert"] is False
    assert "SYSTEM" in c["summary"]


def test_empty_folder(fake):
    configure(FOLDER)
    fake.folders[FOLDER] = None
    assert by_id(checks.farm_checks(fake))["drive.folder.0"]["status"] == "warn"


def test_a_stale_backup_pages(fake, pushed):
    push.public_key()
    push.add_sub({"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}, "iPhone")
    configure(FOLDER)
    fresh(fake, hours=24 * 5)
    for _ in range(config.FAIL_AFTER):
        monitor.run_once(fake)
    assert [p[1]["title"] for p in pushed] == ["Farm: Boord backups - problem"]


def test_real_newest_file(tmp_path):
    import os
    import time

    from system import System
    (tmp_path / "kudde").mkdir()
    old, new = tmp_path / "a.zip", tmp_path / "kudde" / "b.zip"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    path, when = System().newest_file(str(tmp_path))
    assert path == str(new)
    assert abs((dt.datetime.utcnow() - when).total_seconds()) < 60
    (tmp_path / "empty").mkdir()
    assert System().newest_file(str(tmp_path / "empty")) is None
