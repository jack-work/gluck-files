import os
import re
import socket
import threading

import boto3

HOME = re.compile(r"users/([a-z][a-z0-9_-]{2,31})/")
PRESIGN_SECONDS = 60


class Unplaced(Exception):
    pass


def home_owner(key):
    m = HOME.match(key or "")
    return m.group(1) if m else None


def user_bucket(user):
    return "u-" + user.replace("_", ".")


def read_key_file(path):
    found = {}
    with open(path) as f:
        for line in f:
            name, sep, value = line.strip().partition("=")
            if sep and name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
                found[name] = value
    if len(found) != 2:
        raise Unplaced(f"key file {path} is incomplete")
    return found["AWS_ACCESS_KEY_ID"], found["AWS_SECRET_ACCESS_KEY"]


def ask_provisioner(path, user, timeout=190):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(f"ensure {user}\n".encode())
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 512:
            chunk = s.recv(512)
            if not chunk:
                break
            answer += chunk
    answer = answer.decode(errors="replace").strip()
    if answer != "ok":
        raise Unplaced(answer or "no answer from the provisioner")


class Places:
    def __init__(self, shared, shared_bucket, endpoint, region, config, keys_dir="", provision_socket=""):
        self.shared = shared
        self.shared_bucket = shared_bucket
        self.endpoint = endpoint
        self.region = region
        self.config = config
        self.keys_dir = keys_dir
        self.provision_socket = provision_socket
        self._clients = {}
        self._lock = threading.Lock()

    def at(self, bucket, key, ensure=False):
        user = home_owner(key) if bucket == self.shared_bucket and self.keys_dir else None
        if user is None:
            return self.shared, bucket, key
        return self._client(user, ensure), user_bucket(user), key[len(f"users/{user}/"):]

    def outer(self, bucket, key):
        user = home_owner(key) if bucket == self.shared_bucket and self.keys_dir else None
        return f"users/{user}/" if user else ""

    def _client(self, user, ensure):
        path = os.path.join(self.keys_dir, user)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            if not ensure:
                raise Unplaced(f"{user} has no bucket yet")
            ask_provisioner(self.provision_socket, user)
            st = os.stat(path)
        stamp = (st.st_ino, st.st_mtime_ns)
        with self._lock:
            held = self._clients.get(user)
            if held and held[0] == stamp:
                return held[1]
        key_id, secret = read_key_file(path)
        client = boto3.client("s3", endpoint_url=self.endpoint, region_name=self.region, config=self.config,
                              aws_access_key_id=key_id, aws_secret_access_key=secret)
        with self._lock:
            self._clients[user] = (stamp, client)
        return client

    def signed_get(self, bucket, key, method):
        client, where, inner = self.at(bucket, key)
        url = client.generate_presigned_url(
            "head_object" if method == "HEAD" else "get_object",
            Params={"Bucket": where, "Key": inner},
            ExpiresIn=PRESIGN_SECONDS,
        )
        prefix = self.endpoint.rstrip("/")
        if not url.startswith(prefix + "/"):
            raise Unplaced("presigned URL is not on the endpoint")
        return url[len(prefix):]
