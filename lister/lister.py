#!/usr/bin/env python3
"""Directory listing for the files bucket, computed per request."""

import os
import sys
from datetime import timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, Response, abort, render_template_string, request
from markupsafe import escape

BUCKETS = frozenset(
    b for b in os.environ.get("LISTER_BUCKETS", "files").replace(",", " ").split() if b
)
ROOT_DOMAIN = os.environ.get("LISTER_ROOT_DOMAIN", "").lower()
ENDPOINT = os.environ.get("LISTER_ENDPOINT", "http://127.0.0.1:3900")
REGION = os.environ.get("LISTER_REGION", "spain")
TEMPLATE = os.environ["LISTER_TEMPLATE"]
MAX_KEYS = int(os.environ.get("LISTER_MAX_KEYS", "2000"))

with open(TEMPLATE, encoding="utf-8") as fh:
    PAGE = fh.read()

app = Flask(__name__)

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


def human(n):
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
    return f"{n:.0f} PiB"


def when(dt):
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


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


def bucket_for(host):
    """Resolve the bucket from the Host header, as Garage's web endpoint does.

    Host minus ROOT_DOMAIN is the bucket name, and it must appear in the
    allowlist. The allowlist is what makes a forged Host header uninteresting:
    without it, `graveyard.kelliher.info` would name a bucket that is
    deliberately not a website.
    """
    h = (host or "").split(":")[0].lower().rstrip(".")
    if not ROOT_DOMAIN:
        return next(iter(BUCKETS)) if len(BUCKETS) == 1 else None
    if not h.endswith(ROOT_DOMAIN):
        return None
    name = h[: -len(ROOT_DOMAIN)]
    if not name or "." in name or name not in BUCKETS:
        return None
    return name


def listing(bucket, prefix):
    dirs, files, truncated, token = [], [], False, None
    shown = 0
    while True:
        kw = {
            "Bucket": bucket,
            "Prefix": prefix,
            "Delimiter": "/",
            "MaxKeys": 1000,
        }
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

        shown = len(dirs) + len(files)
        if shown >= MAX_KEYS:
            truncated = True
            break
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return dirs, files, truncated, shown


def render(bucket, prefix):
    dirs, files, truncated, shown = listing(bucket, prefix)
    total = sum(f["bytes"] for f in files)
    bits = []
    if dirs:
        bits.append(f"{len(dirs)} folder" + ("s" if len(dirs) != 1 else ""))
    if files:
        bits.append(f"{len(files)} file" + ("s" if len(files) != 1 else ""))
        bits.append(human(total))
    tally = " \u00b7 ".join(bits) if bits else "empty"

    name = [p for p in prefix.split("/") if p]
    heading = name[-1] if name else bucket

    return render_template_string(
        PAGE,
        title=("/" + prefix if prefix else bucket) + " \u00b7 kelliher.info",
        heading=heading,
        crumbs=crumbs_for(prefix),
        parent=parent_of(prefix),
        dirs=dirs,
        files=files,
        tally=tally,
        bucket=bucket,
        truncated=truncated,
        shown=shown,
    )


@app.route("/")
def root():
    bucket = bucket_for(request.host)
    if bucket is None:
        abort(404)
    return render(bucket, "")


@app.route("/<path:prefix>/")
def sub(prefix):
    bucket = bucket_for(request.host)
    if bucket is None:
        abort(404)
    if ".." in prefix.split("/"):
        abort(400)
    return render(bucket, prefix.lstrip("/") + "/")


@app.route("/healthz")
def healthz():
    return Response("ok\n", mimetype="text/plain")


@app.errorhandler(ClientError)
def upstream(err):
    app.logger.error("garage: %s", err)
    return Response(
        f"<h1>Storage unavailable</h1><p>{escape(str(err.response.get('Error', {}).get('Code', 'error')))}</p>",
        status=502,
        mimetype="text/html",
    )


def main():
    from waitress import serve

    port = int(os.environ.get("LISTER_PORT", "9099"))
    serve(app, host="127.0.0.1", port=port, threads=4, ident=None)


if __name__ == "__main__":
    sys.exit(main())
