#!/usr/bin/env python3
"""Who may write into a home, and what the upload and save pages reach.

The ACL is real sqlite; the bucket is a stand-in. The properties:

  a write follows the rows the ACL holds: the owner's row, or a row naming another user
  a root grant and a group, files-admin included, never write into a home
  the upload and save pages address the caller's own home and nobody else's,
  whatever the ACL holds, so a row on another home adds no route
"""

import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ.update({
    "UPLOAD_ACL_DB": os.path.join(TMP, "acl.db"), "UPLOAD_ORIGIN": "https://files.kelliher.info",
    "UPLOAD_PAGE_DIR": TMP, "SAVE_PAGE_DIR": TMP, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_EC2_METADATA_DISABLED": "true",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload  # noqa: E402
from test_thumbs import Bucket  # noqa: E402

PHOTOS = {"photos/a.jpg": b"a", "photos/b.jpg": b"b"}
BUCKETS = {"pilar": Bucket(dict(PHOTOS)), "admin": Bucket({})}
KEY = "users/pilar/uploads/a.jpg"


class Base(unittest.TestCase):
    def setUp(self):
        for g in upload.ACL.list_grants():
            upload.ACL.revoke(g["subject"], g["path"], g["perm"])
        for perm in ("read", "list", "write"):
            upload.ACL.grant("user:pilar", "users/pilar/", perm, "fixture")
        self.saved = upload.PLACES.at
        upload.PLACES.at = lambda bucket, key, ensure=False: (
            BUCKETS[key.split("/")[1]], "u-" + key.split("/")[1], key.split("/", 2)[2])
        upload.app.config["TESTING"] = True
        self.http = upload.app.test_client()

    def tearDown(self):
        upload.PLACES.at = self.saved

    def grant_admin(self, *perms):
        for perm in perms:
            upload.ACL.grant("user:admin", "users/pilar/", perm, "fixture")

    def root_group(self):
        for perm in ("read", "list", "write"):
            upload.ACL.grant("group:files-admin", "", perm, "fixture")


class Writes(Base):
    def test_the_owner_writes_in_the_home(self):
        self.assertTrue(upload.may_write("pilar", [], KEY))

    def test_a_named_user_writes_where_the_row_says(self):
        self.assertFalse(upload.may_write("admin", [], KEY))
        self.grant_admin("write")
        self.assertTrue(upload.may_write("admin", [], KEY))
        self.assertTrue(upload.may_write("admin", ["files-admin"], KEY))
        self.assertFalse(upload.may_write("admin", [], "users/dad/uploads/a.jpg"))
        self.assertFalse(upload.may_write("admin", [], "users/pilar/../dad/uploads/a.jpg"))
        self.assertFalse(upload.may_write("admin", [], "users/pilar2/uploads/a.jpg"))

    def test_a_read_and_list_row_is_not_a_write(self):
        self.grant_admin("read", "list")
        self.assertFalse(upload.may_write("admin", [], KEY))

    def test_the_root_group_grant_and_a_group_member_do_not_write_a_home(self):
        self.root_group()
        self.assertFalse(upload.may_write("admin", ["files-admin"], KEY))
        self.assertFalse(upload.may_write("kid", ["files-admin", "pilar", "admin"], KEY))
        self.assertTrue(upload.may_write("admin", ["files-admin"], "notes/a.jpg"))


class Pages(Base):
    def post(self, path, user, body):
        return self.http.post(path, json=body, headers={"Remote-User": user, "Origin": upload.ORIGIN})

    def test_a_started_upload_is_never_continued_by_someone_else(self):
        self.grant_admin("read", "list", "write")
        self.assertFalse(upload.owned_upload("admin", [], KEY, "u1"))
        self.assertEqual(self.post("/_upload/api/complete", "admin", {"key": KEY, "id": "u1", "size": 1}).status_code, 404)
        self.assertEqual(self.post("/_upload/api/abort", "admin", {"key": KEY, "id": "u1"}).status_code, 404)

    def test_a_named_user_starting_an_upload_is_asked_about_their_own_home(self):
        self.grant_admin("read", "list", "write")
        r = self.post("/_upload/api/start", "admin", {"name": "a.jpg", "size": 10})
        self.assertEqual(r.status_code, 403)
        me = self.http.get("/_upload/api/me", headers={"Remote-User": "admin"}).get_json()
        self.assertEqual((me["folder"], me["writable"]), ("users/admin/", False))
        mine = self.http.get("/_upload/api/me", headers={"Remote-User": "pilar"}).get_json()
        self.assertEqual((mine["folder"], mine["writable"]), ("users/pilar/", True))

    def test_the_save_pages_list_the_callers_own_home_only(self):
        self.grant_admin("read", "list", "write")
        listed = self.http.get("/_save/api/list?root=photos/", headers={"Remote-User": "pilar"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["base"], "/users/pilar/photos/")
        for path in ("/_save/api/list?root=photos/", "/_save/api/zip?root=photos/&month=2026-11&batch=1"):
            self.assertEqual(self.http.get(path, headers={"Remote-User": "admin"}).status_code, 404, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
