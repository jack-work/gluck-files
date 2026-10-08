#!/usr/bin/env python3
"""Gateway tests.

The ACL is real sqlite; only Garage is stubbed. The properties:

  a request without Remote-User is refused, not treated as anonymous
  an object with no grant is 404, never 403, so the bucket is not an oracle
  an object with a grant is a 302 to a short-lived presigned URL
  the acceptance case: dad reaches his own directory and nothing else
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp()
with open(os.path.join(_TMP, "tpl.html"), "w") as fh:
    fh.write(
        "<h1>{{ heading }}</h1>{{ tally }}"
        "{% for d in dirs %}<a href='{{ d.href }}'>{{ d.name }}/</a>{% endfor %}"
        "{% for f in files %}<a href='{{ f.href }}'>{{ f.name }}</a>{% endfor %}"
    )

os.environ["LISTER_TEMPLATE"] = os.path.join(_TMP, "tpl.html")
os.environ["LISTER_BUCKETS"] = "files"
os.environ["LISTER_ROOT_DOMAIN"] = ".kelliher.info"
os.environ["LISTER_ACL_DB"] = os.path.join(_TMP, "acl.db")
os.environ["LISTER_PRESIGN_SECONDS"] = "120"
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

sys.path.insert(0, _HERE)
import lister as L  # noqa: E402

# A bucket shaped like the real one: dad's directory plus private documents.
OBJECTS = {
    "dad/Mitchell-MachineLearning-1997.pdf": 60363034,
    "dad/notes.txt": 12,
    "wfh/Rental_Agreement.pdf": 92838,
    "wfh/contacts.pdf": 4096,
    "school/syllabus.pdf": 1024,
}


def fake_list(**kw):
    prefix, delim = kw.get("Prefix", ""), kw.get("Delimiter")
    contents, prefixes = [], set()
    for key, size in OBJECTS.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if delim and delim in rest:
            prefixes.add(prefix + rest.split(delim)[0] + delim)
        else:
            contents.append(
                {"Key": key, "Size": size, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            )
    return {
        "Contents": contents,
        "CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)],
        "IsTruncated": False,
    }


def fake_head(Bucket=None, Key=None):
    if Key not in OBJECTS:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
    return {"ContentLength": OBJECTS[Key]}


class Base(unittest.TestCase):
    HOST = "files.kelliher.info"

    def setUp(self):
        L.app.config["TESTING"] = True
        self.c = L.app.test_client()
        L.s3.list_objects_v2 = fake_list
        L.s3.head_object = fake_head
        for g in L.ACL.list_grants():
            L.ACL.revoke(g["subject"], g["path"], g["perm"])

    def get(self, path, user="dad", groups="", host=None):
        h = {}
        if user is not None:
            h["Remote-User"] = user
        if groups is not None:
            h["Remote-Groups"] = groups
        return self.c.get(path, headers=h, base_url=f"http://{host or self.HOST}")

    def grant_dad(self):
        L.ACL.grant("user:dad", "dad/", "read", "admin")
        L.ACL.grant("user:dad", "dad/", "list", "admin")


class Identity(Base):
    def test_no_remote_user_is_refused(self):
        self.grant_dad()
        for p in ("/", "/dad/", "/dad/notes.txt"):
            r = self.get(p, user=None)
            self.assertEqual(r.status_code, 401, p)

    def test_empty_remote_user_is_refused(self):
        r = self.get("/dad/notes.txt", user="")
        self.assertEqual(r.status_code, 401)

    def test_a_forged_group_header_cannot_invent_a_grant(self):
        # Caddy strips client Remote-*; this only proves the app does not
        # trust a group name that holds no grant.
        r = self.get("/wfh/Rental_Agreement.pdf", user="dad", groups="files-admin")
        self.assertEqual(r.status_code, 404)


class Objects(Base):
    def test_object_without_a_grant_is_404_not_403(self):
        r = self.get("/wfh/Rental_Agreement.pdf")
        self.assertEqual(r.status_code, 404)

    def test_object_with_a_grant_redirects_to_a_presigned_url(self):
        self.grant_dad()
        r = self.get("/dad/Mitchell-MachineLearning-1997.pdf")
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertIn("X-Amz-Signature", loc)
        self.assertIn("X-Amz-Expires=120", loc)
        self.assertIn("Mitchell-MachineLearning-1997.pdf", loc)

    def test_a_granted_but_absent_object_is_404(self):
        self.grant_dad()
        r = self.get("/dad/not-here.pdf")
        self.assertEqual(r.status_code, 404)

    def test_traversal_is_refused(self):
        self.grant_dad()
        for p in ("/dad/../wfh/Rental_Agreement.pdf", "/dad/%2e%2e/wfh/contacts.pdf"):
            self.assertEqual(self.get(p).status_code, 404, p)

    def test_sibling_prefix_does_not_inherit(self):
        L.ACL.grant("user:dad", "dad/", "read", "admin")
        OBJECTS["dadtaxes/secret.pdf"] = 10
        try:
            self.assertEqual(self.get("/dadtaxes/secret.pdf").status_code, 404)
        finally:
            del OBJECTS["dadtaxes/secret.pdf"]


class Directories(Base):
    def test_listing_without_list_and_without_grants_is_404(self):
        self.assertEqual(self.get("/wfh/").status_code, 404)

    def test_listing_with_list_shows_contents(self):
        self.grant_dad()
        r = self.get("/dad/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Mitchell-MachineLearning-1997.pdf", r.data)
        self.assertIn(b"notes.txt", r.data)

    def test_root_shows_only_granted_subtrees(self):
        self.grant_dad()
        r = self.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"dad", r.data)
        self.assertNotIn(b"wfh", r.data)
        self.assertNotIn(b"school", r.data)

    def test_admin_root_grant_sees_everything(self):
        L.ACL.grant("group:files-admin", "", "read", "admin")
        L.ACL.grant("group:files-admin", "", "list", "admin")
        r = self.get("/", user="admin", groups="files-admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"wfh", r.data)
        self.assertIn(b"dad", r.data)
        self.assertIn(b"school", r.data)

    def test_a_name_carrying_markup_is_escaped(self):
        # The page is compiled once at import rather than per request, so
        # autoescape has to come from the environment and not from the call.
        self.grant_dad()
        OBJECTS["dad/<script>alert(1)</script>.pdf"] = 10
        try:
            r = self.get("/dad/")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(b"<script>", r.data)
            self.assertIn(b"&lt;script&gt;", r.data)
        finally:
            del OBJECTS["dad/<script>alert(1)</script>.pdf"]

    def test_admin_can_read_a_private_document(self):
        L.ACL.grant("group:files-admin", "", "read", "admin")
        r = self.get("/wfh/Rental_Agreement.pdf", user="admin", groups="files-admin")
        self.assertEqual(r.status_code, 302)


class Acceptance(Base):
    def test_dad_reaches_his_directory_and_nothing_else(self):
        self.grant_dad()
        allowed = ["/dad/", "/dad/Mitchell-MachineLearning-1997.pdf", "/dad/notes.txt"]
        denied = [
            "/wfh/",
            "/wfh/Rental_Agreement.pdf",
            "/wfh/contacts.pdf",
            "/school/",
            "/school/syllabus.pdf",
        ]
        for p in allowed:
            self.assertIn(self.get(p).status_code, (200, 302), p)
        for p in denied:
            self.assertEqual(self.get(p).status_code, 404, p)


class Admission(Base):
    """Overload sheds at the door, read from waitress's own queue.

    The dispatcher here is the real one from a real server bound to an
    ephemeral port, because a counter kept alongside the queue would be a
    mock of the thing under test and would drift under load.
    """

    def setUp(self):
        super().setUp()
        from waitress import create_server

        self.server = create_server(L.app, host="127.0.0.1", port=0, threads=4)
        L.ADMISSION.watch(self.server)
        self.grant_dad()

    def tearDown(self):
        self.server.close()
        L.ADMISSION._dispatcher = None

    def fill_queue(self, n):
        d = self.server.task_dispatcher
        with d.lock:
            for _ in range(n):
                d.queue.append(object())

    def drain_queue(self):
        d = self.server.task_dispatcher
        with d.lock:
            d.queue.clear()

    def test_an_empty_queue_serves_normally(self):
        self.assertEqual(self.get("/dad/").status_code, 200)
        self.assertEqual(self.get("/dad/notes.txt").status_code, 302)

    def test_a_deep_queue_sheds_with_503_and_retry_after(self):
        self.fill_queue(L.MAX_QUEUE + 1)
        r = self.get("/dad/")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["Retry-After"], "2")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_healthz_is_never_shed(self):
        self.fill_queue(L.MAX_QUEUE * 10)
        r = self.c.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_the_limit_is_what_decides(self):
        # Negative control: the same queue depth serves when the limit is
        # above it, so the test is driving the predicate and not something else.
        self.fill_queue(L.MAX_QUEUE + 1)
        self.assertEqual(self.get("/dad/").status_code, 503)
        old = L.ADMISSION._limit
        L.ADMISSION._limit = L.MAX_QUEUE * 100
        try:
            self.assertEqual(self.get("/dad/").status_code, 200)
        finally:
            L.ADMISSION._limit = old

    def test_shedding_stops_when_the_queue_drains(self):
        self.fill_queue(L.MAX_QUEUE + 1)
        self.assertEqual(self.get("/dad/").status_code, 503)
        self.drain_queue()
        self.assertEqual(self.get("/dad/").status_code, 200)
        self.assertEqual(self.get("/dad/notes.txt").status_code, 302)

    def test_a_shed_reveals_nothing_about_the_path(self):
        # 503 is not a denial, so it must not become the oracle that the
        # 404-never-403 rule exists to deny.
        self.fill_queue(L.MAX_QUEUE + 1)
        granted = self.get("/dad/notes.txt")
        denied = self.get("/wfh/Rental_Agreement.pdf")
        absent = self.get("/dad/not-here-at-all.pdf")
        for r in (granted, denied, absent):
            self.assertEqual(r.status_code, 503)
        self.assertEqual(granted.data, denied.data)
        self.assertEqual(granted.data, absent.data)

    def test_a_denial_is_still_404_when_not_overloaded(self):
        self.assertEqual(self.get("/wfh/Rental_Agreement.pdf").status_code, 404)

    def test_the_dispatcher_is_the_servers_own(self):
        self.assertIs(L.ADMISSION._dispatcher, self.server.task_dispatcher)
        self.assertEqual(L.ADMISSION.waiting(), 0)
        self.fill_queue(3)
        self.assertEqual(L.ADMISSION.waiting(), 3)

    def test_no_server_means_no_shedding(self):
        L.ADMISSION._dispatcher = None
        self.assertEqual(L.ADMISSION.waiting(), 0)
        self.assertEqual(self.get("/dad/").status_code, 200)


class Host(Base):
    def test_graveyard_host_is_refused(self):
        self.grant_dad()
        r = self.get("/dad/notes.txt", host="graveyard.kelliher.info")
        self.assertEqual(r.status_code, 404)

    def test_healthz_needs_no_identity(self):
        r = self.c.get("/healthz")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
