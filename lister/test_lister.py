#!/usr/bin/env python3
"""Host -> bucket resolution for the lister.

The allowlist is the security property under test: a forged Host header must
not reach a bucket that is deliberately not a website (`graveyard`).
"""

import os
import sys
import unittest

os.environ.setdefault("LISTER_TEMPLATE", "/dev/null")
os.environ["LISTER_BUCKETS"] = "files,share"
os.environ["LISTER_ROOT_DOMAIN"] = ".kelliher.info"
# boto3 resolves credentials when the client is constructed, at import time.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lister  # noqa: E402


class BucketFor(unittest.TestCase):
    def check(self, host, want):
        self.assertEqual(lister.bucket_for(host), want, f"host={host!r}")

    def test_allowlisted_hosts_resolve(self):
        self.check("files.kelliher.info", "files")
        self.check("share.kelliher.info", "share")

    def test_port_is_ignored(self):
        self.check("share.kelliher.info:8780", "share")

    def test_case_and_trailing_dot(self):
        self.check("SHARE.KELLIHER.INFO", "share")
        self.check("share.kelliher.info.", "share")

    def test_bucket_not_on_the_allowlist_is_refused(self):
        # The whole point: graveyard is not a website. Naming it must not work.
        self.check("graveyard.kelliher.info", None)
        self.check("secrets.kelliher.info", None)

    def test_wrong_root_domain_is_refused(self):
        self.check("share.evil.com", None)
        self.check("share.kelliher.info.evil.com", None)

    def test_nested_label_is_refused(self):
        # `a.share.kelliher.info` must not resolve to a bucket named "a.share".
        self.check("a.share.kelliher.info", None)

    def test_bare_root_domain_is_refused(self):
        self.check("kelliher.info", None)
        self.check(".kelliher.info", None)

    def test_missing_or_empty_host(self):
        self.check(None, None)
        self.check("", None)

    def test_suffix_confusion(self):
        # "notkelliher.info" ends with neither ".kelliher.info" nor a label.
        self.check("filesXkelliher.info", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
