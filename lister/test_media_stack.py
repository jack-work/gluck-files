#!/usr/bin/env python3
import hashlib
import http.client
import http.server
import importlib
import os
import pathlib
import re
import secrets
import socket
import stat
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
SPACE_KEY = 'cs7641/quizzes/sl-quiz/SL Unit Quiz_ CS7641_ Machine Learning - Fall 2026.pdf'
WEB_SOCKET = ROOT / 'web' / 'sock'
LISTER_SOCKET = ROOT / 'lister.sock'
MEDIA_API_SOCKET = ROOT / 'media-api.sock'
IDENTITY = {'alice': ('alice', ''), 'bob': ('bob', ''), 'admin': ('admin', 'files-admin')}
TOKENS = {
    'tv-alice': ('alice', '', 'human', 'aguatv-tv'),
    'tv-bob': ('bob', '', 'human', 'aguatv-tv'),
    'kcal-alice': ('alice', '', 'human', 'kcal'),
    'tv-service': ('aguatv-tv', '', 'service', 'aguatv-tv'),
}


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


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=10)
        self.unix_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.unix_path)


def garage(*args):
    result = subprocess.run([GARAGE, '-c', str(CFG), *args], capture_output=True,
                            text=True, timeout=20, check=True)
    return result.stdout


class Auth(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split('?', 1)[0] != '/api/authz/forward-auth':
            self.send_error(404)
            return
        pairs = [c.strip().split('=', 1) for c in self.headers.get('Cookie', '').split(';') if '=' in c]
        user = dict(pairs).get('user', '')
        identity = IDENTITY.get(user)
        self.send_response(200 if identity else 401)
        if identity:
            self.send_header('Remote-User', identity[0])
            self.send_header('Remote-Groups', identity[1])
        self.end_headers()
    def log_message(self, *_):
        pass


class Verifier(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get('Authorization', '')
        identity = TOKENS.get(auth[len('Bearer '):]) if auth.startswith('Bearer ') else None
        if self.path.split('?', 1)[0] != '/verify/gluck-files' or not identity:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        for name, value in zip(('Remote-User', 'Remote-Groups', 'Remote-Principal', 'Remote-Client'), identity):
            self.send_header(name, value)
        self.end_headers()
    def log_message(self, *_):
        pass


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        import json
        length = int(self.headers.get('Content-Length') or 0)
        sent = self.rfile.read(length).decode() if length else ''
        body = json.dumps({'path': self.path, 'method': self.command, 'body': sent,
                           'headers': [[k, v] for k, v in self.headers.items()]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET
    do_HEAD = do_GET

    def log_message(self, *_):
        pass


class UnixHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)

    def get_request(self):
        request, _ = self.socket.accept()
        return request, ('unix', 0)


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
bind_addr = "{WEB_SOCKET}"
root_domain = ".kelliher.info"
index = "index.html"
[admin]
api_bind_addr = "127.0.0.1:28393"
''')
        CFG.chmod(0o600)
        WEB_SOCKET.parent.mkdir(mode=0o750)
        cls.garage = start([GARAGE, '-c', str(CFG), 'server'], 'Web server listening')
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
            'AWS_EC2_METADATA_DISABLED': 'true', 'LISTER_MEDIA': '1',
        })
        sys.path.insert(0, os.environ['LISTER_SRC'])
        cls.L = importlib.import_module('lister')
        cls.s3 = cls.L.s3
        cls.data = bytes(range(256)) * 16384
        cls.objects = {
            'dad/movie.mp4': (cls.data, 'video/mp4'),
            'dad/audio.mp3': (cls.data[:16000], 'audio/mpeg'),
            'dad/page.html': (b"<script>alert('PRIVATE_ACTIVE')</script>", 'text/html'),
            'dad/image.svg': (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert('PRIVATE_ACTIVE')</script></svg>", 'image/svg+xml'),
            'dad/fake.bin': (b'<script>PRIVATE_ACTIVE</script>', 'application/octet-stream'),
            'wfh/private.mp4': (b'PRIVATE_WFH', 'video/mp4'),
            'dadtaxes/private.mp4': (b'PRIVATE_SIBLING', 'video/mp4'),
            'dad/folder/index.html': (b'PRIVATE_INDEX', 'text/html'),
            'error.html': (b'PRIVATE_ERROR', 'text/html'),
            SPACE_KEY: (b'LIVE_SPACE_KEY_FIXTURE', 'application/pdf'),
        }
        for name in ['a b', 'a+b', 'a#b', 'a?b', 'a;b', 'caf\u00e9', 'cafe\u0301', 'A', 'a', 'percent%file']:
            cls.objects[f'dad/{name}.mp3'] = (name.encode(), 'audio/mpeg')
        for key, (body, kind) in cls.objects.items():
            cls.s3.put_object(Bucket='files', Key=key, Body=body, ContentType=kind)
        cls.L.ACL.grant('user:alice', 'dad/', 'read', 'fixture')
        cls.L.ACL.grant('user:alice', 'dad/', 'list', 'fixture')
        cls.mode = None
        cls.authz_seen = []
        @cls.L.app.before_request
        def record():
            if cls.L.request.path.startswith('/_files/authz'):
                cls.authz_seen.append(dict(cls.L.request.headers))
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
        cls.lister = create_server(cls.L.app, unix_socket=str(LISTER_SOCKET), unix_socket_perms='600', threads=4)
        cls.L.ADMISSION.watch(cls.lister)
        threading.Thread(target=cls.lister.run, daemon=True).start()
        cls.auth = http.server.ThreadingHTTPServer(('127.0.0.1', 28091), Auth)
        threading.Thread(target=cls.auth.serve_forever, daemon=True).start()
        cls.verifier = http.server.ThreadingHTTPServer(('127.0.0.1', 28092), Verifier)
        threading.Thread(target=cls.verifier.serve_forever, daemon=True).start()
        cls.echo = UnixHTTPServer(str(MEDIA_API_SOCKET), Echo)
        threading.Thread(target=cls.echo.serve_forever, daemon=True).start()
        caddyfile = ROOT / 'Caddyfile'
        caddyfile.write_text(pathlib.Path(os.environ['MEDIA_CADDYFILE']).read_text()
                             .replace('@MEDIAAPI@', f'unix/{MEDIA_API_SOCKET}')
                             .replace('@LISTER@', f'unix/{LISTER_SOCKET}')
                             .replace('@GARAGE@', f'unix/{WEB_SOCKET}'))
        cls.caddy = start([CADDY, 'run', '--config', str(caddyfile),
                           '--adapter', 'caddyfile'], 'serving initial configuration')

    @classmethod
    def tearDownClass(cls):
        cls.auth.shutdown()
        cls.verifier.shutdown()
        cls.echo.shutdown()
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
        self.L.ACL.grant('user:alice', SPACE_KEY, 'read', 'fixture')
        type(self).mode = None

    def get(self, path, user='alice', method='GET', headers=None, unix=None, port=28780, body=None):
        h = {'Host': 'files.kelliher.info'}
        if user:
            h['Cookie'] = f'user={user}'
        h.update(headers or {})
        c = UnixConnection(unix) if unix else http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        body = r.read()
        result = (r.status, {k.lower(): v for k, v in r.getheaders()}, body)
        c.close()
        return result

    def safe_headers(self, h, document=False):
        self.assertIn('no-store', h['cache-control'])
        self.assertEqual(h['x-content-type-options'], 'nosniff')
        if document:
            self.assertNotIn('sandbox',h['content-security-policy'])
            self.assertIn("script-src 'none'",h['content-security-policy'])
        else:
            self.assertIn('sandbox',h['content-security-policy'])
        self.assertIn("default-src 'none'", h['content-security-policy'])

    def denied(self, result, document=False):
        status, headers, body = result
        self.assertGreaterEqual(status, 400)
        for marker in [b'PRIVATE_WFH', b'PRIVATE_SIBLING', b'PRIVATE_INDEX', b'PRIVATE_ERROR']:
            self.assertNotIn(marker, body)
        self.safe_headers(headers, document=document)

    def test_01_real_range_and_head(self):
        status, h, body = self.get('/dad/movie.mp4', headers={'Range': 'bytes=6000-6999'})
        self.assertEqual(status, 206)
        self.assertEqual(h['content-range'], f'bytes 6000-6999/{len(self.data)}')
        self.assertEqual(body, self.data[6000:7000])
        self.assertNotIn('content-disposition', h)
        self.assertEqual(h['content-type'], 'video/mp4')
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
            ('/' + SPACE_KEY.replace(' ', '%20'), SPACE_KEY),
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
        status,_,body=self.get('/dad/folder/',unix=WEB_SOCKET)
        self.assertEqual(status,200)
        self.assertEqual(body,b'PRIVATE_INDEX')
        status,_,body=self.get('/dad/missing.mp3',unix=WEB_SOCKET)
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
        for path, kind in [('/dad/page.html','text/html'),('/dad/image.svg','image/svg+xml'),('/dad/fake.bin','application/octet-stream')]:
            status,h,body=self.get(path)
            self.assertEqual(status,200)
            self.assertNotIn('content-disposition',h)
            self.assertEqual(h['content-type'],kind)
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
        self.assertIn(status,(502,504))
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

    def test_13_parser_rejections_do_not_reach_objects(self):
        for request in [
            b'GET /dad/%ZZ.mp4 HTTP/1.1\r\nHost: files.kelliher.info\r\n\r\n',
            b'GET /dad/movie.mp4 HTTP/1.1\r\nHost: files.kelliher.info\r\nBad Header\r\n\r\n',
            b'GET /dad/movie.mp4 HTTP/1.1\r\nHost: files.kelliher.info\r\nX-Large: '+b'a'*(2*1024*1024)+b'\r\n\r\n',
        ]:
            with socket.create_connection(('127.0.0.1',28780),timeout=5) as connection:
                try:connection.sendall(request)
                except (BrokenPipeError,ConnectionResetError):pass
                chunks=[]
                while True:
                    try:chunk=connection.recv(65536)
                    except ConnectionResetError:break
                    if not chunk:break
                    chunks.append(chunk)
            response=b''.join(chunks)
            status=int(response.split(b' ',2)[1])
            self.assertIn(status,(400,431))
            header,_,body=response.partition(b'\r\n\r\n')
            self.assertIn(body.strip(),(b'400 Bad Request',b'431 Request Header Fields Too Large'))
            self.assertNotIn(b'/dad/',response)
            self.assertNotIn(b'Server:',header)
            for name in [b'Cache-Control:',b'Content-Security-Policy:',b'X-Content-Type-Options:']:
                self.assertNotIn(name,header)
            for marker in [b'PRIVATE_',self.data[:1024]]:
                self.assertNotIn(marker,response)
            print('Parser rejection',status,'fixed response bytes',len(response),flush=True)

    def test_14_large_body_on_non_read_is_not_forwarded(self):
        with socket.create_connection(('127.0.0.1',28780),timeout=5) as connection:
            connection.sendall(b'POST /dad/movie.mp4 HTTP/1.1\r\nHost: files.kelliher.info\r\nCookie: user=alice\r\nContent-Length: 1000000000\r\nConnection: close\r\n\r\n')
            response=connection.recv(65536)
            self.assertIn(b' 403 ',response.split(b'\r\n',1)[0])
            self.assertIn(b'private, no-store',response)
            self.assertNotIn(self.data[:1024],response)

    def test_15_viewer_is_trusted_html_not_bucket_html(self):
        from urllib.parse import quote
        hostile='dad/\"><script>alert(1)\u00e9.mp3'
        self.s3.put_object(Bucket='files',Key=hostile,Body=b'fixture',ContentType='audio/mpeg')
        uri='/_files/view?uri='+quote(quote('/'+hostile,safe='/'),safe='')
        status,h,body=self.get(uri)
        self.assertEqual(status,200)
        self.assertNotIn('sandbox',h['content-security-policy'])
        self.assertIn("script-src 'none'",h['content-security-policy'])
        self.assertIn(b'&lt;script&gt;',body)
        self.assertNotIn(b'<script>',body)
        self.assertIn(b'<audio controls',body)
        self.assertNotIn(b'X-Amz-',body)
        self.denied(self.get(uri,user='bob'),document=True)
        status,h,body=self.get('/_files/view?uri=%2Fdad%2Fpage.html')
        self.assertEqual(status,200)
        self.assertNotIn(b'<audio',body)
        self.assertNotIn(b'<video',body)
        self.assertIn(b'download>Download</a>',body)
        self.assertNotIn(b'PRIVATE_ACTIVE',body)
        self.denied(self.get('/_files/view?uri=%2Fdad%2F..%2Fwfh%2Fprivate.mp4'),document=True)

    def test_16_garage_web_socket_mode_controls_nothing_so_its_directory_must(self):
        self.assertEqual(stat.S_IMODE(WEB_SOCKET.stat().st_mode), 0o222)
        self.assertEqual(stat.S_IMODE(WEB_SOCKET.parent.stat().st_mode), 0o750)

    def test_17_a_page_load_of_a_video_lands_in_the_viewer(self):
        status,h,body=self.get('/dad/movie.mp4',headers={'Sec-Fetch-Dest':'document'})
        self.assertEqual(status,303)
        self.assertEqual(h['location'],'/_files/view?uri=%2Fdad%2Fmovie.mp4')
        self.assertNotIn(self.data[:64],body)
        self.safe_headers(h)
        status,h,body=self.get('/dad/movie.mp4',headers={'Sec-Fetch-Dest':'video','Range':'bytes=0-99'})
        self.assertEqual(status,206)
        self.assertEqual(body,self.data[:100])
        status,h,body=self.get('/dad/page.html',headers={'Sec-Fetch-Dest':'document'})
        self.assertEqual(status,200)
        self.assertEqual(h['content-type'],'text/html')
        self.assertNotIn('content-disposition',h)
        self.denied(self.get('/wfh/private.mp4',headers={'Sec-Fetch-Dest':'document'}))

    def echoed(self, path, cookie, extra=None):
        import json
        status, _, body = self.get(path, user=None, headers={'Cookie': cookie, **(extra or {})})
        self.assertEqual(status, 200, (path, body[:200]))
        seen = json.loads(body)
        headers = {}
        for k, v in seen['headers']:
            headers.setdefault(k.lower(), []).append(v)
        return seen['path'], headers

    def test_18_the_app_and_screen_routes_reach_the_backend_with_the_screen_cookie_alone(self):
        token = 'Abc_' + 'x' * 28 + '-9'
        forged = {'Authorization': 'Basic YWxpY2U6eA==', 'X-Files-URI': '/wfh/private.mp4', 'Remote-User': 'admin'}
        for path in ('/_media/', '/_media/app/home', '/_media/app/home.js', '/_media/api/me', '/_media/api/me/next', '/_media/api/screens', '/_media/api/screens/s1/cast'):
            seen, h = self.echoed(path, f'authelia_session=SESSION_SECRET; user=alice; aguatv_screen={token}; other=1', forged)
            self.assertEqual(seen, path)
            self.assertEqual(h.get('remote-user'), ['alice'], path)
            self.assertEqual(h.get('cookie'), [f'aguatv_screen={token}'], path)
            for name in ('authorization', 'x-files-uri'):
                self.assertNotIn(name, h, (path, name))
            self.assertNotIn('SESSION_SECRET', repr(h), path)

    def test_19_a_malformed_or_missing_screen_cookie_sends_no_cookie_at_all(self):
        for cookie in ('user=alice; authelia_session=SESSION_SECRET',
                       'user=alice; aguatv_screen=short',
                       'user=alice; aguatv_screen=' + 'x' * 30 + '$%',
                       'user=alice; aguatv_screen=' + 'x' * 65,
                       'user=alice; xaguatv_screen=' + 'x' * 32):
            _, h = self.echoed('/_media/api/screens', cookie)
            self.assertNotIn('cookie', h, cookie)

    def test_20_the_object_routes_still_need_the_lister(self):
        status, _, _ = self.get('/_media/api/item/wfh/private.mp4')
        self.assertNotEqual(status, 200)
        self.assertEqual(self.get('/_media/app/home', user=None)[0], 401)

    def bearer(self, path, token='tv-alice', method='GET', headers=None, port=28780, body=None):
        return self.get(path, user=None, method=method, port=port, body=body,
                        headers={'Authorization': f'Bearer {token}', **(headers or {})})

    def test_21_the_tv_client_reads_objects_renditions_and_the_app_with_its_token(self):
        import json
        self.authz_seen.clear()
        status, h, body = self.bearer('/dad/movie.mp4', headers={'Range': 'bytes=6000-6999'})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[6000:7000])
        self.safe_headers(h)
        self.assertEqual(self.bearer('/dad/movie.mp4', method='HEAD')[0], 200)
        self.assertTrue(self.authz_seen)
        for seen in self.authz_seen:
            self.assertNotIn('Authorization', seen)
            self.assertEqual(seen.get('Remote-User'), 'alice')
        for path in ('/_media/app/home', '/_media/api/me', '/_media/api/me/next'):
            status, _, body = self.bearer(path)
            self.assertEqual(status, 200, path)
            seen = {k.lower(): v for k, v in json.loads(body)['headers']}
            self.assertEqual(seen.get('remote-user'), 'alice', path)
            self.assertEqual(seen.get('remote-client'), 'aguatv-tv', path)
            self.assertNotIn('authorization', seen, path)
        for path in ('/_media/api/me', '/_media/api/me/state'):
            status, _, body = self.bearer(path, method='POST', body=b'{"state":"idle"}',
                                          headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200, path)
            echoed = json.loads(body)
            self.assertEqual((echoed['method'], echoed['body']), ('POST', '{"state":"idle"}'), path)
        self.denied(self.bearer('/wfh/private.mp4'))
        self.denied(self.bearer('/dad/movie.mp4', token='tv-bob'))
        for path in ('/_media/api/item/dad/movie.mp4', '/_media/api/lib/dad/'):
            for method in ('GET', 'HEAD'):
                status, _, body = self.bearer(path, method=method)
                self.assertEqual(status, 200, (method, path))
            seen = {k.lower(): v for k, v in json.loads(self.bearer(path)[2])['headers']}
            self.assertEqual(seen.get('remote-user'), 'alice', path)
            self.assertNotIn('authorization', seen, path)
            self.assertNotEqual(self.bearer(path.replace('/dad/', '/wfh/'))[0], 200, path)
            self.assertNotEqual(self.bearer(path, token='tv-bob')[0], 200, path)

    def test_22_a_token_reaches_nothing_else(self):
        for method, path in [('POST', '/dad/movie.mp4'), ('PUT', '/dad/movie.mp4'), ('GET', '/dad/'),
                             ('GET', '/'), ('GET', '/_files/view?uri=%2Fdad%2Fmovie.mp4'),
                             ('GET', '/_files/authz'), ('GET', '/_media/api/screens'),
                             ('POST', '/_media/api/screens/s1/cast'), ('POST', '/_media/api/item/dad/movie.mp4'),
                             ('POST', '/_media/api/lib/dad/'), ('PUT', '/_media/api/me/state'),
                             ('POST', '/_media/api/me/next'), ('POST', '/_media/app/home'),
                             ('DELETE', '/_media/api/me')]:
            status, h, body = self.bearer(path, method=method)
            self.assertEqual(status, 403, (method, path))
            self.assertNotIn(b'PRIVATE_', body)
            self.assertNotIn(self.data[:64], body)
        for token in ('kcal-alice', 'tv-service'):
            for method, path in [('GET', '/dad/movie.mp4'), ('GET', '/_media/app/home'), ('POST', '/_media/api/me/state')]:
                self.assertEqual(self.bearer(path, token=token, method=method)[0], 403, (token, path))
        self.denied(self.bearer('/dad/movie.mp4', token='forged'))
        forged = {'Remote-Principal': 'human', 'Remote-Client': 'aguatv-tv', 'Remote-User': 'alice'}
        self.denied(self.bearer('/dad/movie.mp4', token='forged', headers=forged))
        self.assertEqual(self.get('/_media/api/me/state', user='alice', method='POST',
                                  headers={'Authorization': 'Bearer kcal-alice', **forged})[0], 403)

    def test_23_without_bearer_clients_a_token_is_refused_even_with_a_session(self):
        for method, path in [('GET', '/dad/movie.mp4'), ('GET', '/_media/app/home'), ('POST', '/_media/api/me/state')]:
            status, h, body = self.get(path, method=method, port=28781,
                                       headers={'Authorization': 'Bearer tv-alice'})
            self.assertEqual(status, 403, path)
            self.assertNotIn(self.data[:64], body)
        self.assertEqual(self.get('/dad/movie.mp4', port=28781, headers={'Range': 'bytes=0-9'})[0], 206)

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
