#!/usr/bin/env python3
import grp
import json
import logging
import os
import pwd
import signal
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile

USER = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
GARAGE = os.environ["PROVISION_GARAGE"].split()
AWS = os.environ["PROVISION_AWS"]
S3 = os.environ["PROVISION_S3"]
REGION = os.environ["PROVISION_REGION"]
ACL_DB = os.environ["PROVISION_ACL_DB"]
KEYS = os.environ["PROVISION_KEYS_DIR"]
LIFECYCLE = os.environ["PROVISION_LIFECYCLE"]
PEER = pwd.getpwnam(os.environ["PROVISION_PEER_USER"])

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
log = logging.getLogger("provision")


class Refused(Exception):
    pass


def garage(*args):
    out = subprocess.run(GARAGE + list(args), capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"garage {args[0]} {args[1] if len(args) > 1 else ''} failed")
    return out.stdout


def bucket_name(user):
    return "u-" + user.replace("_", ".")


def has_grant(user):
    prefix = f"users/{user}/"
    db = sqlite3.connect(f"file:{ACL_DB}?mode=ro", uri=True, timeout=5)
    try:
        row = db.execute(
            "SELECT 1 FROM grants WHERE subject = ? AND substr(path, 1, ?) = ? LIMIT 1",
            (f"user:{user}", len(prefix), prefix),
        ).fetchone()
    finally:
        db.close()
    return row is not None


def listed(text, name):
    return any(name in line.split() for line in text.splitlines())


def authorized_keys(bucket):
    info = garage("bucket", "info", bucket)
    keys, inside = [], False
    for line in info.splitlines():
        if line.strip().startswith("Authorized keys"):
            inside = True
            continue
        if inside:
            parts = line.split()
            if len(parts) >= 3 and parts[1].startswith("GK"):
                keys.append(parts[2])
            elif line.strip() and not line.startswith(" "):
                inside = False
    return keys


def foreign_buckets(name):
    info = garage("key", "info", name)
    out, inside = [], False
    for line in info.splitlines():
        if line.strip().startswith("Authorized buckets"):
            inside = True
            continue
        if inside:
            if not line.strip():
                continue
            if not line.startswith(" "):
                inside = False
                continue
            if name not in line.split():
                out.append(line.strip())
    return out


def key_secret(name):
    info = garage("key", "info", "--show-secret", name)
    kid = re.search(r"^Key ID: *(\S+)", info, re.M)
    sec = re.search(r"^Secret key: *(\S+)", info, re.M)
    if not kid or not sec:
        raise RuntimeError("garage key info had no id or secret")
    return kid.group(1), sec.group(1)


def write_key(user, kid, sec):
    path = os.path.join(KEYS, user)
    fd, tmp = tempfile.mkstemp(dir=KEYS, prefix=f".{user}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"AWS_ACCESS_KEY_ID={kid}\nAWS_SECRET_ACCESS_KEY={sec}\n")
        os.chown(tmp, PEER.pw_uid, PEER.pw_gid)
        os.chmod(tmp, 0o400)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return path


def lifecycle(bucket, kid, sec):
    env = dict(os.environ, AWS_ACCESS_KEY_ID=kid, AWS_SECRET_ACCESS_KEY=sec, AWS_DEFAULT_REGION=REGION,
               AWS_EC2_METADATA_DISABLED="true", AWS_REQUEST_CHECKSUM_CALCULATION="WHEN_REQUIRED")
    out = subprocess.run([AWS, "--endpoint-url", S3, "s3api", "put-bucket-lifecycle-configuration",
                          "--bucket", bucket, "--lifecycle-configuration", f"file://{LIFECYCLE}"],
                         env=env, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError("lifecycle not applied")


def ensure(user):
    if not USER.fullmatch(user):
        raise Refused("bad name")
    if not has_grant(user):
        raise Refused("no grant on users/<name>/")
    bucket = bucket_name(user)
    created_bucket = created_key = False
    if not listed(garage("bucket", "list"), bucket):
        garage("bucket", "create", bucket)
        created_bucket = True
    else:
        foreign = [k for k in authorized_keys(bucket) if k != bucket]
        if foreign:
            raise Refused(f"bucket {bucket} has keys this provisioner did not make")
    if not listed(garage("key", "list"), bucket):
        garage("key", "create", bucket)
        created_key = True
    elif foreign_buckets(bucket):
        raise Refused(f"key {bucket} reaches other buckets")
    garage("bucket", "website", "--deny", bucket)
    garage("bucket", "allow", "--read", "--write", "--owner", bucket, "--key", bucket)
    kid, sec = key_secret(bucket)
    try:
        lifecycle(bucket, kid, sec)
    finally:
        garage("bucket", "deny", "--owner", bucket, "--key", bucket)
    path = write_key(user, kid, sec)
    if created_bucket or created_key:
        log.info("provisioned user=%s bucket=%s key=%s bucket_new=%s key_new=%s", user, bucket, bucket, created_bucket, created_key)
    return path


def peer_ok(conn):
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", raw)
    return uid == PEER.pw_uid


class Deadline(Exception):
    pass


def deadline(signum, frame):
    raise Deadline()


def serve(conn):
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(180)
    try:
        answer(conn)
    except Deadline:
        log.error("ensure ran past its 180 s deadline")
        try:
            conn.sendall(b"refused deadline\n")
        except OSError:
            pass
    finally:
        signal.alarm(0)


def answer(conn):
    conn.settimeout(120)
    if not peer_ok(conn):
        conn.sendall(b"refused peer\n")
        return
    data = b""
    while not data.endswith(b"\n") and len(data) < 128:
        chunk = conn.recv(128)
        if not chunk:
            break
        data += chunk
    words = data.decode("ascii", "replace").strip().split(" ")
    if len(words) != 2 or words[0] != "ensure":
        conn.sendall(b"refused verb\n")
        return
    try:
        ensure(words[1])
        conn.sendall(b"ok\n")
    except Refused as e:
        log.info("refused user=%s why=%s", words[1][:40], e)
        conn.sendall(f"refused {e}\n".encode())
    except Exception as e:
        log.error("failed user=%s err=%s", words[1][:40], e)
        conn.sendall(b"refused internal\n")


def main():
    if os.environ.get("LISTEN_FDS") != "1":
        sys.exit("needs one socket from systemd")
    srv = socket.socket(fileno=3)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                serve(conn)
            except OSError:
                pass


if __name__ == "__main__":
    main()
