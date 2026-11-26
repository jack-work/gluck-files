#!/usr/bin/env python3
"""ACL tests. The property under test is that nothing is reachable that was not
granted, and that a grant on a prefix does not leak sideways."""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aclctl  # noqa: E402
from acl import Acl, BadPath, ancestors, normalise  # noqa: E402


class Normalise(unittest.TestCase):
    def test_strips_leading_and_duplicate_slashes(self):
        self.assertEqual(normalise("/dad/x.pdf"), "dad/x.pdf")
        self.assertEqual(normalise("//dad///x.pdf"), "dad/x.pdf")
        self.assertEqual(normalise("dad/"), "dad/")
        self.assertEqual(normalise("/dad//"), "dad/")

    def test_keeps_the_trailing_slash_distinction(self):
        # Load-bearing: a grant on `dad/` must not cover `dadtaxes`.
        self.assertEqual(normalise("dad/"), "dad/")
        self.assertEqual(normalise("dad"), "dad")
        self.assertNotEqual(normalise("dad/"), normalise("dad"))

    def test_refuses_traversal_rather_than_resolving_it(self):
        for bad in ("../etc", "dad/../wfh", "a/b/../../c", "..", "dad/.."):
            with self.assertRaises(BadPath, msg=bad):
                normalise(bad)

    def test_refuses_null_byte_and_none(self):
        with self.assertRaises(BadPath):
            normalise("dad/\x00x")
        with self.assertRaises(BadPath):
            normalise(None)

    def test_dot_segments_are_dropped(self):
        self.assertEqual(normalise("./dad/./x.pdf"), "dad/x.pdf")


class Ancestors(unittest.TestCase):
    def test_object(self):
        self.assertEqual(
            ancestors("a/b/c.pdf"), ["", "a/", "a/b/", "a/b/c.pdf"]
        )

    def test_directory(self):
        self.assertEqual(ancestors("a/b/"), ["", "a/", "a/b/"])

    def test_root(self):
        self.assertEqual(ancestors(""), [""])

    def test_single_object_at_root(self):
        self.assertEqual(ancestors("x.pdf"), ["", "x.pdf"])


class Allows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.acl = Acl(os.path.join(self.dir, "acl.db"))

    def tearDown(self):
        self.acl.close()

    def test_nothing_is_allowed_by_default(self):
        for p in ("", "dad/", "dad/x.pdf", "wfh/Rental_Agreement.pdf"):
            self.assertFalse(self.acl.allows("dad", [], p, "read"), p)
            self.assertFalse(self.acl.allows("dad", [], p, "list"), p)

    def test_a_grant_on_a_prefix_is_inherited_downward(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertTrue(self.acl.allows("dad", [], "dad/x.pdf", "read"))
        self.assertTrue(self.acl.allows("dad", [], "dad/deep/er/y.pdf", "read"))

    def test_a_grant_does_not_leak_to_a_sibling_with_a_shared_prefix(self):
        # The bug this trailing slash exists to prevent.
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertFalse(self.acl.allows("dad", [], "dadtaxes/x.pdf", "read"))
        self.assertFalse(self.acl.allows("dad", [], "dad2/x.pdf", "read"))

    def test_a_grant_does_not_leak_upward(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertFalse(self.acl.allows("dad", [], "", "read"))
        self.assertFalse(self.acl.allows("dad", [], "wfh/Rental_Agreement.pdf", "read"))

    def test_the_acceptance_case(self):
        # dad may read his own directory and nothing else in a 157-object bucket.
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.acl.grant("user:dad", "dad/", "list", "admin")
        self.assertTrue(self.acl.allows("dad", [], "dad/Mitchell.pdf", "read"))
        self.assertTrue(self.acl.allows("dad", [], "dad/", "list"))
        for forbidden in (
            "wfh/Rental_Agreement.pdf",
            "wfh/",
            "",
            "contacts.pdf",
            "school/x.pdf",
        ):
            self.assertFalse(self.acl.allows("dad", [], forbidden, "read"), forbidden)
            self.assertFalse(self.acl.allows("dad", [], forbidden, "list"), forbidden)

    def test_perms_are_independent(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertTrue(self.acl.allows("dad", [], "dad/x", "read"))
        self.assertFalse(self.acl.allows("dad", [], "dad/x", "list"))
        self.assertFalse(self.acl.allows("dad", [], "dad/x", "write"))

    def test_group_grants(self):
        self.acl.grant("group:family", "shared/", "read", "admin")
        self.assertTrue(self.acl.allows("dad", ["family"], "shared/x", "read"))
        self.assertFalse(self.acl.allows("dad", ["other"], "shared/x", "read"))
        self.assertFalse(self.acl.allows("dad", [], "shared/x", "read"))

    def test_another_user_is_unaffected(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertFalse(self.acl.allows("mum", [], "dad/x.pdf", "read"))

    def test_no_identity_means_no_access(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertFalse(self.acl.allows(None, [], "dad/x.pdf", "read"))
        self.assertFalse(self.acl.allows("", [], "dad/x.pdf", "read"))

    def test_traversal_in_a_request_is_denied_not_resolved(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertFalse(self.acl.allows("dad", [], "dad/../wfh/Rental.pdf", "read"))

    def test_root_grant_covers_everything(self):
        self.acl.grant("group:files-admin", "", "read", "admin")
        self.assertTrue(self.acl.allows("admin", ["files-admin"], "anything/at/all", "read"))
        self.assertTrue(self.acl.allows("admin", ["files-admin"], "", "read"))

    def test_no_root_or_group_grant_reaches_a_users_home(self):
        for p in ("read", "list", "write"):
            self.acl.grant("group:files-admin", "", p, "admin")
            self.acl.grant("user:mom", "", p, "admin")
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/mom/x.jpg", "read"))
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/mom/", "list"))
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/", "list"))
        self.assertFalse(self.acl.allows("mom", [], "users/mom/x.jpg", "read"))
        self.assertTrue(self.acl.allows("admin", ["files-admin"], "notes/x", "read"))

    def test_only_the_owner_row_opens_a_users_home(self):
        self.acl.grant("user:mom", "users/mom/", "read", "admin")
        self.assertTrue(self.acl.allows("mom", [], "users/mom/a/b.jpg", "read"))
        self.assertFalse(self.acl.allows("mom", [], "users/mom/a/b.jpg", "write"))
        self.assertFalse(self.acl.allows("kid", [], "users/mom/a/b.jpg", "read"))
        self.assertFalse(self.acl.allows("mom", [], "users/kid/b.jpg", "read"))
        self.assertFalse(self.acl.allows("mom", [], "users/", "list"))

    def grant_home(self, subject, path, *perms):
        for p in perms:
            self.acl.grant(subject, path, p, "admin")

    def test_a_named_user_row_opens_another_users_home(self):
        self.grant_home("user:mom", "users/mom/", "read", "list", "write")
        self.grant_home("user:admin", "users/mom/", "read", "list", "write")
        for perm in ("read", "list", "write"):
            self.assertTrue(self.acl.allows("admin", [], "users/mom/a/b.jpg", perm), perm)
            self.assertTrue(self.acl.allows("admin", [], "users/mom/", perm), perm)
            self.assertTrue(self.acl.allows("mom", [], "users/mom/a/b.jpg", perm), perm)

    def test_the_named_user_gets_only_the_perms_the_row_names(self):
        self.grant_home("user:admin", "users/mom/", "read", "list")
        self.assertTrue(self.acl.allows("admin", [], "users/mom/a.jpg", "read"))
        self.assertTrue(self.acl.allows("admin", [], "users/mom/", "list"))
        self.assertFalse(self.acl.allows("admin", [], "users/mom/a.jpg", "write"))

    def test_a_row_on_a_folder_inside_a_home_covers_that_folder_only(self):
        self.grant_home("user:admin", "users/mom/photos/", "read", "list")
        self.assertTrue(self.acl.allows("admin", [], "users/mom/photos/keep/a.jpg", "read"))
        self.assertTrue(self.acl.allows("admin", [], "users/mom/photos/", "list"))
        self.assertFalse(self.acl.allows("admin", [], "users/mom/docs/a.pdf", "read"))
        self.assertFalse(self.acl.allows("admin", [], "users/mom/", "list"))
        self.assertFalse(self.acl.allows("admin", [], "users/mom/photosX/a.jpg", "read"))

    def test_an_ungranted_user_is_refused_where_another_is_granted(self):
        self.grant_home("user:mom", "users/mom/", "read", "list")
        self.grant_home("user:admin", "users/mom/", "read", "list")
        for perm in ("read", "list"):
            self.assertFalse(self.acl.allows("kid", [], "users/mom/a.jpg", perm))
            self.assertFalse(self.acl.allows("kid", ["files-admin", "mom", "admin"], "users/mom/a.jpg", perm))

    def test_a_group_member_is_not_the_user_the_row_names(self):
        self.grant_home("user:mom", "users/mom/", "read", "list")
        self.acl.grant("group:files-admin", "", "read", "admin")
        self.acl.grant("group:files-admin", "", "list", "admin")
        self.assertFalse(self.acl.allows("kid", ["files-admin"], "users/mom/a.jpg", "read"))
        self.assertFalse(self.acl.allows("kid", ["files-admin"], "users/mom/", "list"))

    def test_a_group_row_planted_in_a_home_is_never_honoured(self):
        self.acl._db.execute(
            "INSERT INTO grants (subject, path, perm, granted_by, granted_at) VALUES (?,?,?,?,?)",
            ("group:files-admin", "users/mom/", "read", "planted", 0))
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/mom/a.jpg", "read"))
        self.assertFalse(self.acl.allows("mom", ["files-admin"], "users/mom/a.jpg", "read"))

    def test_a_group_subject_cannot_be_granted_in_a_home(self):
        for path in ("users/mom/", "users/mom/photos/", "users/mom/a.jpg"):
            with self.assertRaises(ValueError, msg=path):
                self.acl.grant("group:files-admin", path, "read", "admin")
        self.assertEqual(self.acl.list_grants(), [])

    def test_users_itself_and_a_bare_name_cannot_be_granted_to_anyone(self):
        for path in ("users/", "users/mom"):
            for subject in ("user:admin", "user:mom", "group:files-admin"):
                with self.assertRaises(ValueError, msg=(subject, path)):
                    self.acl.grant(subject, path, "read", "admin")
        self.assertEqual(self.acl.list_grants(), [])

    def test_the_root_grant_still_stops_at_users_with_a_named_row_beside_it(self):
        for p in ("read", "list", "write"):
            self.acl.grant("group:files-admin", "", p, "admin")
            self.acl.grant("user:admin", "", p, "admin")
        self.grant_home("user:admin", "users/mom/", "read")
        self.assertTrue(self.acl.allows("admin", ["files-admin"], "users/mom/a.jpg", "read"))
        for p in ("read", "list", "write"):
            self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/kid/a.jpg", p), p)
            self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/", p), p)
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/mom/a.jpg", "write"))
        self.assertFalse(self.acl.allows("admin", ["files-admin"], "users/mom/", "list"))

    def test_a_named_row_does_not_reach_a_sibling_prefix_or_climb(self):
        self.grant_home("user:admin", "users/mom/", "read", "list")
        for path in ("users/mom2/a.jpg", "users/momma/", "users/mom", "users/", "users/kid/a.jpg",
                     "users/mom/../kid/a.jpg", "users/mom/../../wfh/x.pdf", "users/mom/./../kid/", "mom/a.jpg"):
            for perm in ("read", "list"):
                self.assertFalse(self.acl.allows("admin", [], path, perm), (path, perm))

    def test_a_row_on_a_home_does_not_open_the_grantees_own_home(self):
        self.grant_home("user:admin", "users/mom/", "read", "list", "write")
        for perm in ("read", "list", "write"):
            self.assertFalse(self.acl.allows("admin", [], "users/admin/a.jpg", perm), perm)
        self.assertFalse(self.acl.allows("mom", [], "users/admin/a.jpg", "read"))

    def test_revoking_the_named_row_closes_it_and_leaves_the_owner(self):
        self.grant_home("user:mom", "users/mom/", "read")
        self.grant_home("user:admin", "users/mom/", "read")
        self.assertEqual(self.acl.revoke_subject("user:admin"), 1)
        self.assertFalse(self.acl.allows("admin", [], "users/mom/a.jpg", "read"))
        self.assertTrue(self.acl.allows("mom", [], "users/mom/a.jpg", "read"))

    def test_an_identity_less_caller_with_groups_reaches_no_home(self):
        self.grant_home("user:mom", "users/mom/", "read")
        self.assertFalse(self.acl.allows("", ["files-admin"], "users/mom/a.jpg", "read"))
        self.assertFalse(self.acl.allows(None, ["mom"], "users/mom/a.jpg", "read"))

    def test_grant_normalises_the_stored_path(self):
        self.acl.grant("user:dad", "/dad//", "read", "admin")
        self.assertEqual(self.acl.list_grants()[0]["path"], "dad/")
        self.assertTrue(self.acl.allows("dad", [], "dad/x", "read"))

    def test_revoke(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.assertEqual(self.acl.revoke("user:dad", "dad/", "read"), 1)
        self.assertFalse(self.acl.allows("dad", [], "dad/x", "read"))

    def test_revoke_all_perms_on_a_path(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.acl.grant("user:dad", "dad/", "list", "admin")
        self.assertEqual(self.acl.revoke("user:dad", "dad/"), 2)
        self.assertEqual(self.acl.list_grants(), [])

    def test_revoke_subject_removes_everything(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        self.acl.grant("user:dad", "other/", "read", "admin")
        self.assertEqual(self.acl.revoke_subject("user:dad"), 2)
        self.assertFalse(self.acl.allows("dad", [], "dad/x", "read"))

    def test_bad_subject_and_perm_refused(self):
        with self.assertRaises(ValueError):
            self.acl.grant("dad", "dad/", "read", "admin")
        with self.assertRaises(ValueError):
            self.acl.grant("user:dad", "dad/", "sudo", "admin")

    def test_grants_survive_reopen(self):
        self.acl.grant("user:dad", "dad/", "read", "admin")
        p = os.path.join(self.dir, "acl.db")
        self.acl.close()
        again = Acl(p)
        try:
            self.assertTrue(again.allows("dad", [], "dad/x", "read"))
        finally:
            again.close()


class Ctl(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "acl.db")

    def run_ctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = aclctl.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_the_admin_row_on_a_home_goes_through_the_cli(self):
        code, out, _ = self.run_ctl("grant", "admin", "users/ctkelliher/", "--read", "--list", "--write")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("granted"), 3)
        self.assertEqual(self.run_ctl("check", "admin", "users/ctkelliher/photos/a.jpg", "read", "--groups", "files-admin")[0], 0)
        self.assertEqual(self.run_ctl("check", "admin", "users/ctkelliher/", "list")[0], 0)
        self.assertEqual(self.run_ctl("check", "admin", "users/ctkelliher/photos/a.jpg", "write")[0], 0)
        self.assertEqual(self.run_ctl("check", "kid", "users/ctkelliher/photos/a.jpg", "read", "--groups", "files-admin")[0], 1)
        self.assertEqual(self.run_ctl("check", "admin", "users/other/a.jpg", "read", "--groups", "files-admin")[0], 1)

    def test_the_cli_still_refuses_a_group_or_users_itself(self):
        for subject, path in (("group:files-admin", "users/ctkelliher/"), ("admin", "users/"), ("admin", "users/ctkelliher")):
            code, _, err = self.run_ctl("grant", subject, path, "--read")
            self.assertEqual(code, 2, (subject, path))
            self.assertIn("reserved", err)
        self.assertEqual(self.run_ctl("ls")[1].strip(), "no grants")

    def test_dropping_the_admin_closes_the_home_again(self):
        self.run_ctl("grant", "admin", "users/ctkelliher/", "--read")
        self.assertEqual(self.run_ctl("drop", "admin")[0], 0)
        self.assertEqual(self.run_ctl("check", "admin", "users/ctkelliher/a.jpg", "read")[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
