#!/usr/bin/env python3
import os
import pathlib
import sys
import tempfile
import unittest

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
from acl import BadPath, media_request
import lister as L

REV = '0123456789ab'


class Parse(unittest.TestCase):
    def test_a_rendition_is_governed_by_its_source(self):
        self.assertEqual(
            media_request(f'/_media/r/dad/a%20b.mp3/{REV}/128_00001.m4s', 'GET'),
            ('read', 'dad/a b.mp3', f'/dad/a%20b.mp3/{REV}/128_00001.m4s'))
        self.assertEqual(
            media_request(f'/_media/r/dad/x.mp3/{REV}/master.m3u8?t=1', 'HEAD'),
            ('read', 'dad/x.mp3', f'/dad/x.mp3/{REV}/master.m3u8'))

    def test_item_and_library(self):
        self.assertEqual(media_request('/_media/api/item/dad/a+b.mp3', 'PUT'),
                         ('read', 'dad/a+b.mp3', '/dad/a%2Bb.mp3'))
        self.assertEqual(media_request('/_media/api/lib/dad/', 'GET'), ('list', 'dad/', '/dad/'))
        self.assertEqual(media_request('/_media/api/lib/', 'GET'), ('list', '', '/'))

    def test_malformed_media_requests_are_refused(self):
        for uri, method in [
            (f'/_media/r/dad/x.mp3/{REV}/master.m3u8', 'PUT'),
            (f'/_media/r/{REV}/master.m3u8', 'GET'),
            ('/_media/r/dad/x.mp3/0123456789AB/master.m3u8', 'GET'),
            ('/_media/r/dad/x.mp3/0123456789a/master.m3u8', 'GET'),
            (f'/_media/r/dad/x.mp3/{REV}/master.html', 'GET'),
            (f'/_media/r/dad/x.mp3/{REV}/../../wfh/x.m4s', 'GET'),
            (f'/_media/r/dad/%2e%2e/wfh/x.mp3/{REV}/master.m3u8', 'GET'),
            (f'/_media/r/dad/x.mp3/{REV}/sub/master.m3u8', 'GET'),
            ('/_media/api/item/dad/', 'GET'),
            ('/_media/api/item/dad/x.mp3', 'DELETE'),
            ('/_media/api/item/dad/x.mp3', 'POST'),
            ('/_media/api/lib/dad', 'GET'),
            ('/_media/api/lib/dad/', 'PUT'),
            ('/_media/api/lib//dad/', 'GET'),
            ('/_media/%72/dad/x.mp3/0123456789ab/master.m3u8', 'GET'),
            ('/_media/play/dad/x.mp3', 'GET'),
            ('/_media/other', 'GET'),
        ]:
            with self.assertRaises((BadPath, ValueError), msg=f'{method} {uri}'):
                media_request(uri, method)


class Authorisation(unittest.TestCase):
    def setUp(self):
        L.app.config['TESTING'] = True
        self.client = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')

    def ask(self, uri, user='alice', method='GET', endpoint='/_files/authz-media', **extra):
        h = {'X-Forwarded-Uri': uri, 'X-Forwarded-Method': method}
        if user is not None:
            h['Remote-User'] = user
        h.update(extra)
        return self.client.get(endpoint, base_url='http://files.kelliher.info', headers=h)

    def test_rendition_follows_the_source_grant(self):
        r = self.ask(f'/_media/r/dad/a.mp3/{REV}/s_00000.m4s')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['X-Files-URI'], f'/dad/a.mp3/{REV}/s_00000.m4s')
        self.assertEqual(self.ask(f'/_media/r/wfh/a.mp3/{REV}/s_00000.m4s').status_code, 403)
        self.assertEqual(self.ask(f'/_media/r/dadtaxes/a.mp3/{REV}/s_00000.m4s').status_code, 403)

    def test_revoking_the_source_revokes_the_next_segment(self):
        self.assertEqual(self.ask(f'/_media/r/dad/a.mp3/{REV}/s_00001.m4s').status_code, 200)
        L.ACL.revoke('user:alice', 'dad/', 'read')
        r = self.ask(f'/_media/r/dad/a.mp3/{REV}/s_00002.m4s')
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('X-Files-URI', r.headers)

    def test_position_writes_need_read(self):
        self.assertEqual(self.ask('/_media/api/item/dad/a.mp3', method='PUT').status_code, 200)
        self.assertEqual(self.ask('/_media/api/item/wfh/a.mp3', method='PUT').status_code, 403)

    def test_library_needs_list_not_read(self):
        self.assertEqual(self.ask('/_media/api/lib/dad/').status_code, 403)
        L.ACL.grant('user:alice', 'dad/', 'list', 'fixture')
        r = self.ask('/_media/api/lib/dad/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['X-Files-URI'], '/dad/')
        self.assertEqual(self.ask('/_media/api/lib/').status_code, 403)

    def test_caller_echo_is_ignored(self):
        r = self.ask('/_media/api/item/dad/a.mp3', **{'X-Files-URI': '/wfh/secret.mp3'})
        self.assertEqual(r.headers['X-Files-URI'], '/dad/a.mp3')

    def test_no_identity_is_closed(self):
        self.assertEqual(self.ask('/_media/api/item/dad/a.mp3', user=None).status_code, 401)

    def test_each_authorizer_answers_only_its_own_routes(self):
        L.ACL.grant('user:alice', '', 'read', 'fixture')
        rendition = f'/_media/r/dad/a.mp3/{REV}/s_00000.m4s'
        r = self.ask(rendition, endpoint='/_files/authz')
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('X-Files-URI', r.headers)
        r = self.ask('/dad/a.mp3')
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('X-Files-URI', r.headers)
        self.assertEqual(self.ask('/dad/a.mp3', endpoint='/_files/authz').status_code, 200)

    def test_audio_page_loads_go_to_the_player(self):
        r = self.ask('/dad/a%20b.mp3', endpoint='/_files/authz', **{'Sec-Fetch-Dest': 'document'})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['Location'], '/_media/play/dad/a%20b.mp3')
        r = self.ask('/dad/a.mov', endpoint='/_files/authz', **{'Sec-Fetch-Dest': 'document'})
        self.assertEqual(r.headers['Location'], '/_files/view?uri=%2Fdad%2Fa.mov')


if __name__ == '__main__':
    unittest.main(verbosity=2)
