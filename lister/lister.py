#!/usr/bin/env python3
"""The gateway for files.kelliher.info. Every request passes through here.

Before this, Caddy sent directory requests to the lister and objects straight to
Garage, so an ACL table would have governed the listing and nothing else: a
control that looks real and does nothing. Now there is exactly one place where
a policy is checked on the object path, and it is this file.

  identity   Remote-User, stamped by Caddy after forward_auth. Caddy strips any
             client-supplied Remote-* first. A request without it is refused,
             not treated as anonymous.
  directory  needs `list` on the prefix
  object     never served here. Caddy asks /_files/authz, which needs `read` on
             the canonical key and answers without looking the object up, then
             streams it from Garage. No bytes pass through this process.
  denied     a listing is 404, never 403, so the bucket's shape is not an oracle.

Admin is not a special case in code: `group:files-admin` holds read/list/write
on the root, which inherits everywhere. One mechanism, one code path.
"""

import json
import logging
import os
import socket
import sys
from datetime import timezone
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, Response, abort, render_template_string, request
from markupsafe import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import Acl, BadPath, canonical_object_uri, media_request, normalise
import cast

BUCKETS = frozenset(
    b for b in os.environ.get("LISTER_BUCKETS", "files").replace(",", " ").split() if b
)
ROOT_DOMAIN = os.environ.get("LISTER_ROOT_DOMAIN", "").lower()
ENDPOINT = os.environ.get("LISTER_ENDPOINT", "http://127.0.0.1:3900")
REGION = os.environ.get("LISTER_REGION", "spain")
TEMPLATE = os.environ["LISTER_TEMPLATE"]
MAX_KEYS = int(os.environ.get("LISTER_MAX_KEYS", "2000"))
ACL_DB = os.environ.get("LISTER_ACL_DB", "/var/lib/gluck-files-lister/acl.db")
MAX_QUEUE = int(os.environ.get("LISTER_MAX_QUEUE", "8"))
MEDIA = os.environ.get("LISTER_MEDIA") == "1"
CAST_ORIGIN = os.environ.get("LISTER_CAST_ORIGIN", "")
try:
    CAST_KEY = cast.load_key()
except (OSError, ValueError) as e:
    logging.getLogger("gluck-files").error("casting is off: %s", e)
    CAST_KEY = None
MEDIA_PLAYER_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".aac", ".wav", ".oga", ".ogg", ".opus", ".flac")

with open(TEMPLATE, encoding="utf-8") as fh:
    PAGE = fh.read()

app = Flask(__name__)
PAGE_TEMPLATE = app.jinja_env.from_string(PAGE)
log = logging.getLogger("gluck-files")

ACL = Acl(ACL_DB)

# Admin is a GRANT, not a branch in the request path. Seeding it on a fresh
# database means there is one mechanism and one code path, so the admin case is
# exercised by the same checks as everyone else.
ADMIN_GROUP = os.environ.get("LISTER_ADMIN_GROUP", "files-admin")
if ACL.is_fresh() and ADMIN_GROUP:
    for _p in ("read", "list", "write"):
        ACL.grant(f"group:{ADMIN_GROUP}", "", _p, "bootstrap")

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    region_name=REGION,
    config=Config(
        signature_version="s3v4",
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=3,
        read_timeout=15,
    ),
)

def caller():
    """The authenticated user, or None. Absence is refusal, not anonymity."""
    return (request.headers.get("Remote-User") or "").strip() or None


def caller_groups():
    raw = request.headers.get("Remote-Groups") or ""
    return [g.strip() for g in raw.split(",") if g.strip()]


def bucket_for(host):
    """Resolve the bucket from Host, as Garage's web endpoint does, behind an
    allowlist. Without the allowlist a forged Host would name a bucket that was
    deliberately never a website."""
    h = (host or "").split(":")[0].lower().rstrip(".")
    if not ROOT_DOMAIN:
        return next(iter(BUCKETS)) if len(BUCKETS) == 1 else None
    if not h.endswith(ROOT_DOMAIN):
        return None
    name = h[: -len(ROOT_DOMAIN)]
    if not name or "." in name or name not in BUCKETS:
        return None
    return name


def human(n):
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
    return f"{n:.0f} PiB"


def when(dt):
    return "" if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def crumbs_for(prefix):
    out, walked = [], ""
    for part in [p for p in prefix.split("/") if p]:
        walked += part + "/"
        out.append({"name": part, "href": "/" + walked})
    return out


def parent_of(prefix):
    if not prefix:
        return None
    parts = [p for p in prefix.split("/") if p]
    return "/" + "".join(p + "/" for p in parts[:-1])


def listing(bucket, prefix):
    dirs, files, truncated, token = [], [], False, None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)

        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"][len(prefix):].rstrip("/")
            if name:
                dirs.append({"name": name, "href": "/" + cp["Prefix"]})

        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or key.endswith("/"):
                continue
            files.append(
                {
                    "name": key[len(prefix):],
                    "href": "/" + key,
                    "viewer": viewer_href(quote("/" + key, safe="/")) if plays_in_viewer(key) else None,
                    "size": human(obj.get("Size", 0)),
                    "bytes": obj.get("Size", 0),
                    "when": when(obj.get("LastModified")),
                }
            )

        if len(dirs) + len(files) >= MAX_KEYS:
            truncated = True
            break
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return dirs, files, truncated, len(dirs) + len(files)


PLAYER_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".oga", ".ogg", ".opus", ".flac",
                     ".mp4", ".m4v", ".mov", ".webm", ".ogv")


PLAYER_TYPES = {t: t.split("/", 1)[0] for t in (
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav", "audio/ogg", "audio/opus",
    "audio/flac", "video/mp4", "video/x-m4v", "video/quicktime", "video/webm", "video/ogg")}


def plays_in_viewer(key):
    return key.lower().endswith(PLAYER_EXTENSIONS)


def viewer_href(uri):
    if MEDIA and uri.lower().endswith(MEDIA_PLAYER_EXTENSIONS):
        return "/_media/play" + uri
    return "/_files/view?uri=" + quote(uri, safe="")


def granted_subtrees(user, groups, prefix):
    """When a caller cannot list `prefix` but holds grants beneath it, show just
    those. It reveals only paths already granted to them, and it stops a
    legitimate user landing on a dead end at the root."""
    out = []
    for perm in ("list", "read"):
        for p in ACL.readable_prefixes(user, groups, perm):
            if p and p != prefix and p.startswith(prefix):
                rest = p[len(prefix):]
                head = rest.split("/")[0]
                if head:
                    out.append(head + "/" if "/" in rest or p.endswith("/") else head)
    return sorted(set(out))


SCHEMES = {"light", "dark"}


def reader_scheme():
    scheme = request.cookies.get("zanni-scheme")
    return scheme if scheme in SCHEMES else None


def render(bucket, prefix, user, groups):
    may_list = ACL.allows(user, groups, prefix, "list")

    if may_list:
        dirs, files, truncated, shown = listing(bucket, prefix)
    else:
        subs = granted_subtrees(user, groups, prefix)
        if not subs:
            abort(404)
        dirs = [{"name": s.rstrip("/"), "href": "/" + prefix + s} for s in subs]
        files, truncated, shown = [], False, len(dirs)

    total = sum(f["bytes"] for f in files)
    bits = []
    if dirs:
        bits.append(f"{len(dirs)} folder" + ("s" if len(dirs) != 1 else ""))
    if files:
        bits.append(f"{len(files)} file" + ("s" if len(files) != 1 else ""))
        bits.append(human(total))
    tally = " \u00b7 ".join(bits) if bits else "empty"

    name = [p for p in prefix.split("/") if p]
    return PAGE_TEMPLATE.render(
        scheme=reader_scheme(),
        title=("/" + prefix if prefix else bucket) + " \u00b7 kelliher.info",
        heading=name[-1] if name else bucket,
        crumbs=crumbs_for(prefix),
        parent=parent_of(prefix) if prefix else None,
        dirs=dirs,
        files=files,
        tally=tally,
        bucket=bucket,
        truncated=truncated,
        shown=shown,
    )


@app.route("/healthz")
def healthz():
    return Response("ok\n", mimetype="text/plain")


class Admission:
    def __init__(self, limit):
        self._limit = limit
        self._dispatcher = None

    def watch(self, server):
        self._dispatcher = server.task_dispatcher

    def waiting(self):
        dispatcher = self._dispatcher
        if dispatcher is None:
            return 0
        with dispatcher.lock:
            return len(dispatcher.queue)

    def is_overloaded(self):
        return self.waiting() > self._limit


ADMISSION = Admission(MAX_QUEUE)


@app.before_request
def refuse_when_overloaded():
    if request.path == "/healthz" or not ADMISSION.is_overloaded():
        return None
    log.warning("shed %s, %d waiting", request.path, ADMISSION.waiting())
    return Response(
        "busy\n",
        status=503,
        mimetype="text/plain",
        headers={"Retry-After": "2", "Cache-Control": "no-store"},
    )


@app.get("/_files/view")
def media_viewer():
    user = caller()
    if not user:
        abort(401)
    bucket = bucket_for(request.host)
    if bucket is None:
        abort(403)
    try:
        key, uri = canonical_object_uri(request.args.get("uri"))
    except (BadPath, ValueError):
        abort(403)
    if not ACL.allows(user, caller_groups(), key, "read"):
        abort(403)
    try:
        meta = s3.head_object(Bucket=bucket, Key=key)
    except ClientError:
        abort(404)
    kind = PLAYER_TYPES.get(meta.get("ContentType"))
    return render_template_string(
        "<!doctype html><html{% if scheme %} data-scheme=\"{{ scheme }}\"{% endif %}>"
        "<meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<meta name=color-scheme content='light dark'>"
        "<style>:root[data-scheme=light]{color-scheme:light}:root[data-scheme=dark]{color-scheme:dark}</style>"
        "<title>{{ title }}</title><h1>{{ title }}</h1>"
        "{% if kind == 'audio' %}<audio controls preload=metadata src=\"{{ uri }}\"></audio>"
        "{% elif kind == 'video' %}<video controls preload=metadata src=\"{{ uri }}\"></video>{% endif %}"
        "<p><a href=\"{{ uri }}?download=1\" download>Download</a></p>",
        title=key.rsplit("/", 1)[-1], uri=uri, kind=kind, scheme=reader_scheme(),
    ), {"Vary": "Cookie"}


@app.get("/_files/authz")
def authorise_object():
    headers = {"Cache-Control": "no-store"}
    user = caller()
    if not user:
        return Response("unauthenticated\n", status=401, headers=headers)
    if bucket_for(request.host) is None:
        return Response("denied\n", status=403, headers=headers)
    if (request.headers.get("X-Forwarded-Uri") or "").startswith("/_media/"):
        return Response("denied\n", status=403, headers=headers)
    if request.headers.get("X-Forwarded-Method") not in ("GET", "HEAD"):
        return Response("denied\n", status=403, headers=headers)
    try:
        key, uri = canonical_object_uri(request.headers.get("X-Forwarded-Uri"))
    except (BadPath, ValueError):
        return Response("denied\n", status=403, headers=headers)
    if not ACL.allows(user, caller_groups(), key, "read"):
        return Response("denied\n", status=403, headers=headers)
    if (request.headers.get("Sec-Fetch-Dest") == "document" and plays_in_viewer(key)
            and "download=1" not in request.headers.get("X-Forwarded-Uri", "").partition("?")[2].split("&")):
        headers["Location"] = viewer_href(uri)
        return Response("", status=303, headers=headers)
    headers["X-Files-URI"] = uri
    return Response("", status=200, headers=headers)


def cast_enabled():
    return bool(CAST_ORIGIN) and CAST_KEY is not None


@app.post("/_files/cast-ticket")
def cast_ticket():
    if not cast_enabled():
        abort(404)
    origin = request.headers.get("Origin", "")
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    if origin != CAST_ORIGIN or request.headers.get("Sec-Fetch-Site", "same-site") not in ("same-site", "same-origin"):
        return Response("denied\n", status=403, headers=headers)
    headers.update({"Access-Control-Allow-Origin": CAST_ORIGIN, "Access-Control-Allow-Credentials": "true"})
    user = caller()
    if not user:
        return Response("unauthenticated\n", status=401, headers=headers)
    if bucket_for(request.host) is None:
        return Response("denied\n", status=403, headers=headers)
    try:
        body = json.loads(request.get_data(cache=False, as_text=True) or "{}")
        key, _ = canonical_object_uri(body.get("uri"))
        device = body.get("device")
        groups = caller_groups()
        if not ACL.allows(user, groups, key, "read"):
            return Response("denied\n", status=403, headers=headers)
        ticket, exp = cast.mint(CAST_KEY, user, groups, key, device)
    except (BadPath, ValueError, AttributeError, cast.BadTicket):
        return Response("bad request\n", status=400, headers=headers)
    log.info("cast ticket for %s to device %s", user, device)
    headers["Content-Type"] = "application/json"
    return Response(json.dumps({"ticket": ticket, "exp": exp}), status=200, headers=headers)


@app.get("/_files/authz-cast")
def authorise_cast():
    headers = {"Cache-Control": "no-store"}
    if not cast_enabled():
        return Response("denied\n", status=403, headers=headers)
    if request.headers.get("X-Forwarded-Method") not in ("GET", "HEAD"):
        return Response("denied\n", status=403, headers=headers)
    try:
        t = cast.verify(CAST_KEY, request.headers.get("X-Cast-Ticket", ""), request.headers.get("X-Cast-Device", ""))
        key, uri = canonical_object_uri(quote("/" + t["k"], safe="/"))
    except (cast.BadTicket, BadPath, ValueError):
        return Response("denied\n", status=403, headers=headers)
    if key != t["k"] or not ACL.allows(t["c"], cast.ticket_groups(t), key, "read"):
        return Response("denied\n", status=403, headers=headers)
    headers["X-Files-URI"] = uri
    return Response("", status=200, headers=headers)


@app.get("/_files/authz-media")
def authorise_media():
    headers = {"Cache-Control": "no-store"}
    user = caller()
    if not user:
        return Response("unauthenticated\n", status=401, headers=headers)
    if not MEDIA or bucket_for(request.host) is None:
        return Response("denied\n", status=403, headers=headers)
    try:
        perm, key, echo = media_request(request.headers.get("X-Forwarded-Uri"), request.headers.get("X-Forwarded-Method"))
    except (BadPath, ValueError):
        return Response("denied\n", status=403, headers=headers)
    if not ACL.allows(user, caller_groups(), key, perm):
        return Response("denied\n", status=403, headers=headers)
    headers["X-Files-URI"] = echo
    return Response("", status=200, headers=headers)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    user = caller()
    if not user:
        # Caddy strips client Remote-* and stamps its own after forward_auth.
        # No header means the request did not come through that path.
        log.warning("request with no Remote-User for %r", path)
        abort(401)

    bucket = bucket_for(request.host)
    if bucket is None:
        abort(404)

    try:
        key = normalise(path)
    except BadPath:
        abort(404)

    groups = caller_groups()
    is_dir = key == "" or key.endswith("/") or path.endswith("/")
    if is_dir and key and not key.endswith("/"):
        key += "/"

    if not is_dir:
        abort(404)
    return render(bucket, key, user, groups), {"Vary": "Cookie"}


@app.errorhandler(401)
def unauthorised(_):
    return Response("unauthenticated\n", status=401, mimetype="text/plain")


@app.errorhandler(ClientError)
def upstream(err):
    log.error("garage: %s", err)
    code = escape(str(err.response.get("Error", {}).get("Code", "error")))
    return Response(
        f"<h1>Storage unavailable</h1><p>{code}</p>",
        status=502,
        mimetype="text/html",
    )


def inherited_sockets():
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return []
    return [socket.socket(fileno=3 + i) for i in range(int(os.environ.get("LISTEN_FDS", "0")))]


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from waitress import create_server

    sockets = inherited_sockets()
    if sockets:
        server = create_server(app, sockets=sockets, threads=4, ident=None)
    elif os.environ.get("LISTER_LISTEN") == "socket":
        sys.exit("no listening socket from systemd; refusing to fall back to TCP")
    else:
        port = int(os.environ.get("LISTER_PORT", "9099"))
        server = create_server(app, host="127.0.0.1", port=port, threads=4, ident=None)
    ADMISSION.watch(server)
    server.run()


if __name__ == "__main__":
    sys.exit(main())
