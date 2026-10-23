"""Test fixtures.

Environment is set BEFORE config and friends are imported, because they read
it at import time. The check loop is switched off - each test runs the
checks itself, against a FakeSystem, so nothing here touches the network,
PowerShell or Tailscale.
"""
import datetime as dt
import json
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="boordmonitor-tests-")
os.environ["MONITOR_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["MONITOR_BOORD_DIR"] = os.path.join(_TMP, "Boord")
os.environ["MONITOR_NO_LOOP"] = "1"
# Off unless a test turns it on: every other test counts pushes, and the
# fake PC's clock sits inside the morning summary's window.
os.environ["MONITOR_DAILY_AT"] = "off"
os.environ["MONITOR_TAILSCALE"] = "tailscale"
os.makedirs(os.path.join(_TMP, "Boord", "data"), exist_ok=True)

import pytest  # noqa: E402

import config  # noqa: E402
from system import Probe  # noqa: E402

NOW = dt.datetime(2026, 10, 18, 8, 0, 0)
DNS = "farm-pc.tail1234.ts.net"


def by_id(results):
    return {c["id"]: c for c in results}


def ts_status(state="Running", online=True, key_expiry=None, health=None, peers=None):
    me = {"DNSName": DNS + ".", "Online": online, "TailscaleIPs": ["100.64.0.1"]}
    if key_expiry:
        me["KeyExpiry"] = key_expiry
    out = {"BackendState": state, "Self": me, "Health": health or []}
    if peers:
        out["Peer"] = {f"nodekey:{i}": p for i, p in enumerate(peers)}
    return out


def peer(host, online=True, last_seen=None, key_expiry=None, os="iOS", dns=None, sharee=False):
    """One entry of `tailscale status --json`'s Peer map."""
    p = {"HostName": host, "DNSName": (dns or host) + ".tail1234.ts.net.", "OS": os,
         "Online": online, "TailscaleIPs": ["100.64.0.9"]}
    if last_seen:
        p["LastSeen"] = last_seen
    if key_expiry:
        p["KeyExpiry"] = key_expiry
    if sharee:
        p["ShareeNode"] = True
    return p


def serve_for(mapping, funnel=()):
    """{ts_port: local_port} -> the JSON `tailscale serve status --json` prints."""
    web = {f"{DNS}:{p}": {"Handlers": {"/": {"Proxy": f"http://localhost:{lp}"}}} for p, lp in mapping.items()}
    out = {"TCP": {str(p): {"HTTPS": True} for p in mapping}, "Web": web}
    if funnel:
        out["AllowFunnel"] = {f"{DNS}:{p}": True for p in funnel}
    return out


GOOD_SERVE = {443: 8000, 8443: 8010, 9443: 8020, 8030: 8030, 8040: 8040, 8050: 8050, 8060: 8060}
ALL_TASKS = ["Boord Server", "Boord Owner Server", "Boord Notes Server", "Kudde Server",
             "Boord Labour Server", "Boord Packhouse Server", "Boord Watchdog", "Boord Heartbeat"]


class FakeSystem:
    """A farm PC where everything is fine, until a test says otherwise.

    `down` is a set of local ports that refuse connections; `tailnet_down` a
    set of tailnet ports that do. `http_override` maps a URL to a Probe.
    """

    def __init__(self, windows=True):
        self.windows = windows
        self.now_value = NOW
        self.down = set()
        self.tailnet_down = set()
        self.http_override = {}
        self.tasks = {n: {"Name": n, "State": "Running" if "Server" in n else "Ready",
                          "LastRun": (NOW - dt.timedelta(minutes=2)).isoformat() + "Z", "LastResult": 0}
                      for n in ALL_TASKS}
        self.ts = ts_status()
        self.ts_rc = 0
        self.serve = serve_for(GOOD_SERVE)
        self.cert_expiry = NOW + dt.timedelta(days=60)
        self.disk_free = 120 * 1024 ** 3
        self.uptime = 3 * 86400
        self.log_text = None
        self.drive_running = True
        self.folders = {}  # folder -> newest file's (path, mtime) or None; absent = can't open
        self.boord = None  # what boord_devices returns
        self.update = {"reboot_pending": False, "reasons": [], "active_hours": [6, 18], "scheduled": None}
        # The PC's own clock: South Africa, two hours ahead of NOW (UTC).
        self.local_value = NOW + dt.timedelta(hours=2)
        self.versions = {8000: {"version": "3.15", "tag": "v3.15", "migrations_applied": True,
                                "backups": {"last": "2026-10-18 02:00", "count": 14}, "update": None},
                         8040: {"tag": "v1.0.2"}, 8050: {"tag": "v1.0.0"},
                         8060: {"tag": "v0.18"}}
        self.calls = []

    def now(self):
        return self.now_value

    def is_windows(self):
        return self.windows

    def run(self, args, timeout=20):
        self.calls.append(args)
        if args[1:] == ["status", "--json"]:
            if self.ts_rc == 127:
                return 127, "", "tailscale not found"
            return 0, json.dumps(self.ts), ""
        if args[1:] == ["serve", "status", "--json"]:
            return 0, json.dumps(self.serve), ""
        raise AssertionError(f"unexpected command {args}")

    def powershell(self, script, timeout=30):
        self.calls.append(["powershell"])
        assert "Get-ScheduledTaskInfo" in script
        return 0, json.dumps(list(self.tasks.values())), ""

    def http(self, url, timeout=10, want_json=False, headers=None):
        self.calls.append(url)
        if url in self.http_override:
            return self.http_override[url]
        from urllib.parse import urlparse
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        if u.scheme == "http" and port in self.down:
            return Probe(False, error="connection refused")
        if u.scheme == "https" and port in self.tailnet_down:
            return Probe(False, error="no answer within 10 s")
        if u.scheme == "https":
            # Over the tailnet a port only answers if serve maps it and the
            # app behind it is up.
            mapped = {int(k.rsplit(":", 1)[1]): v for k, v in self.serve.get("Web", {}).items()}
            if port not in mapped:
                return Probe(False, error="connection refused")
            local = int(mapped[port]["Handlers"]["/"]["Proxy"].rsplit(":", 1)[1])
            if local in self.down:
                return Probe(True, 502, 20)
        body = self.versions.get(port) if want_json else None
        return Probe(True, 200, 15, body=body)

    def tls_expiry(self, host, port, timeout=10):
        return self.cert_expiry, ""

    def disk(self, path):
        return 500 * 1024 ** 3, self.disk_free

    def memory(self):
        return 16 * 1024 ** 3, 8 * 1024 ** 3

    def uptime_s(self):
        return self.uptime

    def read_tail(self, path, max_bytes=65536):
        return self.log_text

    def newest_file(self, folder, max_files=5000):
        if folder not in self.folders:
            raise FileNotFoundError(folder)
        return self.folders[folder]

    def boord_devices(self, db_path):
        return self.boord

    def windows_update(self):
        return self.update if self.windows else None

    def local_now(self):
        return self.local_value

    def process_running(self, image):
        return self.drive_running if self.windows else None


@pytest.fixture
def fake():
    return FakeSystem()


@pytest.fixture(autouse=True)
def clean_state():
    import admin_auth
    import monitor
    import push
    import system
    monitor.reset()
    system._cache.clear()
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    admin_auth._failures, admin_auth._locked_until = 0, 0.0
    push_calls = []
    push._webpush = lambda sub, payload: push_calls.append((sub, json.loads(payload))) or 201
    yield push_calls


@pytest.fixture
def pushed(clean_state):
    return clean_state


@pytest.fixture
def client(fake, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    monkeypatch.setattr(main, "System", lambda: fake)
    # https, because the admin cookie is Secure and httpx will not send a
    # Secure cookie back over plain http.
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c


ADMIN_PIN = "4321"


@pytest.fixture
def admin_client(client):
    import admin_auth
    admin_auth.set_pin(ADMIN_PIN)
    assert client.post("/api/admin/login", json={"pin": ADMIN_PIN}).status_code == 200
    return client
