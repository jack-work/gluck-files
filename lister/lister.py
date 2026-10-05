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
  object     needs `read` on the key, then a SHORT-LIVED presigned URL and a
             302. No bytes pass through this process.
  denied     404, never 403, so the bucket's shape is not an oracle.

Admin is not a special case in code: `group:files-admin` holds read/list/write
on the root, which inherits everywhere. One mechanism, one code path.
"""

import logging
import os
import sys
from datetime import timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, Response, abort, redirect, render_template_string, request
from markupsafe import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import Acl, BadPath, normalise

BUCKETS = frozenset(
    b for b in os.environ.get("LISTER_BUCKETS", "files").replace(",", " ").split() if b
)
ROOT_DOMAIN = os.environ.get("LISTER_ROOT_DOMAIN", "").lower()
ENDPOINT = os.environ.get("LISTER_ENDPOINT", "http://127.0.0.1:3900")
PUBLIC_ENDPOINT = os.environ.get("LISTER_PUBLIC_ENDPOINT", "")
REGION = os.environ.get("LISTER_REGION", "spain")
TEMPLATE = os.environ["LISTER_TEMPLATE"]
MAX_KEYS = int(os.environ.get("LISTER_MAX_KEYS", "2000"))
ACL_DB = os.environ.get("LISTER_ACL_DB", "/var/lib/gluck-files-lister/acl.db")
# A presigned URL is a bearer capability until it expires. Minutes, not the
# seven days SigV4 would allow.
PRESIGN_SECONDS = int(os.environ.get("LISTER_PRESIGN_SECONDS", "120"))

with open(TEMPLATE, encoding="utf-8") as fh:
    PAGE = fh.read()

app = Flask(__name__)
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

# A separate client whose generated URLs point at the public hostname, so a
# redirect is followable from a browser. Same credentials, same signature.
presigner = (
    boto3.client(
        "s3",
        endpoint_url=PUBLIC_ENDPOINT,
        region_name=REGION,
        config=Config(signature_version="s3v4"),
    )
    if PUBLIC_ENDPOINT
    else s3
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
    return render_template_string(
        PAGE,
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

    if is_dir:
        return render(bucket, key, user, groups)

    if not ACL.allows(user, groups, key, "read"):
        log.info("denied read %s to %s", key, user)
        abort(404)

    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError:
        abort(404)

    url = presigner.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGN_SECONDS,
    )
    log.info("allowed read %s to %s", key, user)
    return redirect(url, code=302)


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from waitress import serve as waitress_serve

    port = int(os.environ.get("LISTER_PORT", "9099"))
    waitress_serve(app, host="127.0.0.1", port=port, threads=4, ident=None)


if __name__ == "__main__":
    sys.exit(main())
