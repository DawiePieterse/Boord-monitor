"""The gate on the road in from the internet.

This app has never checked who is asking. It binds 127.0.0.1 and
`tailscale serve` is the only thing that publishes it, so "reached us at
all" has always meant "is on the farm's tailnet", and that was the whole of
the access control.

The Cloudflare Tunnel is a second thing that can publish it - and its
connector runs on this PC, so its requests arrive on loopback looking
exactly like Tailscale's. Without this file, pointing the tunnel at this
port would hand the app to anybody on the internet who had the address, with
nothing in the app to say so.

So: a request carrying Cloudflare's headers came through the tunnel, and is
let in only if it is from a device the farm has paired (device_auth.py),
with a role this app admits. Everything else - Tailscale, the server's own
console - is untouched: no header, no check, exactly as before. An app that
admits no roles is simply closed through the tunnel, which is the right
default for one nobody has decided to open.

THE SAME FILE IN EVERY APP that has no gate of its own (Owner, Notes,
Labour, Monitor, Packhouse). Boord and Kudde already judged the network a
request arrived on, so they carry this rule inside their security.py.
"""
from fastapi import Request
from fastapi.responses import PlainTextResponse

import device_auth

# Cloudflare puts these on every request it passes on, and a visitor cannot
# take them off again - which is the whole use of them here. A tailnet
# device that adds them itself only gets itself checked for a cookie.
_CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip")

NOT_PAIRED_MESSAGE = (
    "This device is not paired with the farm for this app - sign in at Boord, "
    "or ask the office to add it from Boord Admin > Settings > Devices"
)
CLOSED_MESSAGE = "This app is only reachable over Tailscale"


def came_through_cloudflare(request: Request) -> bool:
    return any(header in request.headers for header in _CLOUDFLARE_HEADERS)


def install(app, roles) -> None:
    """Put the gate in front of every route and every served file of `app`.
    `roles` are the paired-device roles this app admits through the tunnel:
    any of "admin", "owner" and "field", or none at all."""
    roles = tuple(roles)

    @app.middleware("http")
    async def paired_devices_only_through_the_tunnel(request: Request, call_next):
        device = None
        if came_through_cloudflare(request):
            if not roles:
                return PlainTextResponse(CLOSED_MESSAGE, status_code=403)
            device = device_auth.verify(request.cookies.get(device_auth.COOKIE_NAME, ""))
            if device is None or device["role"] not in roles:
                return PlainTextResponse(NOT_PAIRED_MESSAGE, status_code=403)
        response = await call_next(request)
        # A device still in use is remembered for another full term, so a
        # phone that only ever opens this app is not logged out of it for
        # never having opened Boord.
        if device is not None and device_auth.needs_renewal(device):
            device_auth.renew(response, device)
        return response
