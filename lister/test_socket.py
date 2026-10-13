#!/usr/bin/env python3
"""The shipped gateway over an inherited unix socket.

  belief     Remote-User on the socket is believed: 404 for an ungranted
             object, where no identity is 401. The positive control.
  underscore Remote_User and remote_user are not believed: still 401
  no tcp     LISTER_PORT is set but nothing listens on it
  refuse     LISTER_LISTEN=socket with no inherited socket exits nonzero
"""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.environ["LISTER_SRC"]
ACTIVATE = os.environ["SOCKET_ACTIVATE"]
TMP = tempfile.mkdtemp()
SOCK = os.path.join(TMP, "lister.sock")
PORT = 29099
HOST = "files.kelliher.info"
OBJECT = "/wfh/Rental_Agreement.pdf"
PASSED = ("LISTER_LISTEN", "LISTER_PORT", "LISTER_TEMPLATE", "LISTER_BUCKETS", "LISTER_ROOT_DOMAIN",
          "LISTER_ACL_DB", "LISTER_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
          "AWS_EC2_METADATA_DISABLED", "PYTHONPATH")


def env():
    tpl = os.path.join(TMP, "tpl.html")
    with open(tpl, "w") as fh:
        fh.write("<h1>{{ heading }}</h1>")
    return {
        **os.environ,
        "LISTER_LISTEN": "socket",
        "LISTER_PORT": str(PORT),
        "LISTER_TEMPLATE": tpl,
        "LISTER_BUCKETS": "files",
        "LISTER_ROOT_DOMAIN": ".kelliher.info",
        "LISTER_ACL_DB": os.path.join(TMP, "acl.db"),
        "LISTER_ENDPOINT": "http://127.0.0.1:1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_EC2_METADATA_DISABLED": "true",
        "PYTHONPATH": SRC,
    }


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(SOCK)


def fetch(headers):
    c = UnixConnection(HOST)
    c.putrequest("GET", OBJECT, skip_host=True)
    c.putheader("Host", HOST)
    for k, v in headers:
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


class Socket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        forward = [f"--setenv={k}" for k in PASSED]
        cls.proc = subprocess.Popen(
            [ACTIVATE, "-l", SOCK, *forward, sys.executable, os.path.join(SRC, "lister.py")],
            env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                fetch([])
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("the gateway never answered on the socket")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def test_no_identity_is_401(self):
        self.assertEqual(fetch([]), 401)

    def test_positive_control_hyphen_is_believed(self):
        self.assertEqual(fetch([("Remote-User", "forged-probe")]), 404)

    def test_underscore_forms_are_not_believed(self):
        for name in ("Remote_User", "remote_user"):
            self.assertEqual(fetch([(name, "forged-probe")]), 401, name)

    def test_no_tcp_listener(self):
        s = socket.socket()
        s.settimeout(1)
        with self.assertRaises(OSError):
            s.connect(("127.0.0.1", PORT))
        s.close()


class Refuse(unittest.TestCase):
    def test_no_inherited_socket_means_no_start(self):
        r = subprocess.run([sys.executable, os.path.join(SRC, "lister.py")], env=env(),
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to fall back to TCP", r.stderr + r.stdout)
        s = socket.socket()
        s.settimeout(1)
        with self.assertRaises(OSError):
            s.connect(("127.0.0.1", PORT))
        s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
