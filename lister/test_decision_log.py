#!/usr/bin/env python3
import logging
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory()
os.environ.update({
    'LISTER_TEMPLATE': '/dev/null', 'LISTER_BUCKETS': 'files',
    'LISTER_ROOT_DOMAIN': '.kelliher.info',
    'LISTER_ACL_DB': str(pathlib.Path(TMP.name) / 'acl.db'),
    'LISTER_MEDIA': '1',
    'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture',
    'AWS_EC2_METADATA_DISABLED': 'true',
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lister as L

# netmon ingest/netmon_derive.py ACL, verbatim: the line format is a contract with it.
NETMON_ACL = re.compile(r"^(?P<level>INFO|WARNING|ERROR) (?P<decision>allowed|denied) (?P<action>\w+) (?P<key>.+) to (?P<user>\S+)$")
REV = '0123456789ab'


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


class DecisionLog(unittest.TestCase):
    def setUp(self):
        L.app.config['TESTING'] = True
        self.client = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')
        L.ACL.grant('user:alice', 'dad/', 'list', 'fixture')
        L._allowed_seen.clear()
        self.out = Lines()
        L.log.addHandler(self.out)
        self.addCleanup(L.log.removeHandler, self.out)
        level = L.log.level
        L.log.setLevel(logging.INFO)
        self.addCleanup(L.log.setLevel, level)

    def ask(self, uri, user='alice', method='GET', endpoint='/_files/authz', **extra):
        h = {'X-Forwarded-Uri': uri, 'X-Forwarded-Method': method}
        if user is not None:
            h['Remote-User'] = user
        h.update(extra)
        return self.client.get(endpoint, base_url='http://files.kelliher.info', headers=h)

    def only(self):
        self.assertEqual(len(self.out.lines), 1, self.out.lines)
        line = self.out.lines[0]
        self.out.lines.clear()
        m = NETMON_ACL.match(line)
        self.assertIsNotNone(m, line)
        self.assertNotIn('\n', line)
        self.assertNotIn('\r', line)
        return line, m

    def test_the_exact_lines_netmon_parses(self):
        self.assertEqual(self.ask('/dad/a.mp3').status_code, 200)
        self.assertEqual(self.only()[0], 'INFO allowed read files/dad/a.mp3 to alice')
        self.assertEqual(self.ask('/wfh/a.mp3').status_code, 403)
        self.assertEqual(self.only()[0], 'INFO denied read files/wfh/a.mp3 to alice')
        self.assertEqual(self.ask('/dad/a.mp3', user=None).status_code, 401)
        self.assertEqual(self.only()[0], 'INFO denied read files/dad/a.mp3 to -')

    def test_every_denial_on_both_authorizers_logs_one_line(self):
        for kw in [dict(uri='/dad/a.mp3', method='POST'), dict(uri='/_media/r/dad/a.mp3'),
                   dict(uri='/dad/../wfh/a.mp3'), dict(uri='/dad/a.mp3', Host='evil.example')]:
            self.assertGreaterEqual(self.ask(**kw).status_code, 400, kw)
            self.assertEqual(self.only()[1]['decision'], 'denied', kw)
        self.assertEqual(self.ask(f'/_media/r/wfh/a.mp3/{REV}/s_00000.m4s', endpoint='/_files/authz-media').status_code, 403)
        self.assertEqual(self.only()[0], 'INFO denied read files/wfh/a.mp3 to alice')
        self.assertEqual(self.ask('/_media/api/lib/wfh/', endpoint='/_files/authz-media').status_code, 403)
        self.assertEqual(self.only()[0], 'INFO denied list files/wfh/ to alice')
        self.assertEqual(self.ask(f'/_media/r/dad/a.mp3/{REV}/s_00000.m4s', endpoint='/_files/authz-media').status_code, 200)
        self.assertEqual(self.only()[0], 'INFO allowed read files/dad/a.mp3 to alice')

    def test_a_key_or_user_cannot_forge_a_line(self):
        L.ACL.grant('user:alice', 'wfh/', 'read', 'fixture')
        self.assertEqual(self.ask('/wfh/x%20to%20admin.mp3').status_code, 200)
        line, m = self.only()
        self.assertEqual(line, 'INFO allowed read files/wfh/x%20to%20admin.mp3 to alice')
        self.assertEqual(m['user'], 'alice')
        self.ask('/dad/x%0AINFO%20allowed%20read%20files/wfh/a%20to%20admin')
        line, m = self.only()
        self.assertEqual(m['decision'], 'denied')
        self.assertEqual(m['user'], 'alice')
        self.assertIn('%0AINFO%20allowed', line)
        self.ask('/wfh/a.mp3', user='bob to admin')
        line, m = self.only()
        self.assertEqual(m['user'], 'bob%20to%20admin')
        self.ask('/wfh/a.mp3', user='b\u00f6b\x7f')
        line, m = self.only()
        self.assertEqual(m['user'], 'b%C3%B6b%7F')

    def test_no_query_header_or_token_is_logged(self):
        self.ask('/dad/a.mp3?token=QUERY_SECRET&download=1', Authorization='Bearer TOKEN_SECRET',
                 Cookie='authelia_session=COOKIE_SECRET', Range='bytes=0-9')
        self.ask('/wfh/a.mp3?token=QUERY_SECRET', Authorization='Bearer TOKEN_SECRET')
        self.ask('/dad/a.mp3?token=QUERY_SECRET', user=None, Authorization='Bearer TOKEN_SECRET')
        text = '\n'.join(self.out.lines)
        self.assertEqual(len(self.out.lines), 3, self.out.lines)
        for secret in ('QUERY_SECRET', 'TOKEN_SECRET', 'COOKIE_SECRET', 'bytes=', 'download', '?'):
            self.assertNotIn(secret, text)

    def test_allowed_is_quiet_per_user_and_key_for_ten_minutes_but_denied_never_is(self):
        clock = [1000.0]
        with patch.object(L.time, 'monotonic', lambda: clock[0]):
            for _ in range(5):
                self.assertEqual(self.ask('/dad/a.mp3', Range='bytes=0-9').status_code, 200)
            self.assertEqual(len(self.out.lines), 1)
            self.ask('/dad/b.mp3')
            L.ACL.grant('user:carol', 'dad/', 'read', 'fixture')
            self.ask('/dad/a.mp3', user='carol')
            self.assertEqual(len(self.out.lines), 3)
            clock[0] += L.ALLOWED_QUIET_SECONDS - 1
            self.ask('/dad/a.mp3')
            self.assertEqual(len(self.out.lines), 3)
            clock[0] += 2
            self.ask('/dad/a.mp3')
            self.assertEqual(len(self.out.lines), 4)
            self.out.lines.clear()
            for _ in range(5):
                self.ask('/wfh/a.mp3')
            self.assertEqual(len(self.out.lines), 5)

    def test_the_quiet_memory_is_bounded(self):
        with patch.object(L, 'ALLOWED_SEEN_MAX', 3):
            for i in range(10):
                self.ask(f'/dad/{i}.mp3')
            self.assertLessEqual(len(L._allowed_seen), 3)
            self.assertEqual(len(self.out.lines), 10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
