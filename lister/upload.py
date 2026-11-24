#!/usr/bin/env python3
import json
import logging
import os
import re
import socket
import sys
import time
import unicodedata

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, Response, request

from acl import Acl, BadPath, normalise

BUCKET = os.environ.get("UPLOAD_BUCKET", "files")
ENDPOINT = os.environ.get("UPLOAD_ENDPOINT", "http://127.0.0.1:3900")
REGION = os.environ.get("UPLOAD_REGION", "spain")
ACL_DB = os.environ.get("UPLOAD_ACL_DB", "/var/lib/gluck-files-acl/acl.db")
ORIGIN = os.environ["UPLOAD_ORIGIN"]
PAGE_DIR = os.environ["UPLOAD_PAGE_DIR"]
PART_BYTES = 16 * 1024 * 1024
MAX_PARTS = 10000
MAX_UPLOAD_BYTES = 8 * 1024 ** 3
USER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

TYPES = {
    ".vcf": "text/vcard",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".3gp": "video/3gpp",
    ".pdf": "application/pdf",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = PART_BYTES + 1024
log = logging.getLogger("gluck-files-upload")
ACL = Acl(ACL_DB)
s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    region_name=REGION,
    config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                  connect_timeout=3, read_timeout=60),
)


def refuse(status, why):
    return Response(json.dumps({"error": why}), status=status, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


def ok(body):
    return Response(json.dumps(body), mimetype="application/json", headers={"Cache-Control": "no-store"})


def caller():
    user = (request.headers.get("Remote-User") or "").strip()
    groups = [g.strip() for g in (request.headers.get("Remote-Groups") or "").split(",") if g.strip()]
    return user, groups


def home(user):
    return f"users/{user}/"


def safe_name(name):
    name = unicodedata.normalize("NFC", os.path.basename(str(name or "").replace("\\", "/")))
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name[:120] or "file"


def stored_as(name):
    known = TYPES.get(os.path.splitext(name)[1].lower())
    if known:
        return {"ContentType": known}
    return {"ContentType": "application/octet-stream", "ContentDisposition": "attachment"}


def may_write(user, groups, key):
    try:
        return normalise(key) == key and ACL.allows(user, groups, key, "write")
    except BadPath:
        return False


def owned_upload(user, groups, key, upload_id):
    if not isinstance(key, str) or not isinstance(upload_id, str) or not upload_id:
        return False
    if not key.startswith(home(user)) or not may_write(user, groups, key):
        return False
    try:
        s3.list_parts(Bucket=BUCKET, Key=key, UploadId=upload_id, MaxParts=1)
    except ClientError:
        return False
    return True


def received(key, upload_id):
    parts, marker = [], 0
    while True:
        r = s3.list_parts(Bucket=BUCKET, Key=key, UploadId=upload_id, PartNumberMarker=marker)
        parts += [{"PartNumber": p["PartNumber"], "ETag": p["ETag"], "Size": p["Size"]} for p in r.get("Parts", [])]
        if not r.get("IsTruncated"):
            return parts
        marker = r["NextPartNumberMarker"]


def free_key(user, name):
    day = time.strftime("%Y-%m-%d")
    stem, ext = os.path.splitext(name)
    for i in range(100):
        key = f"{home(user)}uploads/{day}/{stem}{'' if i == 0 else f' ({i})'}{ext}"
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return key
            raise
    return None


@app.before_request
def gate():
    if request.headers.get("Authorization"):
        return refuse(403, "browser only")
    user, _ = caller()
    if not user:
        return refuse(401, "not signed in")
    if not USER.fullmatch(user):
        return refuse(403, "this account name can't own a folder")
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("Origin") != ORIGIN or request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return refuse(403, "wrong origin")
    return None


@app.get("/_upload/")
def page():
    return static("index.html", "text/html; charset=utf-8")


@app.get("/_upload/app.js")
def script():
    return static("app.js", "text/javascript; charset=utf-8")


@app.get("/_upload/app.css")
def style():
    return static("app.css", "text/css; charset=utf-8")


def static(name, mime):
    with open(os.path.join(PAGE_DIR, name), "rb") as f:
        return Response(f.read(), mimetype=mime.split(";")[0], headers={"Content-Type": mime, "Cache-Control": "no-cache"})


@app.get("/_upload/api/me")
def me():
    user, groups = caller()
    return ok({"user": user, "folder": home(user), "writable": may_write(user, groups, f"{home(user)}uploads/x"),
               "partBytes": PART_BYTES})


@app.post("/_upload/api/start")
def start():
    user, groups = caller()
    body = request.get_json(silent=True) or {}
    size = body.get("size")
    if not isinstance(size, int) or size < 0:
        return refuse(400, "bad size")
    if size > MAX_UPLOAD_BYTES:
        return refuse(413, "file larger than 8 GB")
    name = safe_name(body.get("name"))
    if os.path.splitext(name)[1].lower() not in TYPES:
        return refuse(415, "this kind of file can't be uploaded here")
    if not may_write(user, groups, f"{home(user)}uploads/{name}"):
        log.info("denied start %s for %s", name, user)
        return refuse(403, "no upload rights")
    key = free_key(user, name)
    if key is None:
        return refuse(409, "too many files with that name today")
    if not may_write(user, groups, key):
        return refuse(403, "no upload rights")
    r = s3.create_multipart_upload(Bucket=BUCKET, Key=key, **stored_as(name))
    log.info("started %s for %s, %d bytes", key, user, size)
    return ok({"key": key, "id": r["UploadId"], "partBytes": PART_BYTES})


@app.get("/_upload/api/parts")
def parts():
    user, groups = caller()
    key, upload_id = request.args.get("key"), request.args.get("id")
    if not owned_upload(user, groups, key, upload_id):
        return refuse(404, "no such upload")
    return ok({"parts": [{"n": p["PartNumber"], "size": p["Size"]} for p in received(key, upload_id)]})


@app.put("/_upload/api/part")
def part():
    user, groups = caller()
    key, upload_id = request.args.get("key"), request.args.get("id")
    try:
        n = int(request.args.get("n", ""))
    except ValueError:
        return refuse(400, "bad part number")
    if not 1 <= n <= MAX_PARTS:
        return refuse(400, "bad part number")
    if not owned_upload(user, groups, key, upload_id):
        return refuse(404, "no such upload")
    data = request.get_data(cache=False)
    if not data or len(data) > PART_BYTES:
        return refuse(400, "bad part size")
    s3.upload_part(Bucket=BUCKET, Key=key, UploadId=upload_id, PartNumber=n, Body=data)
    return ok({"n": n, "size": len(data)})


@app.post("/_upload/api/complete")
def complete():
    user, groups = caller()
    body = request.get_json(silent=True) or {}
    key, upload_id, size = body.get("key"), body.get("id"), body.get("size")
    if not owned_upload(user, groups, key, upload_id):
        return refuse(404, "no such upload")
    got = received(key, upload_id)
    if not got or [p["PartNumber"] for p in got] != list(range(1, len(got) + 1)):
        return refuse(409, "parts missing")
    if not isinstance(size, int) or sum(p["Size"] for p in got) != size or size > MAX_UPLOAD_BYTES:
        return refuse(409, "size does not match")
    s3.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id, MultipartUpload={
        "Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in got]})
    log.info("completed %s for %s, %d bytes", key, user, size)
    return ok({"key": key, "size": size})


@app.post("/_upload/api/abort")
def abort():
    user, groups = caller()
    body = request.get_json(silent=True) or {}
    key, upload_id = body.get("key"), body.get("id")
    if not owned_upload(user, groups, key, upload_id):
        return refuse(404, "no such upload")
    s3.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id)
    return ok({"aborted": key})


@app.errorhandler(ClientError)
def upstream(err):
    log.error("garage: %s", err)
    return refuse(502, "storage unavailable, try again")


@app.errorhandler(413)
def too_big(_):
    return refuse(413, "part too large")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from waitress import create_server
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        sys.exit("no listening socket from systemd")
    sockets = [socket.socket(fileno=3 + i) for i in range(int(os.environ.get("LISTEN_FDS", "0")))]
    server = create_server(app, sockets=sockets, threads=6, ident=None,
                           max_request_body_size=PART_BYTES + 1024)
    server.run()


if __name__ == "__main__":
    sys.exit(main())
