"""When a phone gets woken, and when it doesn't."""
import datetime as dt

import config
import monitor
import push

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


def test_no_alert_until_it_has_failed_three_runs_in_a_row(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER - 1)
    assert pushed == []
    run(fake)
    assert titles(pushed) == ["Farm: Boord - problem"]
    assert "port 8000" in pushed[0][1]["body"]
    # ...and not again on the next run.
    run(fake, 3)
    assert len(pushed) == 1


def test_a_blip_the_watchdog_fixes_never_alerts(fake, pushed):
    subscribe()
    run(fake)
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER - 1)
    fake.down.clear()
    run(fake, 2)
    assert pushed == []
    # ...but it is on the dashboard's list of changes.
    events = monitor.latest["events"]
    assert [(e["id"], e["to"]) for e in events[:2]] == [("app.boord", "ok"), ("app.boord", "fail")]


def test_recovery_is_announced(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    fake.down.clear()
    run(fake)
    assert titles(pushed) == ["Farm: Boord - problem", "Farm: back to normal"]
    assert pushed[1][1]["body"] == "Boord: fixed."


def test_the_icon_badge_counts_what_is_still_broken(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER - 1)
    assert monitor.latest["alerting"] == 0  # still inside its grace
    run(fake)
    assert pushed[0][1]["badge"] == 1 and monitor.latest["alerting"] == 1
    fake.down.clear()
    run(fake)
    assert pushed[1][1]["badge"] == 0  # the "back to normal" clears it
    push.send("Farm: test", "hello")
    assert "badge" not in pushed[2][1]  # a test push leaves it alone


def test_reminder_while_still_broken(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    fake.now_value += dt.timedelta(seconds=config.REMIND_EVERY_S)
    run(fake)
    assert titles(pushed) == ["Farm: Boord - problem", "Farm: Boord - problem (still)"]


def test_several_problems_in_one_notification(fake, pushed):
    subscribe()
    fake.down.update({8000, 8040})
    run(fake, config.FAIL_AFTER)
    assert titles(pushed) == ["Farm: 2 problems"]


def test_warnings_never_push(fake, pushed):
    subscribe()
    fake.uptime = 60
    fake.disk_free = 3 * 1024 ** 3
    run(fake, 10)
    assert pushed == []


def test_a_restart_of_the_monitor_does_not_re_alert(fake, pushed):
    subscribe()
    fake.down.add(8000)
    run(fake, config.FAIL_AFTER)
    assert len(pushed) == 1
    monitor.reset()  # as after a restart: memory gone, data/state.json still there
    run(fake, 3)
    assert len(pushed) == 1


def test_gone_subscriptions_are_dropped(fake, pushed):
    subscribe()
    push._webpush = lambda sub, payload: 410
    push.send("t", "b")
    assert push.load_subs() == []


def test_a_real_push_is_encrypted_and_signed(monkeypatch):
    """The real pywebpush path, up to the HTTP request - proves the key file
    and the claims are in a form it accepts, which a fake _webpush can't."""
    import base64
    import importlib

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    importlib.reload(push)  # undo the fixture's fake _webpush
    receiver = ec.generate_private_key(ec.SECP256R1())
    p256dh = base64.urlsafe_b64encode(receiver.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
    auth = base64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode()
    push.public_key()
    push.add_sub({"endpoint": "https://web.push.apple.com/xyz", "keys": {"p256dh": p256dh, "auth": auth}}, "t")

    sent = {}

    class Resp:
        status_code = 201
        text = ""

    def fake_post(self, url, data=None, headers=None, timeout=None):
        sent.update(url=url, headers=headers, data=data)
        return Resp()

    import requests
    monkeypatch.setattr(requests.Session, "post", fake_post)
    monkeypatch.setattr(requests, "post", lambda url, **kw: fake_post(None, url, **kw))
    assert push.send("Farm: test", "hello")["sent"] == 1
    assert sent["url"] == "https://web.push.apple.com/xyz"
    assert sent["headers"]["content-encoding"] == "aes128gcm"
    assert sent["headers"]["authorization"].startswith("vapid t=")
    assert b"hello" not in sent["data"]
