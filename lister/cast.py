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
        raise ValueError("the cast ticket key must be at least 32 bytes")
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
