"""The road in from the internet (tunnel_gate.py).

This app has no gate of its own: reaching it at all used to mean being on
the tailnet. These pin down the one new rule - through the Cloudflare
Tunnel only a paired device with a role this app admits gets in - and, just
as much, that nothing changed for a request that did not come that way.

The same file in every app that carries tunnel_gate.py. Requests are put
through the real app by hand rather than through a client fixture, so it
depends on nothing but main.app and main.TUNNEL_ROLES.
"""
import asyncio
import time

import pytest

import device_auth
import main
import tunnel_gate

SECRET = "s" * 40
VIA_TUNNEL = {"cf-ray": "8a1b2c3d4e5f-JNB", "cf-connecting-ip": "41.13.200.7"}
# A path no app serves: whatever answers it, it is not the gate.
PATH = "/no-such-page-for-the-gate-test"


@pytest.fixture()
def farm_secret(tmp_path, monkeypatch):
    (tmp_path / device_auth.SECRET_FILE).write_text(SECRET + "\n")
    monkeypatch.setenv(device_auth.AUTH_DIR_ENV, str(tmp_path))
    return tmp_path


def cookie(role, expires_in=device_auth.LIFETIME_SECONDS, secret=SECRET, **extra):
    payload = dict({"id": "dev-" + role, "role": role, "exp": int(time.time()) + expires_in}, **extra)
    return device_auth.COOKIE_NAME + "=" + device_auth.sign(payload, secret.encode())


def call(headers=None, path=PATH):
    """(status, headers, text) for one GET, arriving on loopback - where both
    `tailscale serve` and the tunnel's connector arrive from."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "scheme": "https", "server": ("app.example", 443), "client": ("127.0.0.1", 54321),
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    got = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], got, body.decode("utf-8", "replace")


def refused(response):
    status, _, text = response
    return status == 403 and text in (tunnel_gate.NOT_PAIRED_MESSAGE, tunnel_gate.CLOSED_MESSAGE)


def test_without_the_tunnel_nothing_is_checked(farm_secret):
    """Tailscale and the console arrive with no Cloudflare headers, and are
    not asked for anything - with or without a cookie."""
    assert not refused(call())
    assert not refused(call({"cookie": cookie("dashboard")}))


def test_the_internet_gets_nothing_without_a_pairing(farm_secret):
    for path in (PATH, "/", "/api/version", "/healthz"):
        assert refused(call(VIA_TUNNEL, path)), path


def test_a_stranger_cannot_talk_their_way_in(farm_secret):
    """Everything a visitor can type: a tailnet name, a tailnet address, a
    cookie signed by somebody else, an expired one."""
    for extra in ({"host": "app.example.ts.net"}, {"x-forwarded-host": "app.example.ts.net"},
                  {"x-forwarded-for": "100.64.0.1"},
                  {"cookie": cookie("admin", secret="x" * 40)},
                  {"cookie": cookie("admin", expires_in=-1)},
                  {"cookie": device_auth.COOKIE_NAME + "=junk"}):
        assert refused(call(dict(VIA_TUNNEL, **extra))), extra


@pytest.mark.parametrize("role", ["admin", "owner", "field", "dashboard"])
def test_a_paired_device_gets_in_only_with_a_role_this_app_admits(farm_secret, role):
    """dashboard is the farm manager's view of Boord's harvest and opens no
    other app; the owner opens every app; which of admin and field open this
    one is main.TUNNEL_ROLES."""
    assert "dashboard" not in main.TUNNEL_ROLES
    assert "owner" in main.TUNNEL_ROLES
    response = call(dict(VIA_TUNNEL, cookie=cookie(role)))
    assert refused(response) == (role not in main.TUNNEL_ROLES)


def test_a_farm_that_has_paired_nothing_admits_nobody(tmp_path, monkeypatch):
    """No secret file: no cookie is believed, whatever it claims."""
    monkeypatch.setenv(device_auth.AUTH_DIR_ENV, str(tmp_path))
    assert refused(call(dict(VIA_TUNNEL, cookie=cookie("admin"))))


def test_a_revoked_device_is_refused(farm_secret):
    (farm_secret / device_auth.REVOKED_FILE).write_text("dev-admin  # lost\n")
    assert refused(call(dict(VIA_TUNNEL, cookie=cookie("admin"))))


def test_a_device_in_use_is_renewed_and_a_refused_one_never_is(farm_secret):
    if not main.TUNNEL_ROLES:
        pytest.skip("this app is closed through the tunnel, so nothing is ever renewed")
    role = main.TUNNEL_ROLES[0]
    _, headers, _ = call(dict(VIA_TUNNEL, cookie=cookie(role, expires_in=100 * 24 * 3600)))
    assert headers["set-cookie"].startswith(device_auth.COOKIE_NAME + "=")
    assert "set-cookie" not in call(dict(VIA_TUNNEL, cookie=cookie(role)))[1]
    assert "set-cookie" not in call(dict(VIA_TUNNEL, cookie=cookie(role, expires_in=-1)))[1]
