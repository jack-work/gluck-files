#!/usr/bin/env python3
import argparse
import errno
import ipaddress
import os
import platform
import resource
import socket
import sys
import time
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thumbs  # noqa: E402
from place import Places, Unplaced  # noqa: E402
from save import UNDATED, day_of  # noqa: E402

CPU_SENSORS = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "x86_pkg_temp", "cpu-thermal")
RESULTS = ("made", "current", "marked", "failed", "stale", "unsupported", "unavailable", "empty", "gone")
PAUSE_SECONDS = 15
PAUSE_LIMIT_SECONDS = 1800


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def forbid_egress():
    real = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not loopback(str(address[0])):
            raise OSError(errno.ENETUNREACH, f"thumbsctl does not connect off the box: {address[0]}")
        return real(self, address)

    socket.socket.connect = guarded


def settle_down():
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    number = thumbs.IOPRIO_NUMBERS.get(platform.machine())
    if number is not None:
        import ctypes

        ctypes.CDLL(None, use_errno=True).syscall(number, 1, 0, 3 << 13)


def cpu_temp_c(root="/sys"):
    readings = []
    hwmon = os.path.join(root, "class/hwmon")
    for chip in sorted(os.listdir(hwmon)) if os.path.isdir(hwmon) else []:
        base = os.path.join(hwmon, chip)
        try:
            name = open(os.path.join(base, "name")).read().strip()
        except OSError:
            continue
        if name not in CPU_SENSORS:
            continue
        for entry in os.listdir(base):
            if entry.startswith("temp") and entry.endswith("_input"):
                try:
                    readings.append(int(open(os.path.join(base, entry)).read()) / 1000)
                except (OSError, ValueError):
                    pass
    zones = os.path.join(root, "class/thermal")
    for zone in sorted(os.listdir(zones)) if os.path.isdir(zones) else []:
        base = os.path.join(zones, zone)
        try:
            kind = open(os.path.join(base, "type")).read().strip()
            if kind in CPU_SENSORS:
                readings.append(int(open(os.path.join(base, "temp")).read()) / 1000)
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


def cpu_seconds():
    mine, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return mine.ru_utime + mine.ru_stime + kids.ru_utime + kids.ru_stime


class Pace:
    def __init__(self, cpu_percent, pause_above_c, sleep=time.sleep, temp=cpu_temp_c):
        self.share = max(1, min(100, cpu_percent)) / 100
        self.limit = pause_above_c
        self.sleep = sleep
        self.temp = temp
        self.hottest = None
        self.paused = 0.0

    def after(self, cpu_used):
        if self.share < 1 and cpu_used > 0:
            self.sleep(cpu_used * (1 / self.share - 1))

    def cool(self):
        waited = 0
        while True:
            now = self.temp()
            if now is not None and (self.hottest is None or now > self.hottest):
                self.hottest = now
            if self.limit is None or now is None or now <= self.limit or waited >= PAUSE_LIMIT_SECONDS:
                return
            self.sleep(PAUSE_SECONDS)
            waited += PAUSE_SECONDS
            self.paused += PAUSE_SECONDS


def newest_first(keys):
    ordered = sorted(keys, key=lambda k: k[0].rsplit("/", 1)[-1], reverse=True)
    return sorted(ordered, key=lambda k: day_of(k[0].rsplit("/", 1)[-1], None) == UNDATED)


def sources(client, bucket, inner):
    found, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": inner, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if thumbs.wanted(obj["Key"]):
                found.append((obj["Key"], thumbs.bare(obj.get("ETag")), obj.get("Size", 0)))
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            return found


def backfill(places, bucket, users, args, pace, out, timings=None, clock=time.monotonic):
    counts = {name: 0 for name in RESULTS}
    started, cpu_started, tried, total = clock(), cpu_seconds(), 0, 0
    for user in users:
        try:
            client, where, inner = places.at(bucket, f"users/{user}/{args.prefix}")
        except Unplaced as e:
            out(f"thumbsctl: {user}: {e}")
            continue
        todo = newest_first(sources(client, where, inner))
        total += len(todo)
        out(f"thumbsctl: {user}: {len(todo)} photos and videos under {args.prefix or '/'}")
        for key, etag, size in todo:
            if args.limit and tried >= args.limit:
                break
            pace.cool()
            t0, c0 = clock(), cpu_seconds()
            result = thumbs.ensure(client, where, key, source=(etag, size), retry_failed=args.retry_failed,
                                   dry_run=args.dry_run)
            wall, cpu = clock() - t0, cpu_seconds() - c0
            counts[result] += 1
            if result in ("made", "marked", "failed", "stale"):
                tried += 1
                pace.after(cpu)
            if timings is not None:
                timings.write(f"{key}\t{result}\t{wall:.4f}\t{cpu:.4f}\t{size}\n")
            if sum(counts.values()) % 100 == 0:
                out(f"thumbsctl: {sum(counts.values())}/{total} " + " ".join(f"{k}={v}" for k, v in counts.items() if v))
    counts["seen"] = sum(counts[name] for name in RESULTS)
    counts["elapsed_s"] = round(clock() - started, 1)
    counts["cpu_s"] = round(cpu_seconds() - cpu_started, 1)
    counts["paused_s"] = pace.paused
    counts["hottest_c"] = pace.hottest
    return counts


def parser():
    p = argparse.ArgumentParser(prog="thumbsctl", description="make the small copies the explorer shows, for photos and videos that lack them")
    p.add_argument("command", choices=["backfill"])
    p.add_argument("--user", action="append", help="owner whose folder to fill (repeatable)")
    p.add_argument("--all", action="store_true", help="every user with a key file in FILES_USER_KEYS")
    p.add_argument("--prefix", default="", help="only below this folder of the user's, e.g. camera/")
    p.add_argument("--limit", type=int, default=0, help="stop after this many thumbs were attempted; run again to continue")
    p.add_argument("--dry-run", action="store_true", help="count what would be made, write nothing")
    p.add_argument("--retry-failed", action="store_true", help="try again the photos that were marked undecodable")
    p.add_argument("--cpu-percent", type=int, default=50, help="share of one core to use, by sleeping after each file")
    p.add_argument("--pause-above-c", type=float, default=80.0, help="wait while the CPU is hotter than this; 0 turns the guard off")
    p.add_argument("--timings", help="write path, result, wall s, cpu s, bytes per file to this TSV")
    p.add_argument("--endpoint", default=os.environ.get("UPLOAD_ENDPOINT", "http://127.0.0.1:3900"))
    p.add_argument("--bucket", default=os.environ.get("UPLOAD_BUCKET", "files"))
    p.add_argument("--region", default=os.environ.get("UPLOAD_REGION", "spain"))
    p.add_argument("--keys-dir", default=os.environ.get("FILES_USER_KEYS", ""))
    return p


def main(argv=None, out=lambda line: print(line, file=sys.stderr, flush=True), settle=settle_down):
    args = parser().parse_args(argv)
    host = urlsplit(args.endpoint).hostname or ""
    if not loopback(host):
        out(f"thumbsctl: refusing endpoint {args.endpoint}: the backfill does not leave the box")
        return 2
    users = args.user or (sorted(os.listdir(args.keys_dir)) if args.all and os.path.isdir(args.keys_dir) else [])
    if not users:
        out("thumbsctl: name a user with --user, or --all with FILES_USER_KEYS set")
        return 2
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    forbid_egress()
    settle()
    config = Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=3, read_timeout=60)
    shared = boto3.client("s3", endpoint_url=args.endpoint, region_name=args.region, config=config)
    places = Places(shared, args.bucket, args.endpoint, args.region, config, keys_dir=args.keys_dir)
    pace = Pace(args.cpu_percent, args.pause_above_c or None)
    timings = open(args.timings, "w") if args.timings else None
    try:
        counts = backfill(places, args.bucket, users, args, pace, out, timings)
    finally:
        if timings:
            timings.close()
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
