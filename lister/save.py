import io
import json
import os
import re
import time
import zipfile
from collections import deque
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from flask import Response, g, request

from acl import BadPath, normalise
from place import Unplaced

BATCH_FILES = 50
BATCH_BYTES = 500 * 1024 * 1024
MAX_LISTED = 20000
CHUNK = 1024 * 1024

DATE = re.compile(r"(?<!\d)((?:19|20)\d\d)(0[1-9]|1[0-2])([0-3]\d)(?!\d)")
DASHED = re.compile(r"(?<!\d)((?:19|20)\d\d)-(0[1-9]|1[0-2])-([0-3]\d)(?!\d)")

TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".heif": "image/heif",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp", ".pdf": "application/pdf",
}
SAVABLE = tuple(TYPES)
VIDEOS = (".mp4", ".mov", ".m4v", ".3gp")
UNDATED = "undated"
DEFAULT_ROOT = "photos/keep/"
SETTINGS = ".save.json"
DATE_COLUMNS = ("date", "taken", "taken_at", "date_taken", "datetime", "exif_date", "created")
MANIFEST = "manifest.tsv"
_manifests = {}


def is_video(rel):
    return rel.lower().endswith(VIDEOS)


def month_from_value(value):
    value = (value or "").strip()
    m = re.match(r"((?:19|20)\d\d)[-/]?(0[1-9]|1[0-2])", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    if re.fullmatch(r"\d{9,10}(\.\d+)?", value):
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m")
    return None


def manifest_months(text):
    rows = text.splitlines()
    if not rows:
        return {}
    header = [h.strip().lower() for h in rows[0].split("\t")]
    col = next((header.index(c) for c in DATE_COLUMNS if c in header), None)
    path = header.index("path") if "path" in header else None
    if col is None or path is None:
        return {}
    out = {}
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) <= max(col, path):
            continue
        month = month_from_value(cells[col])
        if not month:
            continue
        parts = cells[path].strip("/").split("/")
        for i in range(len(parts)):
            out.setdefault("/".join(parts[i:]), month)
    return out


def month_of(rel, modified=None, manifest=None):
    if manifest and rel in manifest:
        return manifest[rel]
    base = rel.rsplit("/", 1)[-1]
    for rx in (DATE, DASHED):
        m = rx.search(base)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    if modified is not None:
        return modified.strftime("%Y-%m")
    return UNDATED


def day_of(rel, modified=None):
    base = rel.rsplit("/", 1)[-1]
    for rx in (DATE, DASHED):
        m = rx.search(base)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if modified is not None:
        return modified.strftime("%Y-%m-%d")
    return UNDATED


IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")


def kind_of(name):
    low = name.lower()
    if low.endswith(IMAGES):
        return "image"
    if low.endswith(VIDEOS):
        return "video"
    if low.endswith(".pdf"):
        return "pdf"
    return "other"


def batches_of(files):
    photos = [f for f in files if not is_video(f["rel"])]
    videos = [f for f in files if is_video(f["rel"])]
    return cut(photos) + cut(videos)


def cut(files):
    out, cur, size = [], [], 0
    for f in files:
        if cur and (len(cur) >= BATCH_FILES or size + f["size"] > BATCH_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += f["size"]
    if cur:
        out.append(cur)
    return out


class Sink(io.RawIOBase):
    def __init__(self):
        self.parts = deque()
        self.pos = 0

    def writable(self):
        return True

    def write(self, data):
        if data:
            self.parts.append(bytes(data))
            self.pos += len(data)
        return len(data)

    def tell(self):
        return self.pos

    def drain(self):
        while self.parts:
            yield self.parts.popleft()


def stream_zip(entries, open_body):
    sink = Sink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    for rel, size, stamp in entries:
        info = zipfile.ZipInfo(rel, date_time=stamp)
        info.compress_type = zipfile.ZIP_STORED
        info.file_size = size
        info.external_attr = 0o644 << 16
        body = open_body(rel)
        try:
            with zf.open(info, "w", force_zip64=size >= 0xFFFFFFFF) as out:
                for chunk in body.iter_chunks(CHUNK):
                    out.write(chunk)
                    yield from sink.drain()
        finally:
            body.close()
        yield from sink.drain()
    zf.close()
    yield from sink.drain()


def zip_stamp(when):
    when = when or datetime.now(timezone.utc)
    year = max(when.year, 1980)
    return (year, when.month, when.day, when.hour, when.minute, when.second - when.second % 2)


def register(app, *, place, caller, acl, page_dir, refuse, ok, log):
    def static(name, mime):
        with open(os.path.join(page_dir, name), "rb") as f:
            return Response(f.read(), mimetype=mime.split(";")[0],
                            headers={"Content-Type": mime, "Cache-Control": "no-cache"})

    def clean_root(raw, user):
        raw = raw if isinstance(raw, str) else ""
        try:
            root = normalise(raw)
        except BadPath:
            return None
        if root and not root.endswith("/"):
            root += "/"
        return root

    def read_object(user, rel, limit):
        try:
            client, bucket, inner = place(f"users/{user}/{rel}")
            r = client.get_object(Bucket=bucket, Key=inner)
            etag = r.get("ETag")
            body = r["Body"]
            parts, got = [], 0
            try:
                for chunk in body.iter_chunks(min(CHUNK, limit + 1)):
                    parts.append(chunk)
                    got += len(chunk)
                    if got > limit:
                        return None, None
            finally:
                body.close()
            data = b"".join(parts)
        except (ClientError, Unplaced, KeyError):
            return None, None
        return (data or None), etag

    def settings_root(user):
        data, _ = read_object(user, SETTINGS, 4096)
        if not data:
            return None
        try:
            root = json.loads(data.decode()).get("root")
        except (ValueError, AttributeError):
            return None
        return clean_root(root, user) if isinstance(root, str) else None

    def has_objects(user, root):
        try:
            client, bucket, inner = place(f"users/{user}/{root}")
            return bool(client.list_objects_v2(Bucket=bucket, Prefix=inner, MaxKeys=1).get("Contents"))
        except (ClientError, Unplaced):
            return False

    def resolve_root(user, requested):
        if requested is not None:
            return clean_root(requested, user)
        configured = settings_root(user)
        if configured is not None:
            return configured
        return DEFAULT_ROOT if has_objects(user, DEFAULT_ROOT) else ""

    def months_for(user, root):
        parent = root.rstrip("/").rpartition("/")[0]
        key = (parent + "/" if parent else "") + MANIFEST
        data, etag = read_object(user, key, 8 * 1024 * 1024)
        if not data:
            return {}
        held = _manifests.get((user, key))
        if held and held[0] == etag and etag:
            return held[1]
        parsed = manifest_months(data.decode("utf-8", "replace"))
        _manifests[(user, key)] = (etag, parsed)
        return parsed

    def listed(user, groups, root):
        home = f"users/{user}/"
        top = home + root
        if not acl.allows(user, groups, top, "list") or not acl.allows(user, groups, top, "read"):
            return None
        try:
            client, bucket, inner = place(top)
        except Unplaced:
            return None
        files, token = [], None
        while True:
            kw = {"Bucket": bucket, "Prefix": inner, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = client.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(inner):]
                if not rel or rel.endswith("/") or not rel.lower().endswith(SAVABLE):
                    continue
                if any(p.startswith(".") for p in rel.split("/")):
                    continue
                if not obj.get("Size", 0):
                    g.setdefault("empties", []).append({"rel": rel, "modified": obj.get("LastModified")})
                    continue
                files.append({"rel": rel, "size": obj.get("Size", 0), "modified": obj.get("LastModified")})
            if len(files) >= MAX_LISTED or not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break
        picks = set(request.args.getlist("pick"))
        if picks:
            files = [f for f in files if f["rel"] in picks]
        files.sort(key=lambda f: f["rel"].lower())
        return files

    def by_month(files, manifest=None):
        months = {}
        for f in files:
            months.setdefault(month_of(f["rel"], f["modified"], manifest), []).append(f)
        return months

    @app.get("/_save/")
    def save_page():
        return static("index.html", "text/html; charset=utf-8")

    @app.get("/_save/save.js")
    def save_script():
        return static("save.js", "text/javascript; charset=utf-8")

    @app.get("/_save/save.css")
    def save_style():
        return static("save.css", "text/css; charset=utf-8")

    @app.get("/_save/api/list")
    def save_list():
        user, groups = caller()
        root = resolve_root(user, request.args.get("root"))
        if root is None:
            return refuse(400, "bad folder")
        files = listed(user, groups, root)
        if files is None:
            return refuse(404, "nothing to save here")
        out = []
        manifest = months_for(user, root)
        grouped = by_month(files, manifest)
        order = sorted((m for m in grouped if m != UNDATED), reverse=True) + ([UNDATED] if UNDATED in grouped else [])
        for month in order:
            group = grouped[month]
            bs = batches_of(group)
            out.append({
                "empty": sum(1 for e in g.get("empties", []) if month_of(e["rel"], e["modified"], manifest) == month),
                "month": month, "files": len(group), "bytes": sum(f["size"] for f in group),
                "batches": [{"n": i + 1, "kind": "videos" if is_video(b[0]["rel"]) else "photos", "files": [{"rel": f["rel"], "size": f["size"]} for f in b],
                             "bytes": sum(f["size"] for f in b)} for i, b in enumerate(bs)],
            })
        return ok({"user": user, "root": root, "base": f"/users/{user}/{root}",
                   "empty": len(g.get("empties", [])), "picked": len(request.args.getlist("pick")), "batchFiles": BATCH_FILES, "batchBytes": BATCH_BYTES, "months": out,
                   "truncated": len(files) >= MAX_LISTED})

    @app.get("/_save/api/zip")
    def save_zip():
        user, groups = caller()
        root = resolve_root(user, request.args.get("root"))
        month = request.args.get("month", "")
        try:
            n = int(request.args.get("batch", ""))
        except ValueError:
            return refuse(400, "bad batch")
        if root is None or not (re.fullmatch(r"\d{4}-\d\d", month) or month == UNDATED) or n < 1:
            return refuse(400, "bad request")
        files = listed(user, groups, root)
        if files is None:
            return refuse(404, "nothing to save here")
        bs = batches_of(by_month(files, months_for(user, root)).get(month, []))
        if n > len(bs):
            return refuse(404, "no such batch")
        batch = bs[n - 1]
        client, bucket, inner = place(f"users/{user}/{root}")
        stamps = {f["rel"]: zip_stamp(f["modified"]) for f in batch}
        entries = [(f["rel"], f["size"], stamps[f["rel"]]) for f in batch]

        def open_body(rel):
            return client.get_object(Bucket=bucket, Key=inner + rel)["Body"]

        name = f"photos-{month}-part-{n}.zip"
        log.info("zip %s part %d for %s, %d files", month, n, user, len(batch))
        total = sum(size for _, size, _ in entries)
        return Response(stream_zip(entries, open_body), mimetype="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Zip-Entries": str(len(entries)),
            "X-Zip-Payload-Bytes": str(total),
        })
