#!/usr/bin/env python3
import hashlib
import http.client
import http.server
import importlib
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(tempfile.mkdtemp(prefix='files-media-'))
PROCESSES = []
GARAGE = shutil.which('garage')
CADDY = shutil.which('caddy')
CFG = ROOT / 'garage.toml'
IDENTITY = {'alice': ('alice', ''), 'bob': ('bob', ''), 'admin': ('admin', 'files-admin')}


def start(argv, marker):
    event = threading.Event()
    log = []
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env={**os.environ, 'RUST_LOG': 'info'})
    PROCESSES.append(process)
    process.lines = log
    def drain():
        for line in process.stdout:
            log.append(line.rstrip())
            if marker in line:
                event.set()
        event.set()
    threading.Thread(target=drain, daemon=True).start()
    if not event.wait(30) or process.poll() is not None:
        raise RuntimeError(f'{argv[0]} did not start: ' + '\n'.join(log[-12:]))
    return process


def garage(*args):
    result = subprocess.run([GARAGE, '-c', str(CFG), *args], capture_output=True,
                            text=True, timeout=20, check=True)
    return result.stdout


class Auth(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split('?', 1)[0] != '/api/authz/forward-auth':
            self.send_error(404)
            return
        user = self.headers.get('Cookie', '').removeprefix('user=')
        identity = IDENTITY.get(user)
        self.send_response(200 if identity else 401)
        if identity:
            self.send_header('Remote-User', identity[0])
            self.send_header('Remote-Groups', identity[1])
        self.end_headers()
    def log_message(self, *_):
        pass


class Stack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CFG.write_text(f'''metadata_dir = "{ROOT}/meta"
data_dir = "{ROOT}/data"
db_engine = "lmdb"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:28391"
rpc_public_addr = "127.0.0.1:28391"
rpc_secret = "{secrets.token_hex(32)}"
[s3_api]
s3_region = "test"
api_bind_addr = "127.0.0.1:28390"
root_domain = ".s3.kelliher.info"
[s3_web]
bind_addr = "127.0.0.1:28392"
root_domain = ".kelliher.info"
index = "index.html"
[admin]
api_bind_addr = "127.0.0.1:28393"
''')
        CFG.chmod(0o600)
        cls.garage = start([GARAGE, '-c', str(CFG), 'server'], '28392')
        node = garage('node', 'id', '-q').strip().split('@')[0]
        garage('layout', 'assign', '-z', 'test', '-c', '1G', node)
        garage('layout', 'apply', '--version', '1')
        garage('bucket', 'create', 'files')
        garage('bucket', 'website', '--allow', '-i', 'index.html', '-e', 'error.html', 'files')
        raw = garage('key', 'create', 'fixture')
        key = re.search(r'^Key ID:\s*(\S+)', raw, re.M).group(1)
        secret = re.search(r'^Secret key:\s*(\S+)', raw, re.M).group(1)
        garage('bucket', 'allow', '--read', '--write', '--owner', 'files', '--key', key)
        os.environ.update({
            'LISTER_TEMPLATE': '/dev/null', 'LISTER_ACL_DB': str(ROOT / 'acl.db'),
            'LISTER_BUCKETS': 'files', 'LISTER_ROOT_DOMAIN': '.kelliher.info',
            'LISTER_ENDPOINT': 'http://127.0.0.1:28390', 'LISTER_REGION': 'test',
            'AWS_ACCESS_KEY_ID': key, 'AWS_SECRET_ACCESS_KEY': secret,
            'AWS_EC2_METADATA_DISABLED': 'true',
        })
        sys.path.insert(0, os.environ['LISTER_SRC'])
        cls.L = importlib.import_module('lister')
        cls.s3 = cls.L.s3
        cls.data = bytes(range(256)) * 16384
        cls.objects = {
            'dad/movie.mp4': (cls.data, 'video/mp4'),
            'dad/audio.mp3': (cls.data[:16000], 'audio/mpeg'),
            'dad/page.html': (b'<script>PRIVATE_ACTIVE</script>', 'text/html'),
            'dad/image.svg': (b'<svg><script>PRIVATE_ACTIVE</script></svg>', 'image/svg+xml'),
            'dad/fake.bin': (b'<script>PRIVATE_ACTIVE</script>', 'application/octet-stream'),
            'wfh/private.mp4': (b'PRIVATE_WFH', 'video/mp4'),
            'dadtaxes/private.mp4': (b'PRIVATE_SIBLING', 'video/mp4'),
            'dad/folder/index.html': (b'PRIVATE_INDEX', 'text/html'),
            'error.html': (b'PRIVATE_ERROR', 'text/html'),
        }
        for name in ['a b', 'a+b', 'a#b', 'a?b', 'a;b', 'caf\u00e9', 'cafe\u0301', 'A', 'a', 'percent%file']:
            cls.objects[f'dad/{name}.mp3'] = (name.encode(), 'audio/mpeg')
        for key, (body, kind) in cls.objects.items():
            cls.s3.put_object(Bucket='files', Key=key, Body=body, ContentType=kind)
        cls.L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')
        cls.L.ACL.grant('user:alice', 'dad/', 'list', 'fixture')
        cls.mode = None
        @cls.L.app.after_request
        def fault(response):
            if cls.L.request.path == '/_files/authz' and cls.mode is not None:
                if cls.mode == 'timeout':
                    threading.Event().wait(4)
                    return response
                response = cls.L.Response('', status=200)
                if cls.mode != 'absent':
                    response.headers['X-Files-URI'] = cls.mode
            return response
        from waitress import create_server
        cls.lister = create_server(cls.L.app, host='127.0.0.1', port=28999, threads=4)
        cls.L.ADMISSION.watch(cls.lister)
        threading.Thread(target=cls.lister.run, daemon=True).start()
        cls.auth = http.server.ThreadingHTTPServer(('127.0.0.1', 28091), Auth)
        threading.Thread(target=cls.auth.serve_forever, daemon=True).start()
        cls.caddy = start([CADDY, 'run', '--config', os.environ['MEDIA_CADDYFILE'],
                           '--adapter', 'caddyfile'], 'serving initial configuration')

    @classmethod
    def tearDownClass(cls):
        cls.auth.shutdown()
        cls.lister.close()
        for process in reversed(PROCESSES):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        shutil.rmtree(ROOT)

    def setUp(self):
        self.L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')
        type(self).mode = None

    def get(self, path, user='alice', method='GET', headers=None, port=28780):
        h = {'Host': 'files.kelliher.info'}
        if user:
            h['Cookie'] = f'user={user}'
        h.update(headers or {})
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read()
        result = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
        c.close()
        return result

    def safe_headers(self, h):
        self.assertIn('no-store', h['cache-control'])
        self.assertEqual(h['x-content-type-options'], 'nosniff')
        self.assertIn('sandbox', h['content-security-policy'])
        self.assertIn("default-src 'none'", h['content-security-policy'])

    def denied(self, result):
        status, headers, body = result
        self.assertGreaterEqual(status, 400)
        for marker in [b'PRIVATE_WFH', b'PRIVATE_SIBLING', b'PRIVATE_INDEX', b'PRIVATE_ERROR']:
            self.assertNotIn(marker, body)
        self.safe_headers(headers)

    def test_01_real_range_and_head(self):
        status, h, body = self.get('/dad/movie.mp4', headers={'Range': 'bytes=6000-6999'})
        self.assertEqual(status, 206)
        self.assertEqual(h['content-range'], f'bytes 6000-6999/{len(self.data)}')
        self.assertEqual(body, self.data[6000:7000])
        self.assertEqual(h['content-disposition'], 'inline')
        self.safe_headers(h)
        status, h, body = self.get('/dad/movie.mp4', method='HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(int(h['content-length']), len(self.data))
        self.assertEqual(body, b'')
        self.safe_headers(h)
        self.denied(self.get('/wfh/private.mp4', method='HEAD'))

    def test_02_revocation_blocks_the_next_seek(self):
        self.assertEqual(self.get('/dad/movie.mp4', headers={'Range':'bytes=0-99'})[0],206)
        self.L.ACL.revoke('user:alice', 'dad/', 'read')
        self.denied(self.get('/dad/movie.mp4', headers={'Range':'bytes=6000-6999'}))

    def test_03_identity_and_sibling(self):
        for path in ['/dad/movie.mp4','/wfh/private.mp4','/dadtaxes/private.mp4']:
            self.denied(self.get(path,user='bob'))
        self.denied(self.get('/wfh/private.mp4'))
        self.denied(self.get('/dadtaxes/private.mp4'))
        self.denied(self.get('/dad/movie.mp4', user=None))
        self.denied(self.get('/wfh/private.mp4',user='bob',headers={
            'Remote-User':'admin','Remote-Groups':'files-admin',
            'X-Files-URI':'/wfh/private.mp4','X-Forwarded-Uri':'/wfh/private.mp4',
            'X-Forwarded-Method':'GET'}))
        self.denied(self.get('/dad/movie.mp4',headers={'Authorization':'Bearer anything'}))

    def test_04_special_keys_are_exact(self):
        for path, key in [
            ('/dad/a%20b.mp3','dad/a b.mp3'), ('/dad/a+b.mp3','dad/a+b.mp3'),
            ('/dad/a%23b.mp3','dad/a#b.mp3'), ('/dad/a%3Fb.mp3','dad/a?b.mp3'),
            ('/dad/a;b.mp3','dad/a;b.mp3'), ('/dad/caf%C3%A9.mp3','dad/caf\u00e9.mp3'),
            ('/dad/cafe%CC%81.mp3','dad/cafe\u0301.mp3'),
            ('/dad/A.mp3','dad/A.mp3'), ('/dad/a.mp3','dad/a.mp3'),
            ('/dad/percent%25file.mp3','dad/percent%file.mp3'),
        ]:
            status,h,body=self.get(path+'?ignored=1')
            if status != 200:
                print('FAILED KEY TRACE:', path, status, h, body, flush=True)
                print('CADDY TRACE:', '\n'.join(self.caddy.lines[-12:]), flush=True)
            self.assertEqual(status,200,path)
            self.assertEqual(body,self.objects[key][0],path)
            self.safe_headers(h)
        self.denied(self.get('/dad/percent%252e.mp3'))

    def test_05_traversal_and_aliases_are_refused(self):
        for path in ['/dad/../wfh/private.mp4','/dad/%2e%2e/wfh/private.mp4',
                     '/dad/%252e%252e/wfh/private.mp4','/dad//movie.mp4',
                     '/dad/./movie.mp4','/dad%2fmovie.mp4','/dad%5cmovie.mp4',
                     '/dad/movie.mp4.','/dad/movie.mp4%20','/dad/movie.mp4%00']:
            self.denied(self.get(path))

    def test_06_empty_or_malformed_echo_is_closed(self):
        for value in ['absent','', '/', '/wfh/private.mp4?x=y','//wfh/private.mp4',
                      'http://evil/x','/dad/a b.mp3','/dad/%GG.mp3',
                      '/dad/../wfh/private.mp4','/dad/%252e%252e/wfh/private.mp4']:
            type(self).mode=value
            self.denied(self.get('/wfh/private.mp4',user='bob'))
        type(self).mode=None

    def test_07_index_and_error_document_substitution(self):
        status,_,body=self.get('/dad/folder/',port=28392)
        self.assertEqual(status,200)
        self.assertEqual(body,b'PRIVATE_INDEX')
        status,_,body=self.get('/dad/missing.mp3',port=28392)
        self.assertEqual(status,404)
        self.assertEqual(body,b'PRIVATE_ERROR')
        self.denied(self.get('/dad/missing.mp3'))
        self.denied(self.get('/dad/movie.mp4', headers={'Range':'bytes=999999999-'}))
        self.denied(self.get('/dad/movie.mp4', headers={'x-amz-server-side-encryption-customer-algorithm':'AES256'}))
        self.denied(self.get('/dad/folder'))
        self.L.ACL.revoke('user:alice','dad/','read')
        self.L.ACL.grant('user:alice','dad/folder','read','fixture')
        self.denied(self.get('/dad/folder'))
        result=self.get('/dad/folder/')
        self.assertNotIn(b'PRIVATE_INDEX',result[2])

    def test_08_active_content_and_conditional_responses(self):
        for path in ['/dad/page.html','/dad/image.svg','/dad/fake.bin']:
            status,h,body=self.get(path)
            self.assertEqual(status,200)
            self.assertEqual(h['content-disposition'],'attachment')
            self.safe_headers(h)
        _,h,_=self.get('/dad/movie.mp4')
        status,h,body=self.get('/dad/movie.mp4',headers={'If-None-Match':h['etag']})
        self.assertEqual(status,304)
        self.assertEqual(body,b'')
        self.safe_headers(h)
        status,h,_=self.get('/dad/movie.mp4',headers={'Range':'bytes=999999999-'})
        self.assertEqual(status,416)
        self.safe_headers(h)
        self.denied(self.get('/dad/movie.mp4',method='POST'))

    def test_09_authz_failure_and_no_cache(self):
        self.assertEqual(self.get('/dad/movie.mp4')[0],200)
        self.denied(self.get('/dad/movie.mp4',user='bob'))
        type(self).mode='absent'
        self.denied(self.get('/dad/movie.mp4'))
        type(self).mode=None

    def test_10_negative_control_grant_removal_matters(self):
        result=self.get('/wfh/private.mp4')
        self.denied(result)
        self.L.ACL.grant('user:alice','wfh/private.mp4','read','fixture')
        result=self.get('/wfh/private.mp4')
        self.assertEqual(result[0],200)
        self.assertEqual(result[2],b'PRIVATE_WFH')
        with self.assertRaises(AssertionError):
            self.denied(result)
        self.L.ACL.revoke('user:alice','wfh/private.mp4','read')

    def test_11_authz_timeout_is_closed(self):
        type(self).mode='timeout'
        status,h,body=self.get('/dad/movie.mp4')
        self.assertEqual(status,502)
        self.assertEqual(body,b'')
        self.safe_headers(h)
        type(self).mode=None

    def test_12_native_416_preserves_total_size(self):
        garage('bucket','website','--allow','-i','index.html','files')
        try:
            status,h,body=self.get('/dad/movie.mp4',headers={'Range':'bytes=999999999-'})
            self.assertEqual(status,416)
            self.assertEqual(h['content-range'],f'bytes */{len(self.data)}')
            self.assertEqual(body,b'')
            self.safe_headers(h)
        finally:
            garage('bucket','website','--allow','-i','index.html','-e','error.html','files')

    def test_98_first_auth_transport_error_has_policy(self):
        self.auth.shutdown()
        self.auth.server_close()
        try:
            status,h,body=self.get('/dad/movie.mp4')
            self.assertEqual(status,502)
            self.safe_headers(h)
        finally:
            type(self).auth=http.server.ThreadingHTTPServer(('127.0.0.1',28091),Auth)
            threading.Thread(target=self.auth.serve_forever,daemon=True).start()

    def test_99_stopped_lister_is_closed(self):
        self.lister.task_dispatcher.shutdown(cancel_pending=True, timeout=1)
        for channel in list(self.lister._map.values()):
            channel.close()
        self.lister.close()
        status,h,body=self.get('/dad/movie.mp4')
        self.assertEqual(status,502)
        self.assertNotIn(self.data[:1024],body)
        self.safe_headers(h)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        for p in reversed(PROCESSES):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        shutil.rmtree(ROOT,ignore_errors=True)
