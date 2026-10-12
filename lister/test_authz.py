#!/usr/bin/env python3
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock

TMP = tempfile.TemporaryDirectory()
os.environ.update({
    'LISTER_TEMPLATE': '/dev/null', 'LISTER_BUCKETS': 'files',
    'LISTER_ROOT_DOMAIN': '.kelliher.info',
    'LISTER_ACL_DB': str(pathlib.Path(TMP.name) / 'acl.db'),
    'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture',
    'AWS_EC2_METADATA_DISABLED': 'true',
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl import BadPath, canonical_object_uri
import lister as L


class Canonical(unittest.TestCase):
    def test_escaped_characters_name_the_same_key(self):
        for raw, key, echo in [
            ('/dad/a%20b.mp3', 'dad/a b.mp3', '/dad/a%20b.mp3'),
            ('/dad/a+b.mp3', 'dad/a+b.mp3', '/dad/a%2Bb.mp3'),
            ('/dad/a%23b.mp3', 'dad/a#b.mp3', '/dad/a%23b.mp3'),
            ('/dad/a%3fb.mp3', 'dad/a?b.mp3', '/dad/a%3Fb.mp3'),
            ('/dad/a;b.mp3', 'dad/a;b.mp3', '/dad/a%3Bb.mp3'),
            ('/dad/caf%C3%A9.mp3', 'dad/caf\u00e9.mp3', '/dad/caf%C3%A9.mp3'),
            ('/dad/cafe%CC%81.mp3', 'dad/cafe\u0301.mp3', '/dad/cafe%CC%81.mp3'),
            ('/dad/A.mp3?download=1', 'dad/A.mp3', '/dad/A.mp3'),
        ]:
            self.assertEqual(canonical_object_uri(raw), (key, echo), raw)
        self.assertNotEqual(canonical_object_uri('/dad/A.mp3')[0],
                            canonical_object_uri('/dad/a.mp3')[0])

    def test_ambiguous_forms_are_refused(self):
        for uri in [None, '', 'http://evil/x', '//dad/x', '/', '/dad/',
                    '/dad//x', '/dad/./x', '/dad/../secret/x',
                    '/dad/%2e%2e/secret/x', '/dad/%252e%252e/secret/x',
                    '/dad%2Fx', '/dad%5Cx', '/dad\\x', '/dad/x.',
                    '/dad/x%20', '/dad/%00x', '/dad/%0ax', '/dad/%ff',
                    '/dad/%GG', '/dad/x%25file', '/dad/x%2520file',
                    '/dad/x#fragment', '/dad/a b']:
            with self.assertRaises((BadPath, ValueError), msg=str(uri)):
                canonical_object_uri(uri)


class Authorisation(unittest.TestCase):
    def setUp(self):
        L.app.config['TESTING'] = True
        self.client = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')

    def ask(self, uri='/dad/a.mp3', user='alice', method='GET', **extra):
        h={'X-Forwarded-Uri':uri,'X-Forwarded-Method':method}
        if user is not None:h['Remote-User']=user
        h.update(extra)
        return self.client.get('/_files/authz', base_url='http://files.kelliher.info', headers=h)

    def test_get_head_and_ranges_use_the_same_decision(self):
        for method in ('GET','HEAD'):
            r=self.ask(method=method,Range='bytes=100-199')
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.headers['X-Files-URI'],'/dad/a.mp3')
            self.assertEqual(r.headers['Cache-Control'],'no-store')
            self.assertEqual(r.data,b'')

    def test_no_object_lookup_or_bytes(self):
        old=L.s3
        L.s3=Mock(side_effect=AssertionError('object lookup'))
        try:
            self.assertEqual(self.ask('/dad/nonexistent.mp3').status_code,200)
            L.s3.head_object.assert_not_called()
            L.s3.get_object.assert_not_called()
        finally:L.s3=old

    def test_denied_has_no_echo(self):
        for uri in ('/wfh/private.mp3','/dadtaxes/private.mp3','/dad/%2e%2e/wfh/private.mp3'):
            r=self.ask(uri)
            self.assertEqual(r.status_code,403,uri)
            self.assertNotIn('X-Files-URI',r.headers)
            self.assertEqual(r.headers['Cache-Control'],'no-store')

    def test_no_identity_is_closed(self):
        self.assertEqual(self.ask(user=None).status_code,401)

    def test_missing_method_and_non_reads_are_closed(self):
        for method in ('','POST','PUT','DELETE','OPTIONS'):
            self.assertEqual(self.ask(method=method).status_code,403)

    def test_revocation_applies_to_next_range(self):
        self.assertEqual(self.ask().status_code,200)
        L.ACL.revoke('user:alice','dad/','read')
        self.assertEqual(self.ask(Range='bytes=200-299').status_code,403)

    def test_canonical_echo_is_not_caller_input(self):
        r=self.ask('/dad/a+b.mp3',**{'X-Files-URI':'/wfh/private.mp3'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['X-Files-URI'],'/dad/a%2Bb.mp3')

    def test_list_grant_does_not_read_objects(self):
        L.ACL.revoke('user:alice','dad/','read')
        L.ACL.grant('user:alice','dad/','list','fixture')
        self.assertEqual(self.ask().status_code,403)

    def test_admin_is_only_a_grant(self):
        self.assertEqual(self.ask('/wfh/private.mp3',user='admin',**{'Remote-Groups':'files-admin'}).status_code,403)
        L.ACL.grant('group:files-admin','','read','fixture')
        self.assertEqual(self.ask('/wfh/private.mp3',user='admin',**{'Remote-Groups':'files-admin'}).status_code,200)

    def test_unknown_bucket_is_denied(self):
        r=self.client.get('/_files/authz',base_url='http://graveyard.kelliher.info',headers={'Remote-User':'alice','X-Forwarded-Uri':'/dad/a.mp3','X-Forwarded-Method':'GET'})
        self.assertEqual(r.status_code,403)


if __name__=='__main__':unittest.main(verbosity=2)
