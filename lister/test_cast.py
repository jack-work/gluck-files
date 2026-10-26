#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import tempfile
import unittest

TMP = tempfile.TemporaryDirectory()
KEY = pathlib.Path(TMP.name) / 'cast-key'
KEY.write_bytes(b'k' * 48)
os.environ.update({
    'LISTER_TEMPLATE': '/dev/null', 'LISTER_BUCKETS': 'files',
    'LISTER_ROOT_DOMAIN': '.kelliher.info',
    'LISTER_ACL_DB': str(pathlib.Path(TMP.name) / 'acl.db'),
    'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture',
    'AWS_EC2_METADATA_DISABLED': 'true',
    'LISTER_CAST_ORIGIN': 'https://tv.kelliher.info',
    'LISTER_CAST_KEY_FILE': str(KEY),
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cast
import lister as L

ORIGIN = 'https://tv.kelliher.info'


class Tickets(unittest.TestCase):
    secret = b's' * 32

    def test_round_trip(self):
        t, exp = cast.mint(self.secret, 'alice', ['g2', 'g1'], 'films/a.mp4', 'dev1', now=1000)
        got = cast.verify(self.secret, t, 'dev1', now=1001)
        self.assertEqual((got['c'], got['g'], got['k'], got['d'], got['e']), ('alice', ['g1', 'g2'], 'films/a.mp4', 'dev1', exp))

    def test_lifetime_is_capped(self):
        _, exp = cast.mint(self.secret, 'a', [], 'k', 'd', lifetime=10 ** 9, now=0)
        self.assertEqual(exp, cast.CAP_SECONDS)

    def test_refusals(self):
        t, exp = cast.mint(self.secret, 'alice', [], 'films/a.mp4', 'dev1', now=1000)
        payload, sig = t.split('.')
        other, _ = cast.mint(self.secret, 'mallory', [], 'films/a.mp4', 'dev1', now=1000)
        for ticket, device, now in [
            (t, 'dev2', 1001),
            (t, 'dev1', exp + 1),
            (other.split('.')[0] + '.' + sig, 'dev1', 1001),
            (payload + '.' + sig[:-2] + 'AA', 'dev1', 1001),
            (payload, 'dev1', 1001),
            ('', 'dev1', 1001),
        ]:
            with self.assertRaises(cast.BadTicket):
                cast.verify(self.secret, ticket, device, now=now)
        with self.assertRaises(cast.BadTicket):
            cast.verify(b'x' * 32, t, 'dev1', now=1001)

    def test_a_short_key_is_refused_not_used(self):
        short = pathlib.Path(TMP.name) / 'short-key'
        short.write_bytes(b'k' * 8)
        saved = os.environ['LISTER_CAST_KEY_FILE']
        os.environ['LISTER_CAST_KEY_FILE'] = str(short)
        try:
            with self.assertRaises(ValueError):
                cast.load_key()
        finally:
            os.environ['LISTER_CAST_KEY_FILE'] = saved

    def test_device_ids_are_plain(self):
        for bad in ['', 'a/b', 'a b', 'x' * 65, None]:
            with self.assertRaises(cast.BadTicket):
                cast.mint(self.secret, 'a', [], 'k', bad)


class Endpoints(unittest.TestCase):
    def setUp(self):
        L.app.config['TESTING'] = True
        self.client = L.app.test_client()
        for g in L.ACL.list_grants():
            L.ACL.revoke(g['subject'], g['path'], g['perm'])
        L.ACL.grant('user:alice', 'films/', 'read', 'fixture')
        L.ACL.grant('group:family', 'home/', 'read', 'fixture')

    def issue(self, uri='/films/a.mp4', device='dev1', user='alice', groups='', origin=ORIGIN, site='same-site'):
        h = {'Origin': origin, 'Sec-Fetch-Site': site, 'Content-Type': 'text/plain;charset=UTF-8'}
        if user:
            h['Remote-User'] = user
        if groups:
            h['Remote-Groups'] = groups
        return self.client.post('/_files/cast-ticket', base_url='https://files.kelliher.info',
                                data=json.dumps({'uri': uri, 'device': device}), headers=h)

    def stream(self, ticket, device='dev1', method='GET'):
        return self.client.get('/_files/authz-cast', base_url='https://files.kelliher.info',
                               headers={'X-Cast-Ticket': ticket, 'X-Cast-Device': device, 'X-Forwarded-Method': method})

    def test_issue_and_stream(self):
        r = self.issue()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(r.headers['Access-Control-Allow-Credentials'], 'true')
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        ticket = r.get_json()['ticket']
        s = self.stream(ticket)
        self.assertEqual(s.status_code, 200)
        self.assertEqual(s.headers['X-Files-URI'], '/films/a.mp4')

    def test_issuance_needs_the_exact_origin_and_a_login(self):
        self.assertEqual(self.issue(origin='https://evil.example').status_code, 403)
        self.assertEqual(self.issue(origin='').status_code, 403)
        self.assertEqual(self.issue(site='cross-site').status_code, 403)
        self.assertEqual(self.issue(user='').status_code, 401)

    def test_issuance_checks_read(self):
        self.assertEqual(self.issue(uri='/secret/b.mp4').status_code, 403)
        self.assertEqual(self.issue(uri='/home/c.mp4').status_code, 403)
        self.assertEqual(self.issue(uri='/home/c.mp4', groups='family').status_code, 200)
        self.assertEqual(self.issue(uri='/films/../secret/b.mp4').status_code, 400)
        self.assertEqual(self.issue(device='a/b').status_code, 400)

    def test_a_removed_grant_stops_the_next_range(self):
        ticket = self.issue().get_json()['ticket']
        L.ACL.revoke('user:alice', 'films/', 'read')
        self.assertEqual(self.stream(ticket).status_code, 403)

    def test_group_membership_lags_by_design(self):
        ticket = self.issue(uri='/home/c.mp4', groups='family').get_json()['ticket']
        self.assertEqual(self.stream(ticket).status_code, 200)

    def test_another_device_or_method_is_refused(self):
        ticket = self.issue().get_json()['ticket']
        self.assertEqual(self.stream(ticket, device='dev2').status_code, 403)
        self.assertEqual(self.stream(ticket, method='PUT').status_code, 403)
        self.assertEqual(self.stream(ticket + 'x').status_code, 403)
        self.assertEqual(self.stream('').status_code, 403)

    def test_the_ordinary_authorizer_ignores_tickets(self):
        ticket = self.issue().get_json()['ticket']
        r = self.client.get('/_files/authz', base_url='https://files.kelliher.info',
                            headers={'X-Cast-Ticket': ticket, 'X-Cast-Device': 'dev1',
                                     'X-Forwarded-Uri': '/films/a.mp4', 'X-Forwarded-Method': 'GET'})
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
