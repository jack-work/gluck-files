#!/usr/bin/env python3
"""buckets_for: which places a caller holds a row for, and never one they do not.

The ACL is real sqlite. The Knight reads this function on the auth path, so the
refusals carry most of the weight: a bucket appears only for a caller who holds
a read or list row inside it, from the same rows the listing decides on.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import Acl  # noqa: E402
from buckets import buckets_for, current_id, default_bucket  # noqa: E402

TMP = tempfile.mkdtemp()
KEYS = {"id", "label", "root_url", "access", "kind"}


def entry(id, label, root_url, access, kind):
    return {"id": id, "label": label, "root_url": root_url, "access": access, "kind": kind}


class Base(unittest.TestCase):
    def setUp(self):
        self.acl = Acl(os.path.join(TMP, self.id() + ".db"))

    def tearDown(self):
        self.acl.close()

    def give(self, subject, path, *perms):
        for perm in perms:
            self.acl.grant(subject, path, perm, "fixture")

    def inject(self, subject, path, perm):
        self.acl._db.execute(
            "INSERT OR REPLACE INTO grants (subject, path, perm, granted_by, granted_at) VALUES (?,?,?,?,?)",
            (subject, path, perm, "fixture", 1))

    def found(self, user, groups=()):
        return buckets_for(self.acl, user, list(groups))

    def ids(self, user, groups=()):
        return [b["id"] for b in self.found(user, groups)]


class WhatIsListed(Base):
    def test_a_home_only_user_has_one_bucket(self):
        self.give("user:pilar", "users/pilar/", "read", "list", "write")
        self.assertEqual(self.found("pilar"), [
            entry("home", "Your files", "/users/pilar/", ["read", "list", "write"], "home")])

    def test_a_shared_reader_has_the_shared_tree_and_no_home(self):
        self.give("user:dad", "dad/", "read", "list")
        self.assertEqual(self.found("dad"), [entry("shared", "Shared", "/", ["read", "list"], "shared")])

    def test_a_group_row_opens_the_shared_tree_for_its_members(self):
        self.give("group:family", "photos/", "read", "list")
        self.assertEqual(self.ids("someone", ["family"]), ["shared"])
        self.assertEqual(self.ids("someone", ["other-group"]), [])

    def test_a_granted_home_is_named_for_its_owner(self):
        self.give("user:admin", "users/ctkelliher/", "read", "list", "write")
        self.assertEqual(self.found("admin", ["files-admin"]), [
            entry("ctkelliher", "ctkelliher", "/users/ctkelliher/", ["read", "list", "write"], "granted")])

    def test_a_row_on_a_folder_inside_a_home_opens_that_home_at_its_root_url(self):
        self.give("user:admin", "users/ctkelliher/camera/", "read", "list")
        self.assertEqual(self.found("admin"), [
            entry("ctkelliher", "ctkelliher", "/users/ctkelliher/", ["read", "list"], "granted")])

    def test_everything_at_once_is_home_then_granted_by_owner_then_shared(self):
        self.give("user:admin", "users/admin/", "read", "list", "write")
        self.give("group:files-admin", "", "read", "list", "write")
        self.give("user:admin", "users/zoe/", "read", "list")
        self.give("user:admin", "users/ctkelliher/", "read", "list", "write")
        self.assertEqual(self.found("admin", ["files-admin"]), [
            entry("home", "Your files", "/users/admin/", ["read", "list", "write"], "home"),
            entry("ctkelliher", "ctkelliher", "/users/ctkelliher/", ["read", "list", "write"], "granted"),
            entry("zoe", "zoe", "/users/zoe/", ["read", "list"], "granted"),
            entry("shared", "Shared", "/", ["read", "list", "write"], "shared"),
        ])

    def test_access_is_in_one_order_whatever_the_order_of_the_rows(self):
        self.give("user:pilar", "users/pilar/", "write", "list", "read")
        self.assertEqual(self.found("pilar")[0]["access"], ["read", "list", "write"])

    def test_access_is_the_union_over_the_rows_inside(self):
        self.give("user:admin", "users/pilar/", "list")
        self.give("user:admin", "users/pilar/camera/", "read")
        self.assertEqual(self.found("admin")[0]["access"], ["read", "list"])

    def test_every_entry_has_exactly_the_agreed_keys_and_unique_ids(self):
        self.give("user:admin", "users/admin/", "read", "list")
        self.give("user:admin", "dad/", "list")
        self.give("user:admin", "users/pilar/", "read", "list")
        got = self.found("admin")
        self.assertEqual(len(got), 3)
        for b in got:
            self.assertEqual(set(b), KEYS)
        self.assertEqual(len({b["id"] for b in got}), 3)

    def test_the_answer_follows_the_rows_at_once(self):
        self.give("user:admin", "users/pilar/", "read", "list")
        self.assertEqual(self.ids("admin"), ["pilar"])
        self.acl.revoke("user:admin", "users/pilar/")
        self.assertEqual(self.ids("admin"), [])


class EveryCallerWithSomethingToListHasAHome(Base):
    def test_admin_with_only_the_root_grant_has_home_at_the_root_and_no_shared(self):
        self.give("group:files-admin", "", "read", "list", "write")
        self.give("user:admin", "users/ctkelliher/", "read", "list", "write")
        self.assertEqual(self.found("admin", ["files-admin"]), [
            entry("home", "Your files", "/", ["read", "list", "write"], "home"),
            entry("ctkelliher", "ctkelliher", "/users/ctkelliher/", ["read", "list", "write"], "granted"),
        ])

    def test_an_ordinary_user_has_her_home_under_users(self):
        self.give("user:pilar", "users/pilar/", "read", "list", "write")
        self.give("group:family", "photos/", "read", "list")
        got = self.found("pilar", ["family"])
        self.assertEqual([(b["id"], b["root_url"]) for b in got], [("home", "/users/pilar/"), ("shared", "/")])

    def test_a_home_row_wins_over_the_root_grant_and_shared_stays(self):
        self.give("group:files-admin", "", "read", "list", "write")
        self.give("user:admin", "users/admin/", "read", "list", "write")
        got = self.found("admin", ["files-admin"])
        self.assertEqual([(b["id"], b["root_url"]) for b in got], [("home", "/users/admin/"), ("shared", "/")])

    def test_a_caller_with_no_rows_has_no_home_and_no_default(self):
        self.give("group:files-admin", "", "read", "list", "write")
        got = self.found("stranger", ["family"])
        self.assertEqual(got, [])
        self.assertIsNone(default_bucket(got))

    def test_the_root_home_needs_a_list_row_at_the_root(self):
        self.give("group:files-admin", "", "read", "write")
        self.assertEqual(self.found("admin", ["files-admin"]), [])
        self.give("group:files-admin", "photos/", "list")
        self.assertEqual(self.ids("admin", ["files-admin"]), ["shared"])

    def test_the_root_home_access_comes_from_the_root_rows_alone(self):
        self.give("group:files-admin", "", "read", "list")
        self.give("group:files-admin", "inbox/", "write")
        self.assertEqual(self.found("admin", ["files-admin"])[0]["access"], ["read", "list"])

    def test_the_root_grant_can_be_the_callers_own_row(self):
        self.give("user:solo", "", "list")
        self.assertEqual(self.found("solo"), [entry("home", "Your files", "/", ["list"], "home")])

    def test_a_row_below_the_root_is_not_the_root_grant(self):
        self.give("user:dad", "dad/", "read", "list")
        self.assertEqual(self.found("dad"), [entry("shared", "Shared", "/", ["read", "list"], "shared")])


class AGrantedHomeCannotTakeAReservedId(Base):
    def test_owners_named_home_or_shared_are_prefixed(self):
        self.give("user:admin", "users/home/", "read", "list")
        self.give("user:admin", "users/shared/", "read", "list")
        self.give("user:admin", "users/users/", "read", "list")
        self.give("group:files-admin", "", "read", "list")
        got = self.found("admin", ["files-admin"])
        self.assertEqual([(b["id"], b["label"], b["root_url"]) for b in got], [
            ("home", "Your files", "/"),
            ("user-home", "home", "/users/home/"),
            ("user-shared", "shared", "/users/shared/"),
            ("users", "users", "/users/users/"),
        ])
        self.assertEqual([b["kind"] for b in got], ["home", "granted", "granted", "granted"])

    def test_the_escape_cannot_be_forged_by_an_owner_who_spells_it(self):
        for owner in ("home", "user-home", "user-user-home", "shared", "user-shared"):
            self.give("user:admin", f"users/{owner}/", "read", "list")
        got = self.found("admin")
        ids = [b["id"] for b in got]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertNotIn("home", ids)
        self.assertNotIn("shared", ids)
        self.assertEqual({b["label"]: b["id"] for b in got}, {
            "home": "user-home", "user-home": "user-user-home", "user-user-home": "user-user-user-home",
            "shared": "user-shared", "user-shared": "user-user-shared"})

    def test_a_user_named_home_has_a_home_and_the_id_is_still_unique(self):
        self.give("user:home", "users/home/", "read", "list")
        self.give("user:home", "users/pilar/", "read", "list")
        self.assertEqual(self.ids("home"), ["home", "pilar"])
        self.give("user:pilar", "users/home/", "read", "list")
        self.assertEqual(self.ids("pilar"), ["user-home"])


class NeverABucketWithoutARow(Base):
    def setUp(self):
        super().setUp()
        self.give("user:pilar", "users/pilar/", "read", "list", "write")
        self.give("user:other", "users/other/", "read", "list")
        self.give("user:dad", "dad/", "read", "list")
        self.give("group:files-admin", "", "read", "list", "write")

    def test_no_row_no_bucket(self):
        self.assertEqual(self.found("stranger"), [])
        self.assertEqual(self.found("stranger", ["family", "pilar", "other", "dad", "files-admin-not"]), [])

    def test_no_caller_no_bucket(self):
        for user in (None, ""):
            self.assertEqual(self.found(user), [])
            self.assertEqual(self.found(user, []), [])

    def test_the_groups_of_no_caller_open_only_the_root(self):
        for user in (None, ""):
            self.assertEqual(self.found(user, ["files-admin"]), [
                entry("home", "Your files", "/", ["read", "list", "write"], "home")])

    def test_the_admin_group_is_not_a_key_to_anyones_home_row(self):
        self.assertEqual(self.found("root", ["files-admin"]), [
            entry("home", "Your files", "/", ["read", "list", "write"], "home")])
        self.assertEqual(self.ids("pilar", ["files-admin"]), ["home", "shared"])
        self.assertEqual(self.found("pilar", ["files-admin"])[0]["root_url"], "/users/pilar/")

    def test_a_group_row_under_users_is_inert_even_when_it_is_in_the_table(self):
        self.inject("group:files-admin", "users/other/", "read")
        self.inject("group:files-admin", "users/other/", "list")
        self.inject("group:family", "users/pilar/camera/", "read")
        self.assertEqual(self.ids("root", ["files-admin"]), ["home"])
        self.assertEqual(self.ids("friend", ["family"]), [])

    def test_a_row_naming_someone_else_is_not_mine(self):
        self.assertEqual(self.ids("pilar"), ["home"])
        self.assertEqual(self.ids("other"), ["home"])
        self.assertEqual(self.ids("dad"), ["shared"])

    def test_a_name_that_is_a_prefix_or_extension_of_the_subject_is_not_the_subject(self):
        for user in ("pila", "pilar2", "pilar-", "Pilar", "pilar ", "pilar%", "pil_r"):
            self.assertEqual(self.found(user), [], user)

    def test_a_sibling_prefix_is_not_the_home(self):
        self.give("user:pilar2", "users/pilar2/", "read", "list")
        self.assertEqual(self.ids("pilar"), ["home"])
        self.assertEqual(self.ids("pilar2"), ["home"])
        self.assertEqual(self.found("pilar2")[0]["root_url"], "/users/pilar2/")

    def test_someone_granted_into_a_home_does_not_get_a_home_of_their_own(self):
        self.give("user:other", "users/pilar/", "read", "list")
        got = self.found("other")
        self.assertEqual([(b["id"], b["kind"]) for b in got], [("home", "home"), ("pilar", "granted")])
        self.assertEqual(got[0]["root_url"], "/users/other/")

    def test_a_grant_into_a_home_does_not_go_the_other_way(self):
        self.give("user:other", "users/pilar/", "read", "list")
        self.assertEqual(self.ids("pilar"), ["home"])

    def test_a_write_only_row_shows_nothing_to_open(self):
        self.give("user:scribe", "users/pilar/", "write")
        self.give("user:scribe", "inbox/", "write")
        self.give("user:scribe", "users/scribe/", "write")
        self.assertEqual(self.found("scribe"), [])

    def test_write_is_listed_beside_the_list_that_makes_the_bucket(self):
        self.give("user:scribe", "inbox/", "write", "list")
        self.assertEqual(self.found("scribe")[0]["access"], ["list", "write"])

    def test_a_read_only_row_makes_no_bucket_at_any_depth(self):
        self.give("user:eve", "users/pilar/", "read")
        self.give("user:eve", "users/other/photos/", "read")
        self.give("user:eve", "users/zoe/photos/2024/", "read", "write")
        self.give("user:eve", "dad/", "read")
        self.give("user:eve", "docs/2024/", "read")
        self.give("group:readers", "library/", "read")
        self.assertEqual(self.found("eve", ["readers"]), [])

    def test_a_read_only_row_beside_a_list_row_adds_nothing_but_its_access(self):
        self.give("user:eve", "dad/", "read")
        self.give("user:eve", "users/other/photos/", "list")
        got = self.found("eve")
        self.assertEqual([b["id"] for b in got], ["other"])
        self.assertEqual(got[0]["access"], ["list"])

    def test_the_users_folder_itself_is_never_a_bucket(self):
        self.inject("user:eve", "users/", "read")
        self.inject("user:eve", "users/", "list")
        self.inject("user:eve", "users", "read")
        self.inject("user:eve", "users", "list")
        self.assertEqual(self.ids("eve"), ["shared"])

    def test_a_home_whose_owner_is_not_a_well_formed_name_is_left_out(self):
        for path in ("users/ab/", "users/Pilar/", "users//", "users/pilar", "users/-pilar/", "users/" + "a" * 40 + "/"):
            self.inject("user:eve", path, "read")
            self.inject("user:eve", path, "list")
        self.assertEqual(self.ids("eve"), [])

    def test_a_caller_name_that_is_not_a_well_formed_name_has_no_home(self):
        for user in ("Eve", "ev", "e/ve", "eve/x", "eve/../pilar", "eve,user:pilar", "../pilar"):
            self.inject(f"user:{user}", f"users/{user}/", "read")
            self.inject(f"user:{user}", f"users/{user}/", "list")
            self.assertEqual(self.found(user), [], user)

    def test_a_row_that_is_not_in_canonical_form_is_inert_as_it_is_in_the_acl(self):
        for path in ("users/other/../pilar/", "users//other/", "users/./other/", "dad/../wfh/", "/dad/", "dad//"):
            self.inject("user:eve", path, "read")
            self.inject("user:eve", path, "list")
        self.assertEqual(self.found("eve"), [])

    def test_a_name_spoofing_a_second_subject_matches_nothing(self):
        self.assertEqual(self.found("pilar,user:other"), [])
        self.assertEqual(self.found("x", ["a,group:files-admin"]), [])
        self.assertEqual(self.found("x' OR '1'='1"), [])
        self.assertEqual(self.found("%"), [])

    def test_a_row_deeper_than_the_root_does_not_open_the_shared_tree_by_its_prefix(self):
        self.give("user:scribe", "users-archive/", "read", "list")
        self.assertEqual(self.ids("scribe"), ["shared"])
        self.assertEqual(self.found("scribe")[0]["root_url"], "/")

    def test_no_garage_bucket_name_is_in_any_answer(self):
        self.give("user:admin", "users/admin/", "read", "list")
        self.give("user:admin", "users/pilar/", "read", "list")
        self.give("user:ct_kelliher", "users/ct_kelliher/", "read")
        for user, groups in (("admin", ["files-admin"]), ("pilar", []), ("ct_kelliher", []), ("other", [])):
            text = json.dumps(self.found(user, groups))
            for owner in ("admin", "pilar", "ct_kelliher", "ct.kelliher", "other"):
                self.assertNotIn("u-" + owner, text)
                self.assertNotIn("u-" + owner.replace("_", "."), text)

    def test_every_id_is_home_shared_or_home_owner_and_nothing_else(self):
        self.give("user:admin", "users/admin/", "read", "list")
        self.give("user:admin", "users/pilar/", "read", "list")
        self.give("user:admin", "users/other/", "read", "list")
        self.give("user:admin", "dad/", "read", "list")
        ids = self.ids("admin", ["files-admin"])
        self.assertEqual(ids, ["home", "other", "pilar", "shared"])
        for b in self.found("admin", ["files-admin"]):
            self.assertIn(b["kind"], ("home", "shared", "granted"))
            self.assertTrue(b["root_url"].startswith("/"))
            self.assertTrue(b["root_url"] == "/" or b["root_url"].endswith("/"))

    def test_the_function_does_not_write(self):
        before = self.acl.list_grants()
        self.found("admin", ["files-admin"])
        self.found("pilar")
        self.assertEqual(self.acl.list_grants(), before)


class WhereAPrefixSits(unittest.TestCase):
    def test_a_listing_prefix_maps_to_the_bucket_with_the_deepest_root_url(self):
        buckets = [
            entry("home", "Your files", "/users/pilar/", ["read", "list"], "home"),
            entry("other", "other", "/users/other/", ["read", "list"], "granted"),
            entry("shared", "Shared", "/", ["read", "list"], "shared"),
        ]
        for prefix, want in (
            ("users/pilar/", "home"),
            ("users/pilar/camera/2024/", "home"),
            ("users/other/", "other"),
            ("users/other/camera/", "other"),
            ("", "shared"),
            ("dad/", "shared"),
            ("users-archive/", "shared"),
            ("users/pilar2/", "shared"),
        ):
            self.assertEqual(current_id(prefix, buckets), want, prefix)

    def test_no_bucket_is_current_outside_every_root_url(self):
        buckets = [entry("home", "Your files", "/users/pilar/", ["read", "list"], "home")]
        self.assertIsNone(current_id("users/", buckets))
        self.assertIsNone(current_id("dad/", buckets))
        self.assertIsNone(current_id("", []))


class WhereACallerLands(unittest.TestCase):
    def test_home_then_shared_then_the_first_bucket_then_nothing(self):
        home = entry("home", "Your files", "/users/me/", ["list"], "home")
        shared = entry("shared", "Shared", "/", ["list"], "shared")
        granted = entry("zoe", "zoe", "/users/zoe/", ["list"], "granted")
        self.assertEqual(default_bucket([granted, shared, home]), home)
        self.assertEqual(default_bucket([granted, shared]), shared)
        self.assertEqual(default_bucket([granted]), granted)
        self.assertIsNone(default_bucket([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
