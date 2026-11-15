import json
import re
from pathlib import Path
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError

from acl import BadPath, canonical_object_uri

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
TRACK_ID = re.compile(r"[0-9a-f]{32}\Z")
REVISION = re.compile(r"[0-9a-f]{12}\Z")
CAPTION_OBJECT = re.compile(r"captions_([0-9a-f]{32})(?:\.vtt|\.m3u8|_[0-9]{6}\.vtt)\Z")


class ManifestUnavailable(ValueError):
    pass


def canonical_key(value):
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ManifestUnavailable("invalid subtitle source key")
    try:
        key, _ = canonical_object_uri(quote("/" + value, safe="/"))
    except (BadPath, ValueError) as exc:
        raise ManifestUnavailable("invalid subtitle source key") from exc
    if key != value:
        raise ManifestUnavailable("noncanonical subtitle source key")
    return key


def etag(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(c) < 33 or c == '"' for c in value)):
        raise ManifestUnavailable("invalid subtitle source revision")
    return value


def load_tracks(path):
    if not path:
        return []
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ManifestUnavailable("subtitle manifest too large")
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("schemaVersion") != 1 or not isinstance(body.get("tracks"), list):
            raise ManifestUnavailable("invalid subtitle manifest")
        if len(body["tracks"]) > 10000:
            raise ManifestUnavailable("too many subtitle tracks")
        tracks, identities, counts = [], set(), {}
        for row in body["tracks"]:
            if not isinstance(row, dict):
                raise ManifestUnavailable("invalid subtitle track")
            key = canonical_key(row.get("videoKey"))
            source = canonical_key(row.get("sourceKey"))
            video_rev, track_id = row.get("videoRev"), row.get("id")
            if not isinstance(video_rev, str) or not REVISION.fullmatch(video_rev):
                raise ManifestUnavailable("invalid video revision")
            if not isinstance(track_id, str) or not TRACK_ID.fullmatch(track_id):
                raise ManifestUnavailable("invalid subtitle id")
            objects = row.get("objects")
            if not isinstance(objects, list) or not objects or len(objects) > 4096:
                raise ManifestUnavailable("invalid subtitle objects")
            for name in objects:
                match = CAPTION_OBJECT.fullmatch(name) if isinstance(name, str) else None
                if not match or match.group(1) != track_id:
                    raise ManifestUnavailable("unbound subtitle object")
            identity = (key, video_rev, track_id)
            if identity in identities:
                raise ManifestUnavailable("duplicate subtitle id")
            identities.add(identity)
            counts[(key, video_rev)] = counts.get((key, video_rev), 0) + 1
            if counts[(key, video_rev)] > 8:
                raise ManifestUnavailable("too many subtitles for one video")
            tracks.append(dict(row, videoKey=key, sourceKey=source,
                               videoEtag=etag(row.get("videoEtag")), sourceEtag=etag(row.get("sourceEtag"))))
        return tracks
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ManifestUnavailable("subtitle manifest unavailable") from exc


class CaptionGate:
    def __init__(self, path, storage, acl, bucket, user, groups, scope):
        self.path = path
        self.storage = storage
        self.acl = acl
        self.bucket = bucket
        self.user = user
        self.groups = groups
        self.scope = scope
        self._heads = {}
        self._tracks = None

    def tracks(self):
        if self._tracks is None:
            self._tracks = load_tracks(self.path)
        return self._tracks

    def in_scope(self, key):
        return self.scope is None or any(key.startswith(prefix) for prefix in self.scope)

    def current(self, row):
        for key, expected in ((row["videoKey"], row["videoEtag"]), (row["sourceKey"], row["sourceEtag"])):
            if not self.in_scope(key) or not self.acl.allows(self.user, self.groups, key, "read"):
                return False
            if key not in self._heads:
                try:
                    head = self.storage.head_object(Bucket=self.bucket, Key=key)
                    self._heads[key] = str(head.get("ETag", "")).strip('"')
                except (BotoCoreError, ClientError, OSError):
                    self._heads[key] = None
            if self._heads[key] != expected:
                return False
        return True

    def allowed_ids(self, key, revision=None):
        return [row["id"] for row in self.tracks()
                if row["videoKey"] == key and (revision is None or row["videoRev"] == revision) and self.current(row)]

    def associated_master(self, key, revision):
        return any(row["videoKey"] == key and row["videoRev"] == revision for row in self.tracks())

    def allows_object(self, key, revision, name):
        if not CAPTION_OBJECT.fullmatch(name):
            return False
        return any(row["videoKey"] == key and row["videoRev"] == revision and name in row["objects"] and self.current(row)
                   for row in self.tracks())
