import hashlib
import io
import random
from datetime import datetime, timezone

from botocore.exceptions import ClientError


class Body:
    def __init__(self, data):
        self.io = io.BytesIO(data)

    def iter_chunks(self, n):
        while True:
            c = self.io.read(n)
            if not c:
                return
            yield c

    def close(self):
        self.io.close()


class FakeBucket:
    def __init__(self, objects, page=40):
        self.objects = objects
        self.page = page
        self.gets = 0

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None, Delimiter=None, **_):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        entries, seen = [], set()
        for k in keys:
            rest = k[len(Prefix):]
            if Delimiter and Delimiter in rest:
                cp = Prefix + rest.split(Delimiter)[0] + Delimiter
                if cp not in seen:
                    seen.add(cp)
                    entries.append(("p", cp))
            else:
                entries.append(("k", k))
        start = int(ContinuationToken or 0)
        window = entries[start:start + self.page]
        more = start + self.page < len(entries)
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k]), "ETag": '"' + hashlib.md5(self.objects[k]).hexdigest()[:12] + '"',
                          "LastModified": datetime(2026, 11, 26, 1, 30, tzinfo=timezone.utc)} for t, k in window if t == "k"],
            "CommonPrefixes": [{"Prefix": k} for t, k in window if t == "p"],
            "IsTruncated": more,
            "NextContinuationToken": str(start + self.page) if more else None,
        }

    def head_object(self, Bucket, Key, **_):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        ext = Key.rsplit(".", 1)[-1].lower()
        kind = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "heic": "image/heic", "mp4": "video/mp4"}.get(ext, "application/octet-stream")
        return {"ContentType": kind, "ContentLength": len(self.objects[Key]),
                "LastModified": datetime(2026, 11, 26, 1, 30, tzinfo=timezone.utc)}

    def get_object(self, Bucket, Key, **_):
        self.gets += 1
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[Key]
        return {"Body": Body(data), "ETag": hashlib.md5(data).hexdigest()}


def invented(seed=5):
    r = random.Random(seed)
    objs = {}

    def add(key, size):
        objs[key] = hashlib.shake_256(f"{seed}:{key}".encode()).digest(size)

    for day in range(1, 29):
        for i in range(2):
            add(f"DCIM/Camera/202405{day:02d}_{10 + i}0000.jpg", r.randint(20_000, 60_000))
    for day in range(1, 31):
        add(f"DCIM/Camera/202406{day:02d}_090000.heic", r.randint(20_000, 60_000))
    for day in (3, 9, 15):
        add(f"DCIM/Camera/202406{day:02d}_180000.mp4", 3_000_000)
    for day in range(1, 12):
        add(f"DCIM/Camera/202301{day:02d}_120000.jpg", r.randint(20_000, 40_000))
    add("DCIM/Screenshots/Screenshot_20240512-101010_Maps.jpg", 15_000)
    add("DCIM/Camera/20240512_101010.jpg", 15_000)
    add("DCIM/GalleryTab/Album/The Denny/20250626_111217.jpg", 25_000)
    add("DCIM/Camera/IMG_0042.JPG", 18_000)
    add("Pictures/Messages/c979608e-7387-4b79-8993-60a0faa0ac94.jpg", 12_000)
    add("DCIM/Camera/.thumb.jpg", 100)
    add("DCIM/Camera/notes.txt", 100)
    return {"photos/" + k: v for k, v in objs.items()}
