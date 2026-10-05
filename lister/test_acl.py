#!/usr/bin/env python3
"""ACL tests. The property under test is that nothing is reachable that was not
granted, and that a grant on a prefix does not leak sideways."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
