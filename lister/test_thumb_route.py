#!/usr/bin/env python3
"""GET /_thumb/users/<owner>/<rel>: who may read a thumb, and what each refusal reveals.

The ACL is real sqlite; the bucket is a stand-in. The properties:

  the owner gets the bytes, cached privately and for good
  no thumb yet is 404 with X-Thumb: pending, said only to someone who may read
  everyone else gets one plain 404, byte for byte the same whether the
  original exists, the user exists, or the path is nonsense, and the bucket is
  never touched on the way to that answer
"""

import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ.update({
    "LISTER_TEMPLATE": "/dev/null", "LISTER_BUCKETS": "files", "LISTER_ROOT_DOMAIN": ".kelliher.info",
    "LISTER_ACL_DB": os.path.join(TMP, "acl.db"), "AWS_ACCESS_KEY_ID": "fixture", "AWS_SECRET_ACCESS_KEY": "fixture",
    "AWS_EC2_METADATA_DISABLED": "true",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lister as L  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from test_thumbs import Bucket, jpeg_bytes  # noqa: E402

HOST = "http://files.kelliher.info"
THUMB = jpeg_bytes((320, 240), quality=70)


class Places:
    def __init__(self, bucket, per_user=False):
        self.bucket = bucket
        self.per_user = per_user
        self.calls = 0

    def at(self, bucket, key, ensure=False):
        self.calls += 1
        if self.per_user:
            owner = key.split("/")[1]
            return self.bucket, "u-" + owner, key[len("users/" + owner + "/"):]
        return self.bucket, bucket, key

    def outer(self, bucket, key):
        return ""


class Base(unittest.TestCase):
    def setUp(self):
        L.app.config["TESTING"] = True
        self.http = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g["subject"], g["path"], g["perm"])
        L.ACL.grant("user:pilar", "users/pilar/", "read", "fixture")
        L.ACL.grant("user:pilar", "users/pilar/", "list", "fixture")
        L.ACL.grant("group:files-admin", "", "read", "fixture")
        L.ACL.grant("user:other", "users/other/", "read", "fixture")
        self.bucket = Bucket({
            ".thumbs/users/pilar/camera/20240512_101010.jpg.jpg": THUMB,
            ".thumbs/users/pilar/camera/caf\u00e9 (1).heic.jpg": THUMB,
            ".thumbs/users/pilar/camera/zero.jpg.jpg": b"",
            ".thumbs/users/pilar/camera/notjpeg.jpg.jpg": b"<script>alert(1)</script>",
            ".thumbs/users/pilar/camera/huge.jpg.jpg": b"\xff\xd8\xff" + b"0" * (L.THUMB_MAX_BYTES + 10),
            ".thumbs/users/other/camera/theirs.jpg.jpg": THUMB,
            ".thumbs/users/pilar/.thumbs/x.jpg.jpg": THUMB,
        })
        self.places = Places(self.bucket)
        self.saved = (L.PLACES, L.CLIENT_PREFIXES)
        L.PLACES = self.places

    def tearDown(self):
        L.PLACES, L.CLIENT_PREFIXES = self.saved

    def get(self, path, user="pilar", groups="", host=HOST, method="GET", **headers):
        h = dict(headers)
        if user is not None:
            h["Remote-User"] = user
        if groups is not None:
            h["Remote-Groups"] = groups
        return self.http.open(path, method=method, headers=h, base_url=host)

    def shape(self, r):
        return (r.status_code, sorted((k, v) for k, v in r.headers if k not in ("Date",)), r.data)


class Served(Base):
    def test_the_owner_gets_the_bytes_cached_privately_for_good(self):
        r = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg?v=abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, THUMB)
        self.assertEqual(r.headers["Content-Type"], "image/jpeg")
        self.assertEqual(r.headers["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("X-Thumb", r.headers)
        self.assertTrue(r.headers["ETag"])

    def test_names_with_spaces_accents_and_brackets_are_found(self):
        r = self.get("/_thumb/users/pilar/camera/caf%C3%A9%20%281%29.heic")
        self.assertEqual((r.status_code, r.data), (200, THUMB))

    def test_head_answers_without_a_body(self):
        r = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", method="HEAD")
        self.assertEqual((r.status_code, r.data), (200, b""))

    def test_a_revalidation_costs_a_304(self):
        first = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg")
        again = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", **{"If-None-Match": first.headers["ETag"]})
        self.assertEqual((again.status_code, again.data), (304, b""))

    def test_the_per_user_bucket_layout_reads_the_same_rule(self):
        self.bucket.objects[".thumbs/camera/20240512_101010.jpg.jpg"] = THUMB
        L.PLACES = Places(self.bucket, per_user=True)
        r = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg")
        self.assertEqual((r.status_code, r.data), (200, THUMB))

    def test_a_bearer_client_inside_its_prefix_may_read(self):
        L.CLIENT_PREFIXES = {"bot": ["users/pilar/camera/"]}
        self.assertEqual(self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", **{"Remote-Client": "bot"}).status_code, 200)
        self.assertEqual(self.get("/_thumb/users/pilar/other/x.jpg", **{"Remote-Client": "bot"}).headers.get("X-Thumb"), None)
        self.assertEqual(self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", **{"Remote-Client": "stranger"}).status_code, 404)


class Pending(Base):
    def test_no_thumb_yet_says_pending_to_someone_who_may_read(self):
        r = self.get("/_thumb/users/pilar/camera/20240513_101010.jpg")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.headers["X-Thumb"], "pending")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_a_marker_a_non_jpeg_and_an_oversize_object_are_pending_not_served(self):
        for name in ("zero", "notjpeg", "huge"):
            r = self.get(f"/_thumb/users/pilar/camera/{name}.jpg")
            self.assertEqual((r.status_code, r.headers.get("X-Thumb")), (404, "pending"), name)
            self.assertNotIn(b"script", r.data)

    def test_a_storage_fault_is_not_pending(self):
        def broken(**_):
            raise ClientError({"Error": {"Code": "InternalError"}}, "GetObject")

        self.bucket.get_object = broken
        r = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("X-Thumb", r.headers)


class Refused(Base):
    def test_every_refusal_is_the_same_plain_404(self):
        L.CLIENT_PREFIXES = {}
        cases = {
            "other user with a thumb present": self.get("/_thumb/users/other/camera/theirs.jpg"),
            "other user, no such file": self.get("/_thumb/users/other/camera/nothing.jpg"),
            "no such user": self.get("/_thumb/users/nobody/camera/x.jpg"),
            "admin with the root grant": self.get("/_thumb/users/other/camera/theirs.jpg", user="admin", groups="files-admin"),
            "admin on a user that has nothing": self.get("/_thumb/users/nobody/camera/x.jpg", user="admin", groups="files-admin"),
            "caller with no grants": self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", user="stranger"),
            "a forged group": self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", user="stranger", groups="files-admin,pilar"),
            "outside users/": self.get("/_thumb/dad/x.jpg"),
            "the home itself": self.get("/_thumb/users/pilar/"),
            "no rel": self.get("/_thumb/users/pilar"),
            "dot segment": self.get("/_thumb/users/pilar/camera/../../other/camera/theirs.jpg"),
            "hidden segment": self.get("/_thumb/users/pilar/.thumbs/x.jpg"),
            "bad bucket host": self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", host="http://graveyard.kelliher.info"),
            "backslash": self.get("/_thumb/users/pilar/camera%5C..%5Cx.jpg"),
            "control byte": self.get("/_thumb/users/pilar/camera/a%0Ab.jpg"),
            "trailing slash": self.get("/_thumb/users/pilar/camera/20240512_101010.jpg/"),
            "overlong": self.get("/_thumb/users/pilar/" + "a" * 2000 + ".jpg"),
        }
        want = self.shape(cases["no such user"])
        self.assertEqual(want[0], 404)
        self.assertNotIn("X-Thumb", dict(want[1]))
        for name, r in cases.items():
            if name in ("dot segment", "trailing slash", "control byte"):
                self.assertIn(r.status_code, (404, 308), name)
                self.assertNotIn("X-Thumb", r.headers, name)
                continue
            self.assertEqual(self.shape(r), want, name)

    def test_a_refusal_never_touches_the_bucket(self):
        self.bucket.get_object = self.bucket.head_object = self.bucket.list_objects_v2 = lambda **_: self.fail("bucket touched")
        for path, user in (("/_thumb/users/other/camera/theirs.jpg", "pilar"), ("/_thumb/users/pilar/camera/a.jpg", "stranger"),
                           ("/_thumb/users/nobody/x.jpg", "admin"), ("/_thumb/dad/x.jpg", "pilar")):
            self.assertEqual(self.get(path, user=user, groups="files-admin").status_code, 404, path)
        self.assertEqual(self.places.calls, 0)

    def test_no_identity_is_401_and_reaches_nothing(self):
        r = self.get("/_thumb/users/pilar/camera/20240512_101010.jpg", user=None)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.places.calls, 0)

    def test_the_refusal_is_logged_for_the_operator(self):
        with self.assertLogs("gluck-files", "INFO") as seen:
            self.get("/_thumb/users/other/camera/theirs.jpg")
        self.assertTrue(any(line.startswith("INFO:gluck-files:denied thumb files/users/other/camera/theirs.jpg to pilar") for line in seen.output), seen.output)

    def test_serving_a_thumb_is_not_logged_per_tile(self):
        with self.assertNoLogs("gluck-files", "INFO"):
            self.get("/_thumb/users/pilar/camera/20240512_101010.jpg")


class Pressure(Base):
    def setUp(self):
        super().setUp()
        from waitress import create_server

        self.server = create_server(L.app, host="127.0.0.1", port=0, threads=4)
        L.ADMISSION.watch(self.server)

    def tearDown(self):
        self.server.close()
        L.ADMISSION._dispatcher = None
        super().tearDown()

    def queue(self, n):
        d = self.server.task_dispatcher
        with d.lock:
            d.queue.clear()
            d.queue.extend(object() for _ in range(n))

    def test_a_page_of_tiles_is_not_shed_where_a_listing_is(self):
        self.queue(L.MAX_QUEUE + 1)
        self.assertEqual(self.get("/users/pilar/").status_code, 503)
        self.assertEqual(self.get("/_thumb/users/pilar/camera/20240512_101010.jpg").status_code, 200)

    def test_tiles_are_shed_when_the_queue_is_truly_deep(self):
        self.queue(L.MAX_QUEUE * L.THUMB_QUEUE_FACTOR + 1)
        self.assertEqual(self.get("/_thumb/users/pilar/camera/20240512_101010.jpg").status_code, 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
