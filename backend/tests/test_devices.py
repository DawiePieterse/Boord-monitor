"""Devices on the tailnet: online or when last seen, keys about to lapse,
and for Boord's tablets, when each last sent a crate. Never an alert."""
import datetime as dt
import sqlite3

import checks
import config
import system
from tests.conftest import NOW, peer, ts_status


def iso(t):
    return t.isoformat() + "Z"


def configure(*lines):
    with open(config.DEVICES_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def devices(fake):
    return {c["id"]: c for c in checks.farm_checks(fake) if c["group"] == "devices"}


def test_every_peer_shown_without_a_list(fake):
    fake.ts = ts_status(peers=[
        peer("device-02", online=False, last_seen=iso(NOW - dt.timedelta(hours=3))),
        peer("andre-iphone"),
        peer("someone-elses-pc", sharee=True),
    ])
    d = devices(fake)
    assert list(d) == ["dev.andre-iphone", "dev.device-02"]
    assert d["dev.andre-iphone"]["status"] == "ok"
    assert d["dev.andre-iphone"]["value"] == "on"
    off = d["dev.device-02"]
    assert off["status"] == "off" and off["word"] == "Offline"
    assert off["value"] == "3h"
    assert off["short"] == "Dev 02"
    assert "last seen 3 h ago" in off["summary"]
    assert all(c["alert"] is False for c in d.values())


def test_offline_devices_do_not_turn_the_banner_amber(fake):
    fake.ts = ts_status(peers=[peer("device-01", online=False, last_seen=iso(NOW - dt.timedelta(days=9)))])
    assert checks.worst(checks.farm_checks(fake)) == "ok"


def test_a_key_about_to_expire_warns(fake):
    fake.ts = ts_status(peers=[peer("device-01", key_expiry=iso(NOW + dt.timedelta(days=5)))])
    c = devices(fake)["dev.device-01"]
    assert c["status"] == "warn"
    assert "Disable key expiry" in c["summary"]
    assert c["alert"] is False


def test_list_names_and_orders_devices_and_reports_missing_ones(fake):
    fake.ts = ts_status(peers=[peer("ipad-7f3a", dns="ipad-7f3a"), peer("andre-iphone")])
    configure("# tailscale name | name to show | boord device", "ipad-7f3a | Field 1 | device-01",
              "gone-tablet | Field 2")
    d = devices(fake)
    assert list(d) == ["dev.ipad-7f3a", "dev.gone-tablet"]  # only the listed ones, in order
    assert d["dev.ipad-7f3a"]["label"] == "Field 1"
    assert d["dev.gone-tablet"]["status"] == "warn"
    assert "No device called" in d["dev.gone-tablet"]["summary"]


def test_boord_last_crate(fake):
    fake.ts = ts_status(peers=[peer("device-03")])
    fake.boord = {"device-03": {"station": "Block 7", "role": "field",
                                "last_seen": "2026-10-18 06:30:00",
                                "last_sync": "2026-10-18 07:20:00"}}
    c = devices(fake)["dev.device-03"]
    assert "Last crate received 40 min ago" in c["summary"]
    assert any("Block 7" in d for d in c["detail"])


def test_real_boord_database_read_only(tmp_path):
    from system import System
    db = tmp_path / "boord.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE device (id TEXT PRIMARY KEY, station TEXT, role TEXT, active BOOLEAN, last_seen DATETIME);
        CREATE TABLE harvestrecord (uuid TEXT PRIMARY KEY, device_id TEXT, synced_at DATETIME);
        INSERT INTO device VALUES ('device-01', 'Block 7', 'field', 1, '2026-10-18 06:30:00');
        INSERT INTO device VALUES ('device-06', 'Bin tip', 'packhouse', 1, NULL);
        INSERT INTO harvestrecord VALUES ('a', 'device-01', '2026-10-18 07:00:00');
        INSERT INTO harvestrecord VALUES ('b', 'device-01', '2026-10-18 07:20:00');
    """)
    con.commit()
    con.close()
    got = System().boord_devices(str(db))
    assert got["device-01"]["last_sync"] == "2026-10-18 07:20:00"
    assert got["device-06"]["last_sync"] is None
    # A Boord whose schema moved on is no detail, not a crash.
    con = sqlite3.connect(db)
    con.execute("DROP TABLE harvestrecord")
    con.commit()
    con.close()
    # Still the cached answer for a few minutes; then the real one.
    assert System().boord_devices(str(db)) == got
    system._cache.clear()
    assert System().boord_devices(str(db)) is None
    assert System().boord_devices(str(tmp_path / "missing.db")) is None
