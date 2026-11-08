#!/usr/bin/env python3
import json
import logging
import os
import pathlib
import sys
import tempfile
import unittest

TMP = tempfile.TemporaryDirectory()
PREFIXES = pathlib.Path(TMP.name) / 'prefixes.json'
PREFIXES.write_text(json.dumps({
    'media-device': ['books/', 'cs7641/media/', '/rooted/', 'noslash', 'a//b/', '', 42],
    'scoped': ['books/', 'cs7641/media/', 'cs7641/audio/', '30d/voice/', 'caf\u00e9 a/'],
    'aguatv-tv': None,
    'broken': 'books/',
}))
os.environ.update({
    'LISTER_TEMPLATE': '/dev/null', 'LISTER_BUCKETS': 'files',
    'LISTER_ROOT_DOMAIN': '.kelliher.info',
    'LISTER_ACL_DB': str(pathlib.Path(TMP.name) / 'acl.db'),
    'LISTER_MEDIA': '1',
    'LISTER_CLIENT_PREFIXES_FILE': str(PREFIXES),
    'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture',
    'AWS_EC2_METADATA_DISABLED': 'true',
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lister as L

REV = '0123456789ab'


class Load(unittest.TestCase):
    def test_bad_prefixes_are_dropped_and_good_ones_kept(self):
        self.assertEqual(L.CLIENT_PREFIXES['media-device'], ['books/', 'cs7641/media/'])
        self.assertIsNone(L.CLIENT_PREFIXES['aguatv-tv'])
        self.assertEqual(L.CLIENT_PREFIXES['broken'], [])

    def test_an_unreadable_file_refuses_every_client(self):
        bad = pathlib.Path(TMP.name) / 'bad.json'
        bad.write_text('{not json')
        self.assertEqual(L.load_client_prefixes(str(bad)), {})
        self.assertEqual(L.load_client_prefixes(str(pathlib.Path(TMP.name) / 'missing.json')), {})
        bad.write_text('["books/"]')
        self.assertEqual(L.load_client_prefixes(str(bad)), {})


class Gate(unittest.TestCase):
    def setUp(self):
        L.app.config['TESTING'] = True
        self.client = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('group:files-admin', '', 'read', 'fixture')
        L.ACL.grant('group:files-admin', '', 'list', 'fixture')
        logging.getLogger('gluck-files').setLevel(logging.CRITICAL)

    def ask(self, uri, client=None, endpoint='/_files/authz', user='gluck', method='GET'):
        h = {'X-Forwarded-Uri': uri, 'X-Forwarded-Method': method,
             'Remote-User': user, 'Remote-Groups': 'files-admin'}
        if client is not None:
            h['Remote-Client'] = client
        return self.client.get(endpoint, base_url='http://files.kelliher.info', headers=h).status_code

    def media(self, uri, client=None):
        return self.ask(uri, client, endpoint='/_files/authz-media')

    def lib(self, uri, client=None, user='gluck'):
        h = {'X-Forwarded-Uri': uri, 'X-Forwarded-Method': 'GET', 'Remote-User': user, 'Remote-Groups': 'files-admin'}
        if client is not None:
            h['Remote-Client'] = client
        r = self.client.get('/_files/authz-media', base_url='http://files.kelliher.info', headers=h)
        return r.status_code, r.headers.get('X-Files-Scope')

    def test_device_reads_inside_its_prefixes_only(self):
        self.assertEqual(self.ask('/books/catalog.json', 'media-device'), 200)
        self.assertEqual(self.ask('/books/a/b.epub', 'media-device'), 200)
        self.assertEqual(self.ask('/cs7641/media/x.mp4', 'media-device'), 200)
        for uri in ('/bookstuff/x', '/books', '/booksx/y', '/wfh/x.pdf', '/cs7641/notes/x.pdf',
                    '/cs7641/mediax/y.mp4', '/x'):
            self.assertEqual(self.ask(uri, 'media-device'), 403, uri)

    def test_item_and_rendition_follow_the_prefix(self):
        self.assertEqual(self.media('/_media/api/item/books/x.mp3', 'media-device'), 200)
        self.assertEqual(self.media('/_media/api/item/bookstuff/x.mp3', 'media-device'), 403)
        self.assertEqual(self.media('/_media/api/item/wfh/x.mp4', 'media-device'), 403)
        self.assertEqual(self.media(f'/_media/r/cs7641/media/x.mp4/{REV}/s_00000.m4s', 'media-device'), 200)
        self.assertEqual(self.media(f'/_media/r/wfh/x.mp4/{REV}/s_00000.m4s', 'media-device'), 403)

    def test_a_library_inside_a_prefix_is_scoped_to_itself(self):
        self.assertEqual(self.lib('/_media/api/lib/books/', 'scoped'), (200, 'books/'))
        self.assertEqual(self.lib('/_media/api/lib/books/sub/', 'scoped'), (200, 'books/sub/'))
        self.assertEqual(self.lib('/_media/api/lib/cs7641/media/', 'scoped'), (200, 'cs7641/media/'))

    def test_a_library_above_prefixes_is_scoped_to_the_prefixes_under_it(self):
        self.assertEqual(self.lib('/_media/api/lib/', 'scoped'),
                         (200, 'books/ cs7641/media/ cs7641/audio/ 30d/voice/ caf%C3%A9%20a/'))
        self.assertEqual(self.lib('/_media/api/lib/cs7641/', 'scoped'), (200, 'cs7641/media/ cs7641/audio/'))
        self.assertEqual(self.lib('/_media/api/lib/30d/', 'scoped'), (200, '30d/voice/'))
        self.assertEqual(self.lib('/_media/api/lib/', 'media-device'), (200, 'books/ cs7641/media/'))

    def test_a_library_beside_or_outside_every_prefix_is_refused_without_a_scope(self):
        for uri in ('/_media/api/lib/bookstuff/', '/_media/api/lib/wfh/', '/_media/api/lib/cs/',
                    '/_media/api/lib/cs7641/notes/', '/_media/api/lib/30d/cs7641/', '/_media/api/lib/book/'):
            self.assertEqual(self.lib(uri, 'scoped'), (403, None), uri)
        self.assertEqual(self.lib('/_media/api/lib/', 'kcal'), (403, None))
        self.assertEqual(self.lib('/_media/api/lib/', 'broken'), (403, None))

    def test_a_scoped_library_still_needs_the_acl(self):
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:gluck', 'books/', 'list', 'fixture')
        self.assertEqual(self.lib('/_media/api/lib/books/', 'scoped'), (200, 'books/'))
        self.assertEqual(self.lib('/_media/api/lib/', 'scoped'), (200, 'books/'))
        self.assertEqual(self.lib('/_media/api/lib/cs7641/', 'scoped'), (403, None))
        self.assertEqual(self.lib('/_media/api/lib/books/', 'scoped', user='mallory'), (403, None))
        self.assertEqual(self.lib('/_media/api/lib/', 'scoped', user='mallory'), (403, None))

    def test_the_browser_and_whole_bucket_clients_never_get_a_scope(self):
        for client in (None, 'aguatv-tv'):
            for uri in ('/_media/api/lib/', '/_media/api/lib/books/', '/_media/api/lib/wfh/'):
                self.assertEqual(self.lib(uri, client), (200, None), (client, uri))
        r = self.client.get('/_files/authz', base_url='http://files.kelliher.info',
                            headers={'X-Forwarded-Uri': '/books/x', 'X-Forwarded-Method': 'GET', 'Remote-User': 'gluck',
                                     'Remote-Groups': 'files-admin', 'Remote-Client': 'scoped'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('X-Files-Scope', r.headers)
        r = self.client.get('/_files/authz-media', base_url='http://files.kelliher.info',
                            headers={'X-Forwarded-Uri': '/_media/api/item/books/x.mp3', 'X-Forwarded-Method': 'GET',
                                     'Remote-User': 'gluck', 'Remote-Groups': 'files-admin', 'Remote-Client': 'scoped'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('X-Files-Scope', r.headers)

    def test_no_client_is_the_browser_and_is_unchanged(self):
        for uri in ('/bookstuff/x', '/wfh/x.pdf', '/books/catalog.json'):
            self.assertEqual(self.ask(uri), 200, uri)
        self.assertEqual(self.media('/_media/api/lib/'), 200)

    def test_null_is_the_whole_bucket_and_unknown_or_broken_clients_are_refused(self):
        self.assertEqual(self.ask('/wfh/x.pdf', 'aguatv-tv'), 200)
        self.assertEqual(self.lib('/_media/api/lib/', 'aguatv-tv'), (200, None))
        for client in ('kcal', 'broken', 'Media-device', 'media-device '):
            expected = 200 if client == 'media-device ' else 403
            self.assertEqual(self.ask('/books/x', client), expected, client)
            self.assertEqual(self.ask('/wfh/x.pdf', client), 403, client)

    def test_the_acl_still_applies_inside_a_prefix(self):
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:gluck', 'books/a/', 'read', 'fixture')
        self.assertEqual(self.ask('/books/a/x', 'media-device'), 200)
        self.assertEqual(self.ask('/books/b/x', 'media-device'), 403)


if __name__ == '__main__':
    unittest.main(verbosity=2)
