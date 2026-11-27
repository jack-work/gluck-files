#!/usr/bin/env python3
"""GET /_files/api/buckets: the JSON, the no-store header, and who is refused.

The ACL is real sqlite and Garage is the gateway test's stand-in. The body comes
from buckets_for, the same function the listing's menu is built on.
"""

import unittest

from test_gateway import Base, L

ADMIN = ("admin", "files-admin")


class Asking(Base):
    def ask(self, path, user="admin", groups="files-admin", host=None, **headers):
        h = dict(headers)
        if user is not None:
            h["Remote-User"] = user
        if groups is not None:
            h["Remote-Groups"] = groups
        return self.c.get(path, headers=h, base_url=f"http://{host or self.HOST}")


class Api(Asking):
    def setUp(self):
        super().setUp()
        L.ACL.grant("user:pilar", "users/pilar/", "read", "t")
        L.ACL.grant("user:pilar", "users/pilar/", "list", "t")
        L.ACL.grant("user:pilar", "users/pilar/", "write", "t")
        for p in ("read", "list", "write"):
            L.ACL.grant("group:files-admin", "", p, "t")
            L.ACL.grant("user:admin", "users/admin/", p, "t")
            L.ACL.grant("user:admin", "users/ctkelliher/", p, "t")
        L.ACL.grant("user:dad", "dad/", "read", "t")
        L.ACL.grant("user:dad", "dad/", "list", "t")

    def api(self, user="admin", groups="files-admin", host=None, **headers):
        return self.ask("/_files/api/buckets", user, groups, host, **headers)

    def test_the_answer_is_json_with_the_home_id_and_the_buckets(self):
        r = self.api()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/json")
        self.assertEqual(r.get_json(), {"home": "home", "buckets": [
            {"id": "home", "label": "Your files", "root_url": "/users/admin/", "access": ["read", "list", "write"], "kind": "home"},
            {"id": "ctkelliher", "label": "ctkelliher", "root_url": "/users/ctkelliher/",
             "access": ["read", "list", "write"], "kind": "granted"},
            {"id": "shared", "label": "Shared", "root_url": "/", "access": ["read", "list", "write"], "kind": "shared"},
        ]})

    def test_the_root_grant_is_a_home_at_the_root_when_there_is_no_home_row(self):
        L.ACL.revoke("user:admin", "users/admin/")
        self.assertEqual(self.api().get_json(), {"home": "home", "buckets": [
            {"id": "home", "label": "Your files", "root_url": "/", "access": ["read", "list", "write"], "kind": "home"},
            {"id": "ctkelliher", "label": "ctkelliher", "root_url": "/users/ctkelliher/",
             "access": ["read", "list", "write"], "kind": "granted"},
        ]})

    def test_it_is_never_cached_whoever_asks_and_whatever_the_answer(self):
        for r in (self.api(), self.api("pilar", ""), self.api("nobody", ""), self.api(user=None),
                  self.api(host="graveyard.kelliher.info"), self.api(**{"Remote-Client": "player"})):
            self.assertEqual(r.headers.get("Cache-Control"), "no-store", r.status_code)

    def test_a_home_only_user_gets_her_home_and_nothing_else(self):
        self.assertEqual(self.api("pilar", "").get_json(), {"home": "home", "buckets": [
            {"id": "home", "label": "Your files", "root_url": "/users/pilar/", "access": ["read", "list", "write"], "kind": "home"}]})

    def test_a_shared_reader_lands_in_the_shared_tree(self):
        self.assertEqual(self.api("dad", "").get_json(), {"home": "shared", "buckets": [
            {"id": "shared", "label": "Shared", "root_url": "/", "access": ["read", "list"], "kind": "shared"}]})

    def test_a_caller_with_no_rows_is_told_there_is_nothing_not_refused(self):
        r = self.api("nobody", "")
        self.assertEqual((r.status_code, r.get_json()), (200, {"home": None, "buckets": []}))

    def test_the_caller_is_the_remote_user_and_nothing_in_the_query_changes_that(self):
        want = self.api("pilar", "").get_json()
        for q in ("?user=admin", "?Remote-User=admin", "?groups=files-admin", "?bucket=ctkelliher"):
            self.assertEqual(self.ask("/_files/api/buckets" + q, "pilar", "").get_json(), want, q)

    def test_it_is_the_same_function_the_listing_uses(self):
        for user, groups in (ADMIN, ("pilar", ""), ("dad", ""), ("nobody", "")):
            got = self.api(user, groups).get_json()["buckets"]
            self.assertEqual(got, L.buckets_for(user, [g for g in groups.split(",") if g]))

    def test_no_remote_user_is_refused(self):
        for user in (None, "", "  "):
            r = self.api(user=user)
            self.assertEqual(r.status_code, 401, repr(user))
            self.assertNotIn(b"buckets", r.data)

    def test_a_forged_host_is_a_404(self):
        for host in ("graveyard.kelliher.info", "kelliher.info", "files.evil.com"):
            r = self.api(host=host)
            self.assertEqual(r.status_code, 404, host)
            self.assertNotIn(b"buckets", r.data)

    def test_a_bearer_client_is_refused_the_list_of_buckets(self):
        r = self.api(**{"Remote-Client": "player"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(b"home", r.data)

    def test_it_only_reads(self):
        for method in ("post", "put", "delete", "patch"):
            r = getattr(self.c, method)("/_files/api/buckets", headers={"Remote-User": "admin"}, base_url="http://files.kelliher.info")
            self.assertEqual(r.status_code, 405, method)

    def test_no_garage_bucket_name_is_in_the_body(self):
        for user, groups in (ADMIN, ("pilar", "")):
            body = self.api(user, groups).get_data(as_text=True)
            for owner in ("admin", "pilar", "ctkelliher"):
                self.assertNotIn("u-" + owner, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
