#!/usr/bin/env python3
"""The photo-save endpoints: which month a file belongs to, how a month is cut
into batches, that the zip streams the exact bytes, and that only the owner of
a folder can list or zip it."""

import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp()
os.environ["UPLOAD_ORIGIN"] = "https://files.kelliher.info"
os.environ["UPLOAD_PAGE_DIR"] = os.path.join(_HERE, "..", "upload-page")
os.environ["SAVE_PAGE_DIR"] = os.path.join(_HERE, "..", "save-page")
os.environ["UPLOAD_ACL_DB"] = os.path.join(_TMP, "acl.db")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

sys.path.insert(0, _HERE)
import save  # noqa: E402
import upload  # noqa: E402
from fakebucket import FakeBucket, invented  # noqa: E402

OBJECTS = invented()
BUCKET = FakeBucket(OBJECTS)


def tree(prefix, names, size=1000, extra=None):
    out = {prefix + n: hashlib.shake_256(n.encode()).digest(size) for n in names}
    out.update(extra or {})
    return out


KEEP = ["DCIM/Camera/20240301_100000.jpg", "DCIM/Camera/20240302_100000.jpg", "DCIM/Camera/IMG_0001.JPG", "DCIM/Camera/20240303_100000.mp4"]
ALL = KEEP + ["DCIM/Camera/20240301_100001.jpg", "DCIM/Camera/20240301_100002.jpg"]
MANIFEST = ("path\tsize\tw\th\tsha256\tgroup\tkind\tkeeper\n" + "".join(f"all/{n}\t1000\t1\t1\tx\t-\tsingle\t1\n" for n in ALL)).encode()
DATED = ("path\tsize\ttaken\tkind\n" + "all/DCIM/Camera/IMG_0001.JPG\t1000\t2019-07-04T10:00:00\tsingle\n"
         + "keep/DCIM/Camera/20240301_100000.jpg\t1000\t2018-12-31\tsingle\n").encode()
USERS = {
    "pilar": BUCKET,
    "bea": FakeBucket({**tree("photos/all/", ALL), **tree("photos/keep/", KEEP), "photos/manifest.tsv": MANIFEST, "uploads/20250101_000000.jpg": b"u"}),
    "cal": FakeBucket(tree("", ["uploads/20250101_000000.jpg", "docs/20250202_000000.pdf"])),
    "dee": FakeBucket({**tree("photos/all/", ALL), **tree("photos/keep/", KEEP),
                       ".save.json": b'{"root": "photos/all/"}'}),
    "fay": FakeBucket({**tree("photos/keep/", KEEP)}),
    "eli": FakeBucket({**tree("photos/all/", ALL), **tree("photos/keep/", KEEP), "photos/manifest.tsv": DATED}),
}
upload.PLACES.at = lambda bucket, key, ensure=False: (USERS[key.split("/")[1]], "u-" + key.split("/")[1], key.split("/", 2)[2])
for who in USERS:
    upload.ACL.grant(f"user:{who}", f"users/{who}/", "read", "test")
    upload.ACL.grant(f"user:{who}", f"users/{who}/", "list", "test")


class Endless:
    def __init__(self):
        self.read = 0

    def iter_chunks(self, n):
        while True:
            self.read += n
            yield b"x" * n

    def close(self):
        self.closed = True


class Reading(unittest.TestCase):
    def test_an_oversized_settings_or_manifest_object_is_abandoned_at_the_limit(self):
        for key, limit in ((".save.json", 4096), ("photos/manifest.tsv", 8 * 1024 * 1024)):
            body = Endless()
            bucket = USERS["fay"]
            real = bucket.get_object

            def get(Bucket, Key, _k=key, _b=body, _real=real, **kw):
                return {"Body": _b, "ETag": "e"} if Key == _k else _real(Bucket=Bucket, Key=Key, **kw)

            bucket.get_object = get
            try:
                c = upload.app.test_client()
                r = c.get("/_save/api/list", headers={"Remote-User": "fay"})
                self.assertEqual(r.status_code, 200, key)
                self.assertLessEqual(body.read, limit + save.CHUNK, key)
                self.assertTrue(body.closed, key)
            finally:
                bucket.get_object = real


class Months(unittest.TestCase):
    def test_the_phone_names_all_land_in_their_month(self):
        for name, want in [
            ("DCIM/Camera/20250516_174638.jpg", "2025-05"),
            ("DCIM/Camera/20250516_174638_001.jpg", "2025-05"),
            ("DCIM/Camera/20250516_174638(1).jpg", "2025-05"),
            ("DCIM/Videocaptures/VideoCapture_20230706-105825.jpg", "2023-07"),
            ("DCIM/Screenshots/Screenshot_20240512-101010_Maps.jpg", "2024-05"),
            ("DCIM/Camera/IMG_20240101_000000.heic", "2024-01"),
            ("DCIM/GalleryTab/Album/The Denny/20250626_111217.jpg", "2025-06"),
        ]:
            self.assertEqual(save.month_of(name), want, name)

    def test_a_name_without_a_date_falls_back_to_the_object_date_then_to_undated(self):
        from datetime import datetime, timezone
        when = datetime(2026, 11, 26, tzinfo=timezone.utc)
        for name in ["DCIM/Camera/IMG_0042.JPG", "Pictures/Messages/c979608e-7387-4b79-8993-60a0faa0ac94.jpg", "imagejpeg_9.jpg"]:
            self.assertEqual(save.month_of(name, when), "2026-11", name)
            self.assertEqual(save.month_of(name), save.UNDATED, name)

    def test_the_manifest_date_wins_over_the_name(self):
        self.assertEqual(save.month_of("DCIM/Camera/20250516_174638.jpg", None, {"DCIM/Camera/20250516_174638.jpg": "2019-02"}), "2019-02")

    def test_a_manifest_with_a_date_column_is_read_by_its_header(self):
        got = save.manifest_months("path\tsize\ttaken\nall/DCIM/a.jpg\t1\t2019-07-04T10:00:00\nall/DCIM/b.jpg\t1\t20180102\nall/c.jpg\t1\t1500000000\nall/d.jpg\t1\tnonsense\n")
        self.assertEqual(got["DCIM/a.jpg"], "2019-07")
        self.assertEqual(got["all/DCIM/a.jpg"], "2019-07")
        self.assertEqual(got["DCIM/b.jpg"], "2018-01")
        self.assertEqual(got["c.jpg"], "2017-07")
        self.assertNotIn("d.jpg", got)

    def test_a_manifest_without_a_date_column_gives_nothing(self):
        self.assertEqual(save.manifest_months("path\tsize\tw\th\tsha256\tgroup\tkind\tkeeper\nall/a.jpg\t1\t1\t1\tx\t-\tsingle\t1\n"), {})

    def test_an_impossible_date_is_not_a_date(self):
        self.assertEqual(save.month_of("20251345_000000.jpg"), save.UNDATED)


class Batches(unittest.TestCase):
    def test_fifty_files_or_the_byte_cap_whichever_first(self):
        files = [{"rel": f"{i}.jpg", "size": 1000} for i in range(120)]
        self.assertEqual([len(b) for b in save.batches_of(files)], [50, 50, 20])
        self.assertEqual(save.BATCH_BYTES, 500 * 1024 * 1024)
        big = [{"rel": f"{i}.jpg", "size": 300 * 1024 * 1024} for i in range(3)]
        self.assertEqual([len(b) for b in save.batches_of(big)], [1, 1, 1])
        mixed = [{"rel": f"{i}.jpg", "size": 120 * 1024 * 1024} for i in range(9)]
        self.assertEqual([len(b) for b in save.batches_of(mixed)], [4, 4, 1])

    def test_videos_never_share_a_batch_with_photos(self):
        files = [{"rel": f"{i}.jpg", "size": 1000} for i in range(3)] + [{"rel": f"{i}.MOV", "size": 1000} for i in range(2)] \
            + [{"rel": "x.mp4", "size": 1000}, {"rel": "y.heic", "size": 1000}]
        bs = save.batches_of(files)
        self.assertEqual([[f["rel"] for f in b] for b in bs], [["0.jpg", "1.jpg", "2.jpg", "y.heic"], ["0.MOV", "1.MOV", "x.mp4"]])
        for b in bs:
            self.assertEqual(len({save.is_video(f["rel"]) for f in b}), 1)

    def test_nothing_is_dropped_or_repeated(self):
        files = [{"rel": f"{i}.jpg", "size": 3_000_000 + i} for i in range(333)]
        flat = [f["rel"] for b in save.batches_of(files) for f in b]
        self.assertEqual(flat, [f["rel"] for f in files])


class Api(unittest.TestCase):
    def setUp(self):
        upload.app.config["TESTING"] = True
        self.c = upload.app.test_client()

    def get(self, path, user="pilar"):
        h = {"Remote-User": user} if user else {}
        return self.c.get(path, headers=h)

    def test_the_list_groups_by_month_newest_first_undated_last(self):
        j = self.get("/_save/api/list?root=photos/").get_json()
        months = [m["month"] for m in j["months"]]
        self.assertEqual(months, ["2026-11", "2025-06", "2024-06", "2024-05", "2023-01"])
        self.assertEqual(next(m for m in j["months"] if m["month"] == "2026-11")["files"], 2)
        june = next(m for m in j["months"] if m["month"] == "2024-06")
        self.assertEqual(june["files"], 33)
        self.assertEqual([len(b["files"]) for b in june["batches"]], [30, 3])
        self.assertEqual([b["kind"] for b in june["batches"]], ["photos", "videos"])
        may = next(m for m in j["months"] if m["month"] == "2024-05")
        self.assertEqual(may["files"], 58)
        self.assertEqual([len(b["files"]) for b in may["batches"]], [50, 8])
        self.assertEqual(j["base"], "/users/pilar/photos/")

    def test_hidden_files_and_non_media_are_not_offered(self):
        j = self.get("/_save/api/list?root=photos/").get_json()
        rels = [f["rel"] for m in j["months"] for b in m["batches"] for f in b["files"]]
        self.assertFalse([r for r in rels if r.endswith("notes.txt") or "/.thumb" in r])

    def test_another_person_gets_nothing(self):
        self.assertEqual(self.get("/_save/api/list?root=photos/", user="dad").status_code, 404)
        self.assertEqual(self.get("/_save/api/zip?root=photos/&month=2024-05&batch=1", user="dad").status_code, 404)

    def test_no_identity_is_refused(self):
        self.assertEqual(self.get("/_save/api/list", user=None).status_code, 401)
        self.assertEqual(self.get("/_save/", user=None).status_code, 401)

    def test_a_granted_person_with_no_bucket_yet_gets_404_like_no_access(self):
        from place import Unplaced
        upload.ACL.grant("user:newbie", "users/newbie/", "read", "test")
        upload.ACL.grant("user:newbie", "users/newbie/", "list", "test")
        real = upload.PLACES.at

        def at(bucket, key, ensure=False):
            if key.startswith("users/newbie/"):
                raise Unplaced("newbie has no bucket yet")
            return real(bucket, key, ensure)

        upload.PLACES.at = at
        try:
            self.assertEqual(self.get("/_save/api/list?root=photos/", user="newbie").status_code, 404)
            self.assertEqual(self.get("/_save/api/zip?root=photos/&month=2024-05&batch=1", user="newbie").status_code, 404)
        finally:
            upload.PLACES.at = real

    def listed_rels(self, user, query=""):
        j = self.get("/_save/api/list" + query, user=user).get_json()
        return j, sorted(f["rel"] for m in j["months"] for b in m["batches"] for f in b["files"])

    def test_the_default_root_is_the_keepers_when_they_exist(self):
        j, rels = self.listed_rels("bea")
        self.assertEqual(j["root"], "photos/keep/")
        self.assertEqual(rels, sorted(KEEP))

    def test_without_a_keepers_folder_the_whole_home_is_offered(self):
        j, rels = self.listed_rels("cal")
        self.assertEqual(j["root"], "")
        self.assertEqual(rels, ["docs/20250202_000000.pdf", "uploads/20250101_000000.jpg"])

    def test_a_per_user_setting_picks_the_root(self):
        j, rels = self.listed_rels("dee")
        self.assertEqual(j["root"], "photos/all/")
        self.assertEqual(rels, sorted(ALL))

    def test_an_explicit_root_wins_over_the_default(self):
        j, rels = self.listed_rels("bea", "?root=photos/all/")
        self.assertEqual(rels, sorted(ALL))

    def test_the_manifest_date_places_a_file_in_its_month(self):
        j, _ = self.listed_rels("eli")
        by = {m["month"]: [f["rel"] for b in m["batches"] for f in b["files"]] for m in j["months"]}
        self.assertEqual(by["2019-07"], ["DCIM/Camera/IMG_0001.JPG"])
        self.assertEqual(by["2018-12"], ["DCIM/Camera/20240301_100000.jpg"])
        self.assertEqual(by["2024-03"], ["DCIM/Camera/20240302_100000.jpg", "DCIM/Camera/20240303_100000.mp4"])

    def test_without_a_date_column_the_name_date_is_used(self):
        j, _ = self.listed_rels("bea")
        self.assertEqual(sorted(m["month"] for m in j["months"]), ["2024-03", "2026-11"])
        by = {m["month"]: sorted(f["rel"] for b in m["batches"] for f in b["files"]) for m in j["months"]}
        self.assertEqual(by["2024-03"], ["DCIM/Camera/20240301_100000.jpg", "DCIM/Camera/20240302_100000.jpg", "DCIM/Camera/20240303_100000.mp4"])

    def test_the_default_root_zips_only_keepers(self):
        r = self.get("/_save/api/zip?month=2024-03&batch=1", user="bea")
        self.assertEqual(r.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        self.assertEqual(sorted(names), ["DCIM/Camera/20240301_100000.jpg", "DCIM/Camera/20240302_100000.jpg"])
        v = self.get("/_save/api/zip?month=2024-03&batch=2", user="bea")
        self.assertEqual(zipfile.ZipFile(io.BytesIO(v.data)).namelist(), ["DCIM/Camera/20240303_100000.mp4"])

    def test_a_climbing_root_is_refused(self):
        self.assertEqual(self.get("/_save/api/list?root=../dad/").status_code, 400)

    def test_the_zip_is_the_exact_bytes_of_the_batch(self):
        j = self.get("/_save/api/list?root=photos/").get_json()
        may = next(m for m in j["months"] if m["month"] == "2024-05")
        for n, b in enumerate(may["batches"], 1):
            r = self.get(f"/_save/api/zip?root=photos/&month=2024-05&batch={n}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.mimetype, "application/zip")
            self.assertIn(f"photos-2024-05-part-{n}.zip", r.headers["Content-Disposition"])
            z = zipfile.ZipFile(io.BytesIO(r.data))
            self.assertIsNone(z.testzip())
            self.assertEqual(sorted(z.namelist()), sorted(f["rel"] for f in b["files"]))
            for f in b["files"]:
                self.assertEqual(hashlib.sha256(z.read(f["rel"])).digest(),
                                 hashlib.sha256(OBJECTS["photos/" + f["rel"]]).digest(), f["rel"])

    def test_a_missing_batch_is_404(self):
        self.assertEqual(self.get("/_save/api/zip?root=photos/&month=2024-05&batch=9").status_code, 404)
        self.assertEqual(self.get("/_save/api/zip?root=photos/&month=2024-05&batch=0").status_code, 400)
        self.assertEqual(self.get("/_save/api/zip?root=photos/&month=bad&batch=1").status_code, 400)


class Streaming(unittest.TestCase):
    def test_a_big_entry_streams_in_chunks_and_needs_zip64_over_4gib(self):
        class Body:
            def __init__(self, n):
                self.left = n

            def iter_chunks(self, size):
                while self.left > 0:
                    k = min(size, self.left)
                    self.left -= k
                    yield b"\0" * k

            def close(self):
                pass

        chunks = list(save.stream_zip([("a.mp4", 5 * 1024 * 1024, (2026, 1, 1, 0, 0, 0))], lambda rel: Body(5 * 1024 * 1024)))
        self.assertGreater(len(chunks), 4)
        z = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
        self.assertEqual(z.getinfo("a.mp4").file_size, 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
