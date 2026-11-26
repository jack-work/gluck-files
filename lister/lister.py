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

import base64
import json
import logging
import os
import socket
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, Response, abort, render_template_string, request
from markupsafe import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import Acl, BadPath, canonical_object_uri, media_request, normalise
from caption_gate import CaptionGate, ManifestUnavailable
from place import Places, Unplaced
from save import UNDATED, day_of, kind_of
from urllib.parse import urlencode

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
CLIENT_PREFIXES_FILE = os.environ.get("LISTER_CLIENT_PREFIXES_FILE", "")
SUBTITLE_MANIFEST_FILE = os.environ.get("LISTER_SUBTITLE_MANIFEST", "")
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

S3_CONFIG = Config(
    signature_version="s3v4",
    retries={"max_attempts": 2, "mode": "standard"},
    connect_timeout=3,
    read_timeout=15,
)
s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name=REGION, config=S3_CONFIG)
PLACES = Places(s3, os.environ.get("LISTER_SHARED_BUCKET", "files"), ENDPOINT, REGION, S3_CONFIG,
                keys_dir=os.environ.get("FILES_USER_KEYS", ""),
                provision_socket=os.environ.get("FILES_PROVISION_SOCKET", ""))

def caller():
    """The authenticated user, or None. Absence is refusal, not anonymity."""
    return (request.headers.get("Remote-User") or "").strip() or None


def caller_groups():
    raw = request.headers.get("Remote-Groups") or ""
    return [g.strip() for g in raw.split(",") if g.strip()]


SHOWN_SAFE = "/._-~@+"
ALLOWED_QUIET_SECONDS = 600
ALLOWED_SEEN_MAX = 4096
_allowed_seen = OrderedDict()
_allowed_lock = threading.Lock()


def shown(text):
    return quote(text or "-", safe=SHOWN_SAFE) or "-"


def decided(allowed, action, bucket, key, user):
    line = "%s %s %s/%s to %s" % (
        "allowed" if allowed else "denied", action, shown(bucket), shown(key), shown(user))
    if allowed:
        now = time.monotonic()
        with _allowed_lock:
            last = _allowed_seen.get((user, bucket, key, action))
            if last is not None and now - last < ALLOWED_QUIET_SECONDS:
                return
            _allowed_seen[(user, bucket, key, action)] = now
            _allowed_seen.move_to_end((user, bucket, key, action))
            while len(_allowed_seen) > ALLOWED_SEEN_MAX:
                _allowed_seen.popitem(last=False)
    log.info("%s", line)


def asked_path():
    return unquote((request.headers.get("X-Forwarded-Uri") or "").partition("?")[0].lstrip("/"))


def valid_prefix(prefix):
    if not isinstance(prefix, str) or len(prefix) < 2 or not prefix.endswith("/") or prefix.startswith("/"):
        return False
    try:
        return normalise(prefix) == prefix
    except BadPath:
        return False


def load_client_prefixes(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        logging.getLogger("gluck-files").error("client prefixes unreadable, every bearer client is refused: %s", e)
        return {}
    scopes = {}
    for client, prefixes in data.items():
        if prefixes is None:
            scopes[client] = None
            continue
        if not isinstance(prefixes, list):
            logging.getLogger("gluck-files").error("client %s: prefixes are not a list, it is refused", client)
            scopes[client] = []
            continue
        kept = []
        for prefix in prefixes:
            if valid_prefix(prefix):
                kept.append(prefix)
            else:
                logging.getLogger("gluck-files").error("client %s: prefix %r dropped", client, prefix)
        scopes[client] = kept
    return scopes


CLIENT_PREFIXES = load_client_prefixes(CLIENT_PREFIXES_FILE)


def client_scope():
    client = (request.headers.get("Remote-Client") or "").strip()
    if not client:
        return None
    return CLIENT_PREFIXES.get(client, [])


def in_scope(key, scope):
    return scope is None or any(key.startswith(p) for p in scope)


def library_scope(prefix, scope):
    if any(prefix.startswith(p) for p in scope):
        return [prefix]
    return [p for p in scope if p.startswith(prefix)]


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


def listing(bucket, prefix, max_keys=None):
    dirs, files, truncated, token = [], [], False, None
    try:
        client, where, inner = PLACES.at(bucket, prefix)
    except Unplaced:
        return dirs, files, truncated, 0
    outer = PLACES.outer(bucket, prefix)
    while True:
        kw = {"Bucket": where, "Prefix": inner, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)

        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"][len(inner):].rstrip("/")
            if name:
                dirs.append({"name": name, "href": "/" + outer + cp["Prefix"]})

        for obj in page.get("Contents", []):
            key = outer + obj["Key"]
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
                    "dt": obj.get("LastModified"),
                }
            )

        if len(dirs) + len(files) >= (max_keys or MAX_KEYS):
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
                     ".mp4", ".m4v", ".mov", ".webm", ".ogv",
                     ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")


PLAYER_TYPES = {t: t.split("/", 1)[0] for t in (
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/x-wav", "audio/ogg", "audio/opus",
    "audio/flac", "video/mp4", "video/x-m4v", "video/quicktime", "video/webm", "video/ogg")}
PLAYER_TYPES.update({t: "image" for t in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif")})


def plays_in_viewer(key):
    return key.lower().endswith(PLAYER_EXTENSIONS)


def viewer_href(uri):
    if MEDIA and uri.lower().endswith(MEDIA_PLAYER_EXTENSIONS) and not uri.startswith("/users/"):
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


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
PICK_CAP = 60


def day_label(day):
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return "No date"
    return f"{DAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


GRID_MAX = 20000
PAGE_DEFAULT = 200
PAGE_MAX = 500
THUMB_EDGE = 320
NODAY = "0000-00-00"


def thumbs_for(client, where, inner, mine):
    """name -> etag for the small copies that exist beside this folder's files."""
    if not mine:
        return {}
    found, token = {}, None
    base = ".thumbs/" + inner
    while True:
        kw = {"Bucket": where, "Prefix": base, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        try:
            page = client.list_objects_v2(**kw)
        except ClientError:
            return {}
        for o in page.get("Contents", []):
            n = o["Key"][len(base):]
            if n.endswith(".jpg") and o.get("Size", 0) > 0:
                found[n[:-4]] = (o.get("ETag") or "").strip('"')
        if not page.get("IsTruncated"):
            return found
        token = page.get("NextContinuationToken")
        if not token:
            return found


def provino(files, prefix, select, picks, thumbs, home, limit, after, query):
    """Sort a folder date-descending then by name, cut one page after the cursor,
    and group it into dated runs. All state lives in the query, so it is links."""
    here = "/" + prefix
    files = [f for f in files if not f["name"].startswith(".")]
    for f in files:
        f["kind"] = kind_of(f["name"])
        f["day"] = day_of(f["name"], None)
        if f["day"] == UNDATED:
            f["day"] = NODAY
        f["heic"] = f["name"].lower().endswith((".heic", ".heif"))
        f["picked"] = f["name"] in picks
        tag = thumbs.get(f["name"])
        f["thumb"] = bool(tag) and f["kind"] in ("image", "video")
        if f["thumb"]:
            f["thumb_url"] = "/_thumb/" + home + prefix[len(home):] + quote(f["name"], safe="/") + "?v=" + quote(tag, safe="")
            f["thumb_w"] = f["thumb_h"] = THUMB_EDGE
    ordered = sorted(files, key=lambda f: f["name"], reverse=True)
    ordered = sorted(ordered, key=lambda f: f["day"] == NODAY)
    media = [f for f in ordered if f["kind"] in ("image", "video")]
    other = [f for f in ordered if f["kind"] not in ("image", "video")]
    total = len(media) + len(other)
    cur = after or None
    if cur:
        sk = lambda n: (day_of(n, None) == UNDATED, [-ord(c) for c in n])
        media = [f for f in media if sk(f["name"]) > sk(cur)]
    page, rest = media[:limit], media[limit:]
    runs = []
    for f in page:
        if not runs or runs[-1]["day"] != f["day"]:
            runs.append({"day": f["day"], "label": day_label(f["day"]), "month": f["day"][:7], "files": [],
                         "cont": bool(cur and not runs and f["name"] and day_of(cur, None) == (f["day"] if f["day"] != NODAY else UNDATED))})
        runs[-1]["files"].append(f)
    nxt = None
    if rest:
        last = page[-1]
        nxt = here + "?" + urlencode(query + [("after", last["name"])])
    if select:
        for f in media:
            keep = [n for n in sorted(picks) if n != f["name"]] if f["picked"] else sorted(picks | {f["name"]})
            f["toggle"] = here + "?" + urlencode(query + ([("after", after)] if after else []) + [("select", "1")] + [("pick", n) for n in keep[:PICK_CAP]])
    return runs, (other if not cur else []), len(page), total, nxt, bool(cur)


def render(bucket, prefix, user, groups):
    may_list = ACL.allows(user, groups, prefix, "list")

    if may_list:
        dirs, files, truncated, shown = listing(bucket, prefix, GRID_MAX)
    else:
        subs = granted_subtrees(user, groups, prefix)
        if not subs:
            abort(404)
        dirs = [{"name": s.rstrip("/"), "href": "/" + prefix + s} for s in subs]
        files, truncated, shown = [], False, len(dirs)

    home = "users/" + user + "/"
    mine = prefix.startswith(home)
    home_rel = prefix[len(home):] if mine else None
    select = mine and request.args.get("select") == "1"
    picks = set(request.args.getlist("pick")[:PICK_CAP]) if select else set()

    try:
        limit = max(1, min(PAGE_MAX, int(request.args.get("limit", PAGE_DEFAULT))))
    except ValueError:
        limit = PAGE_DEFAULT
    after = request.args.get("after") or ""
    query = [("order", "desc")]
    if request.args.get("limit"):
        query.append(("limit", str(limit)))
    thumbs = {}
    if mine and may_list and files:
        try:
            client, where, inner = PLACES.at(bucket, prefix)
            thumbs = thumbs_for(client, where, inner, True)
        except (Unplaced, ClientError):
            thumbs = {}
    dirs = [d for d in dirs if not d["name"].startswith(".")]
    days, other, shown_media, total_files, next_href, continued = provino(
        files, prefix, select, picks, thumbs, home, limit, after, query)
    nmedia = sum(1 for f in files if kind_of(f["name"]) in ("image", "video") and not f["name"].startswith("."))
    if continued:
        dirs = []
    total = sum(f["bytes"] for f in files)
    bits = []
    if dirs:
        bits.append(f"{len(dirs)} folder" + ("s" if len(dirs) != 1 else ""))
    if files:
        bits.append(f"{len(files)} file" + ("s" if len(files) != 1 else ""))
        bits.append(human(total))
    tally = " \u00b7 ".join(bits) if bits else "empty"

    name = [p for p in prefix.split("/") if p]
    crumbs = crumbs_for(prefix)
    heading = name[-1] if name else bucket
    if mine:
        crumbs = [{"name": "Your files", "href": "/" + home}] + [c for c in crumbs if len(c["href"]) > len(home) + 1]
        heading = "Your files" if home_rel == "" else name[-1]

    save_href = day_href = None
    if mine and (nmedia or dirs):
        save_href = "/_save/?" + urlencode({"root": home_rel})
        day_href = lambda month: "/_save/?" + urlencode({"root": home_rel, "month": month})
    here = "/" + prefix
    pick_href = None
    if select and picks:
        pick_href = "/_save/?" + urlencode([("root", home_rel)] + [("pick", n) for n in sorted(picks)][:PICK_CAP + 1])

    return PAGE_TEMPLATE.render(
        scheme=reader_scheme(),
        title=("/" + prefix if prefix else bucket) + " \u00b7 kelliher.info",
        heading=heading,
        crumbs=crumbs,
        parent=parent_of(prefix) if prefix else None,
        dirs=dirs,
        files=files,
        days=days,
        other=other,
        tally=tally,
        bucket=bucket,
        truncated=truncated,
        shown=shown,
        mine=mine,
        is_home=mine and home_rel == "",
        upload_href="/_upload/" if mine else None,
        save_href=save_href,
        day_href=day_href,
        select_href=(here + "?select=1") if mine and nmedia and not select else None,
        select=select,
        picks=len(picks),
        pick_href=pick_href,
        done_href=here,
        clear_href=here + "?select=1",
        pick_cap=PICK_CAP,
        next_href=next_href,
        next_n=limit,
        shown_n=shown_media,
        total_n=total_files,
        first_href=here,
        continued=continued,
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


VIEWER_PAGE = (
    "<!doctype html><html lang=en{% if scheme %} data-scheme=\"{{ scheme }}\"{% endif %}>"
    "<meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
    "<meta name=color-scheme content='dark light'>"
    "<style>"
    ":root{color-scheme:dark;--g:#d9a94e;--i:#f0dfc8;--m:#a5887b}:root[data-scheme=light]{color-scheme:light}"
    "body{margin:0;background:#0a0506;color:var(--i);font:18px/1.4 ui-monospace,Menlo,Consolas,monospace}"
    ".top{display:flex;align-items:center;gap:10px;padding:8px 10px}"
    ".top a{display:inline-flex;align-items:center;min-height:44px;padding:0 12px;color:var(--i);text-decoration:none}"
    ".top .dl{margin-left:auto;border:3px solid var(--g);color:var(--g)}"
    "h1{flex:1;min-width:0;margin:0;font-size:18px;font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".stage{position:relative;display:grid;place-items:center;min-height:60vh}"
    ".stage img,.stage video{max-width:100%;max-height:78vh;display:block}"
    ".stage audio{width:min(92%,520px)}"
    ".arrow{position:absolute;top:50%;translate:0 -50%;display:grid;place-items:center;width:48px;height:80px;"
    "background:rgba(10,5,6,.7);color:var(--i);text-decoration:none;font-size:30px}"
    ".arrow.l{left:0}.arrow.r{right:0}"
    ".meta{padding:10px 14px 24px;color:var(--m)}"
    ".note{margin:10px 14px;padding:10px 12px;border:2px solid var(--g);font:17px/1.4 Georgia,serif;color:var(--i)}"
    "</style>"
    "<title>{{ title }}</title>"
    "<div class=top><a href=\"{{ folder }}\" aria-label=Back>&#8249; Back</a><h1>{{ title }}</h1>"
    "<a class=dl href=\"{{ uri }}?download=1\" download>Download</a></div>"
    "<div class=stage>"
    "{% if kind == 'audio' %}<audio controls preload=metadata src=\"{{ uri }}\"></audio>"
    "{% elif kind == 'video' %}<video controls preload=metadata src=\"{{ uri }}\"></video>"
    "{% elif kind == 'image' %}<img src=\"{{ uri }}\" alt=\"{{ title }}\">{% endif %}"
    "{% if prev_href %}<a class='arrow l' href=\"{{ prev_href }}\" aria-label=Previous>&#8249;</a>{% endif %}"
    "{% if next_href %}<a class='arrow r' href=\"{{ next_href }}\" aria-label=Next>&#8250;</a>{% endif %}"
    "</div>"
    "<div class=meta>{{ size }}{% if when %} &middot; {{ when }}{% endif %}</div>"
    "{% if heic %}<div class=note>HEIC is the iPhone's own photo format. Apple devices show it as is. "
    "On other devices this page may stay blank: Download gives the original.</div>{% endif %}"
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
        client, where, inner = PLACES.at(bucket, key)
        meta = client.head_object(Bucket=where, Key=inner)
    except (ClientError, Unplaced):
        abort(404)
    kind = PLAYER_TYPES.get(meta.get("ContentType"))
    if kind is None and meta.get("ContentType") in ("application/octet-stream", "binary/octet-stream") \
            and key.lower().endswith((".heic", ".heif")):
        kind = "image"
    prev_href = next_href = None
    here = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
    if kind in ("image", "video") and ACL.allows(user, caller_groups(), here, "list"):
        try:
            _dirs, siblings, _t, _n = listing(bucket, here)
            names = [f["name"] for f in sorted(
                (f for f in siblings if kind_of(f["name"]) in ("image", "video")),
                key=lambda f: (day_of(f["name"], f.get("dt")), f["name"].lower()), reverse=True)]
            me = key[len(here):]
            if me in names:
                i = names.index(me)
                link = lambda n: viewer_href(quote("/" + here + n, safe="/"))
                prev_href = link(names[i - 1]) if i > 0 else None
                next_href = link(names[i + 1]) if i + 1 < len(names) else None
        except (ClientError, Unplaced):
            pass
    return render_template_string(VIEWER_PAGE, title=key.rsplit("/", 1)[-1], uri=uri, kind=kind, scheme=reader_scheme(),
                                  folder="/" + here, prev_href=prev_href, next_href=next_href,
                                  heic=key.lower().endswith((".heic", ".heif")),
                                  size=human(meta.get("ContentLength", 0)), when=when(meta.get("LastModified"))), {"Vary": "Cookie"}


@app.get("/_files/authz")
def authorise_object():
    headers = {"Cache-Control": "no-store"}
    user = caller()
    bucket = bucket_for(request.host)
    def deny(status=403, key=None):
        decided(False, "read", bucket, asked_path() if key is None else key, user)
        return Response("unauthenticated\n" if status == 401 else "denied\n", status=status, headers=headers)
    if not user:
        return deny(401)
    if bucket is None:
        return deny()
    if (request.headers.get("X-Forwarded-Uri") or "").startswith("/_media/"):
        return deny()
    if request.headers.get("X-Forwarded-Method") not in ("GET", "HEAD"):
        return deny()
    try:
        key, uri = canonical_object_uri(request.headers.get("X-Forwarded-Uri"))
    except (BadPath, ValueError):
        return deny()
    if not in_scope(key, client_scope()) or not ACL.allows(user, caller_groups(), key, "read"):
        return deny(key=key)
    decided(True, "read", bucket, key, user)
    if (request.headers.get("Sec-Fetch-Dest") == "document" and plays_in_viewer(key)
            and "download=1" not in request.headers.get("X-Forwarded-Uri", "").partition("?")[2].split("&")):
        headers["Location"] = viewer_href(uri)
        return Response("", status=303, headers=headers)
    if PLACES.outer(bucket, key):
        try:
            headers["X-Files-Signed"] = PLACES.signed_get(bucket, key, request.headers.get("X-Forwarded-Method"))
        except Unplaced:
            return deny(404, key=key)
    else:
        headers["X-Files-URI"] = uri
    return Response("", status=200, headers=headers)


@app.get("/_files/authz-media")
def authorise_media():
    headers = {"Cache-Control": "no-store"}
    user = caller()
    bucket = bucket_for(request.host)
    def deny(status=403, action="read", key=None):
        decided(False, action, bucket, asked_path() if key is None else key, user)
        return Response("unauthenticated\n" if status == 401 else "denied\n", status=status, headers=headers)
    if not user:
        return deny(401)
    if not MEDIA or bucket is None:
        return deny()
    try:
        perm, key, echo = media_request(request.headers.get("X-Forwarded-Uri"), request.headers.get("X-Forwarded-Method"))
    except (BadPath, ValueError):
        return deny()
    if key.startswith("users/"):
        return deny(action=perm, key=key)
    scope = client_scope()
    groups = caller_groups()
    shown_scope = None
    if scope is not None and perm == "list":
        shown_scope = [p for p in library_scope(key, scope) if ACL.allows(user, groups, p, perm)]
        if not shown_scope:
            return deny(action=perm, key=key)
    elif not in_scope(key, scope) or not ACL.allows(user, groups, key, perm):
        return deny(action=perm, key=key)
    uri = request.headers.get("X-Forwarded-Uri") or ""
    is_rendition = uri.partition("?")[0].startswith("/_media/r/")
    revision, name = unquote(echo).rsplit("/", 2)[-2:] if is_rendition else (None, None)
    is_caption = is_rendition and name.startswith("captions_")
    is_master = is_rendition and name == "master.m3u8"
    is_item = uri.partition("?")[0].startswith("/_media/api/item/")
    if is_caption or is_master or is_item:
        gate = CaptionGate(SUBTITLE_MANIFEST_FILE, s3, ACL, bucket, user, groups, scope)
        try:
            if is_caption and not gate.allows_object(key, revision, name):
                return deny(key=key)
            if (is_item or is_master) and request.headers.get("X-Forwarded-Method") in ("GET", "HEAD"):
                allowed = gate.allowed_ids(key, revision if is_master else None)
                headers["X-Files-Subtitle-Ids"] = json.dumps(allowed, separators=(",", ":"))
                if is_master and gate.associated_master(key, revision):
                    headers["X-Files-Subtitle-Master"] = "1"
        except ManifestUnavailable:
            if is_caption:
                return deny(key=key)
            headers["X-Files-Subtitle-Ids"] = "[]"
    decided(True, perm, bucket, key, user)
    headers["X-Files-URI"] = echo
    if shown_scope is not None:
        headers["X-Files-Scope"] = " ".join(quote(p, safe="/") for p in shown_scope)
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
    return render(bucket, key, user, groups), {"Vary": "Cookie", "Cache-Control": "no-cache"}


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
