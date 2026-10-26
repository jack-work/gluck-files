"""Cast tickets: one object, one screen, a few hours, for a caster who is not there.

A person casting from tv.kelliher.info asks this lister, through their own
Authelia session, for a ticket naming one key and the device id the hub gave
them. The lister checks `read` for that person and signs

    {"v": 1, "c": caster, "g": [groups], "k": key, "d": device, "e": expiry}

with HMAC-SHA256. The hub keeps the ticket and, each time the screen fetches a
range, sends it with the device id to /_files/authz-cast. The ticket is checked
again there: signature, expiry, the device, and `read` on the key for the caster
against the ACL as it is at that moment. A grant removed from the ACL stops the
next range.

Groups are frozen into the ticket because the cast path has no Authelia and the
lister has no directory read yet. So taking someone out of a group lags by at
most the ticket's lifetime, CAP_SECONDS, for one object on one device, and
there is no renewal. Rotating the cast key and restarting the lister voids every
ticket at once: that is the kill switch.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time

CAP_SECONDS = 4 * 3600
DEVICE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class BadTicket(ValueError):
    pass


def load_key():
    path = os.environ.get("LISTER_CAST_KEY_FILE")
    if not path:
        cred = os.environ.get("CREDENTIALS_DIRECTORY")
        if not cred:
            return None
        path = os.path.join(cred, "cast-ticket-key")
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        return None
    if len(key) < 32:
        raise RuntimeError("the cast ticket key must be at least 32 bytes")
    return key


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint(secret, caster, groups, key, device, lifetime=CAP_SECONDS, now=None):
    if not DEVICE.match(device or ""):
        raise BadTicket("device")
    lifetime = min(int(lifetime), CAP_SECONDS)
    exp = int(now if now is not None else time.time()) + lifetime
    body = json.dumps(
        {"v": 1, "c": caster, "g": sorted(set(groups)), "k": key, "d": device, "e": exp},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    payload = _b64(body)
    sig = _b64(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    return payload + "." + sig, exp


def verify(secret, ticket, device, now=None):
    if not ticket or ticket.count(".") != 1:
        raise BadTicket("shape")
    payload, sig = ticket.split(".")
    want = _b64(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        raise BadTicket("signature")
    try:
        t = json.loads(_unb64(payload))
    except ValueError as e:
        raise BadTicket("payload") from e
    if t.get("v") != 1 or not isinstance(t.get("c"), str) or not isinstance(t.get("k"), str):
        raise BadTicket("payload")
    if not isinstance(t.get("g"), list) or not all(isinstance(g, str) for g in t["g"]):
        raise BadTicket("payload")
    if not isinstance(t.get("e"), int) or t["e"] < int(now if now is not None else time.time()):
        raise BadTicket("expired")
    if not device or not hmac.compare_digest(t.get("d", ""), device):
        raise BadTicket("device")
    return t


def ticket_groups(ticket):
    return list(ticket["g"])
