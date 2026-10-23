"""The morning summary: once a day, at DAILY_AT on the PC's clock, saying
what is and isn't fine - so a quiet phone can be trusted."""
import datetime as dt

import config
import monitor
import push
from tests.conftest import NOW, peer, ts_status


def subscribe():
    push.public_key()
    push.add_sub({"endpoint": "https://e", "keys": {"p256dh": "p", "auth": "a"}}, "iPhone")


def at(fake, hh, mm=0, day=0):
    fake.local_value = dt.datetime(2026, 10, 18 + day, hh, mm)
    fake.now_value = fake.local_value - dt.timedelta(hours=2)


def dailies(pushed):
    return [p[1] for p in pushed if p[1]["tag"] == "daily"]


def test_once_a_day_after_the_time(fake, pushed, monkeypatch):
    monkeypatch.setattr(config, "DAILY_AT", "06:00")
    subscribe()
    at(fake, 5, 59)
    monitor.run_once(fake)
    assert dailies(pushed) == []
    at(fake, 6, 1)
    monitor.run_once(fake)
    at(fake, 6, 2)
    monitor.run_once(fake)
    assert [d["title"] for d in dailies(pushed)] == ["Farm: all good"]
    at(fake, 6, 1, day=1)
    monitor.run_once(fake)
    assert len(dailies(pushed)) == 2


def test_late_but_not_too_late(fake, pushed, monkeypatch):
    monkeypatch.setattr(config, "DAILY_AT", "06:00")
    subscribe()
    at(fake, 15, 0)  # the monitor was down all morning
    monitor.run_once(fake)
    assert dailies(pushed) == []
    at(fake, 9, 0, day=1)
    monitor.run_once(fake)
    assert len(dailies(pushed)) == 1


def test_a_restart_does_not_send_it_twice(fake, pushed, monkeypatch):
    monkeypatch.setattr(config, "DAILY_AT", "06:00")
    subscribe()
    at(fake, 6, 5)
    monitor.run_once(fake)
    monitor.reset()  # memory gone, data/state.json still there
    at(fake, 6, 30)
    monitor.run_once(fake)
    assert len(dailies(pushed)) == 1


def test_off(fake, pushed, monkeypatch):
    monkeypatch.setattr(config, "DAILY_AT", "off")
    subscribe()
    at(fake, 6, 5)
    monitor.run_once(fake)
    assert dailies(pushed) == []


def test_what_it_says(fake, pushed, monkeypatch):
    monkeypatch.setattr(config, "DAILY_AT", "06:00")
    subscribe()
    at(fake, 6, 5)
    fake.down.add(8030)
    fake.disk_free = 3 * 1024 ** 3
    fake.update = {"reboot_pending": True, "reasons": ["Windows Update"], "active_hours": [6, 18],
                   "scheduled": None}
    fake.ts = ts_status(peers=[peer("device-01"), peer("device-02", online=False)])
    fake.boord = {"device-02": {"station": "", "role": "field", "last_seen": None, "last_sync": None,
                                "queue_pending": 14, "queue_oldest": None,
                                "queue_reported_at": (fake.now_value - dt.timedelta(minutes=5)).isoformat()}}
    monitor.run_once(fake)
    d = dailies(pushed)[0]
    assert d["title"] == "Farm: 1 problem"
    lines = d["body"].split("\n")
    assert lines[0] == "Problem: Kudde"
    assert lines[1].startswith("Check: Disk") and "Update" in lines[1]
    assert "Apps: 5 of 6 up" in lines
    assert "Devices: 1 of 2 online, 14 crates waiting on Dev 02" in lines
    assert "Windows wants to restart the PC" in lines


def test_remote_never_sends_it(fake, pushed, monkeypatch):
    from system import Probe
    monkeypatch.setattr(config, "DAILY_AT", "06:00")
    monkeypatch.setattr(config, "ROLE", "remote")
    monkeypatch.setattr(config, "FARM_URL", "https://farm:8050")
    fake.http_override["https://farm:8050/api/status"] = Probe(False, error="x")
    subscribe()
    at(fake, 6, 5)
    monitor.run_once(fake)
    assert dailies(pushed) == []
