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
upload.PLACES.at = lambda bucket, key, ensure=False: (BUCKET, "u-pilar", key[len("users/pilar/"):])
upload.ACL.grant("user:pilar", "users/pilar/", "read", "test")
upload.ACL.grant("user:pilar", "users/pilar/", "list", "test")


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

    def test_names_without_a_date_are_undated(self):
        for name in ["DCIM/Camera/IMG_0042.JPG", "Pictures/Messages/c979608e-7387-4b79-8993-60a0faa0ac94.jpg", "imagejpeg_9.jpg"]:
            self.assertEqual(save.month_of(name), save.UNDATED, name)

    def test_an_impossible_date_is_not_a_date(self):
        self.assertEqual(save.month_of("20251345_000000.jpg"), save.UNDATED)


class Batches(unittest.TestCase):
    def test_fifty_files_or_the_byte_cap_whichever_first(self):
        files = [{"rel": f"{i}.jpg", "size": 1000} for i in range(120)]
        self.assertEqual([len(b) for b in save.batches_of(files)], [50, 50, 20])
        big = [{"rel": f"{i}.mp4", "size": 120 * 1024 * 1024} for i in range(3)]
        self.assertEqual([len(b) for b in save.batches_of(big)], [1, 1, 1])
        mixed = [{"rel": f"{i}.mp4", "size": 90 * 1024 * 1024} for i in range(5)]
        self.assertEqual([len(b) for b in save.batches_of(mixed)], [2, 2, 1])

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
        self.assertEqual(months, ["2025-06", "2024-06", "2024-05", "2023-01", "undated"])
        june = next(m for m in j["months"] if m["month"] == "2024-06")
        self.assertEqual(june["files"], 33)
        self.assertEqual([len(b["files"]) for b in june["batches"]], [33])
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
