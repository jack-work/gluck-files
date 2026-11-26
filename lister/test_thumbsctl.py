#!/usr/bin/env python3
"""The backfill: complete, repeatable, resumable, gentle, and unable to leave the box."""

import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
import thumbsctl as C  # noqa: E402
from test_thumbs import Bucket, jpeg_bytes, heic_bytes, red_then_blue  # noqa: E402


class Places:
    def __init__(self, bucket, per_user=False):
        self.bucket, self.per_user = bucket, per_user

    def at(self, bucket, key, ensure=False):
        if self.per_user:
            owner = key.split("/")[1]
            return self.bucket, "u-" + owner, key[len("users/" + owner + "/"):]
        return self.bucket, bucket, key


def arguments(**kw):
    base = dict(prefix="", limit=0, dry_run=False, retry_failed=False)
    base.update(kw)
    return SimpleNamespace(**base)


def pace(**kw):
    return C.Pace(kw.get("cpu_percent", 100), kw.get("limit"), sleep=kw.get("sleep", lambda s: None), temp=kw.get("temp", lambda: None))


class Folder(unittest.TestCase):
    def setUp(self):
        photo = jpeg_bytes((800, 600))
        self.bucket = Bucket()
        self.names = [f"20240{m}1{d}_101010.jpg" for m in (1, 2, 3) for d in (1, 2, 3)]
        for n in self.names:
            self.bucket.put_source(f"users/pilar/camera/{n}", jpeg_bytes((800, 600), color=(10 * len(n), 20, 30 + sum(map(ord, n)) % 200)))
        self.bucket.put_source("users/pilar/camera/20240401_101010.heic", heic_bytes((640, 480)))
        self.bucket.put_source("users/pilar/camera/IMG_0042.JPG", photo)
        self.bucket.put_source("users/pilar/camera/notes.txt", b"hi")
        self.bucket.put_source("users/pilar/camera/scan.pdf", b"%PDF")
        self.bucket.put_source("users/pilar/camera/.trashed.jpg", photo)
        self.bucket.put_source("users/pilar/camera/zero.jpg", b"")
        self.bucket.put_source("users/pilar/other/20240101_000000.jpg", photo)
        self.bucket.put_source("users/mom/camera/20240101_000000.jpg", photo)
        self.lines = []
        self.eligible = 12

    def run_backfill(self, places=None, user="pilar", pc=None, **kw):
        return C.backfill(places or Places(self.bucket), "files", [user], arguments(**kw), pc or pace(), self.lines.append)

    def thumbs_of(self, user="pilar"):
        return sorted(k for k in self.bucket.objects if k.startswith(f".thumbs/users/{user}/"))

    def test_every_photo_and_video_gets_its_thumb_and_nothing_else_does(self):
        self.bucket.put_source("users/pilar/camera/20240402_101010.mp4", red_then_blue())
        counts = self.run_backfill()
        self.assertEqual((counts["made"], counts["failed"], counts["marked"]), (self.eligible + 1, 0, 0))
        made = self.thumbs_of()
        self.assertNotIn(".thumbs/users/pilar/camera/notes.txt.jpg", made)
        self.assertNotIn(".thumbs/users/pilar/camera/scan.pdf.jpg", made)
        self.assertNotIn(".thumbs/users/pilar/camera/.trashed.jpg.jpg", made)
        self.assertIn(".thumbs/users/pilar/camera/20240402_101010.mp4.jpg", made)
        self.assertEqual(self.thumbs_of("mom"), [])

    def test_a_second_run_does_nothing(self):
        self.run_backfill()
        self.bucket.puts.clear()
        self.bucket.gets.clear()
        counts = self.run_backfill()
        self.assertEqual((counts["made"], counts["failed"]), (0, 0))
        self.assertGreaterEqual(counts["current"], self.eligible - 1)
        self.assertEqual((self.bucket.puts, self.bucket.gets), ([], []))

    def test_a_limit_stops_early_and_the_next_run_goes_on(self):
        first = self.run_backfill(limit=4)
        self.assertEqual(first["made"], 4)
        second = self.run_backfill(limit=4)
        self.assertEqual(second["current"], 4)
        self.assertEqual(second["made"], 4)
        rest = self.run_backfill()
        self.assertEqual(len(self.thumbs_of()), self.eligible)
        self.assertEqual(first["made"] + second["made"] + rest["made"] + rest["marked"], self.eligible)

    def test_the_newest_pictures_come_first_and_undated_last(self):
        self.run_backfill(limit=3)
        self.assertEqual(sorted(k.rsplit("/", 1)[1] for k in self.thumbs_of()),
                         ["20240312_101010.jpg.jpg", "20240313_101010.jpg.jpg", "20240401_101010.heic.jpg"])
        self.assertNotIn(".thumbs/users/pilar/camera/IMG_0042.JPG.jpg", self.thumbs_of())

    def test_a_changed_source_is_the_only_thing_redone(self):
        self.run_backfill()
        self.bucket.put_source("users/pilar/camera/20240111_101010.jpg", jpeg_bytes((800, 600), color=(255, 0, 255)))
        self.bucket.puts.clear()
        counts = self.run_backfill()
        self.assertEqual(counts["made"], 1)
        self.assertEqual(self.bucket.puts, [".thumbs/users/pilar/camera/20240111_101010.jpg.jpg"])

    def test_an_undecodable_photo_is_counted_once_and_then_left_alone(self):
        self.bucket.put_source("users/pilar/camera/broken.jpg", b"\xff\xd8 not a picture")
        first = self.run_backfill()
        self.assertEqual(first["marked"], 1)
        self.bucket.gets.clear()
        second = self.run_backfill()
        self.assertEqual((second["marked"], second["made"]), (1, 0))
        self.assertNotIn("users/pilar/camera/broken.jpg", self.bucket.gets)
        self.run_backfill(retry_failed=True)
        self.assertIn("users/pilar/camera/broken.jpg", self.bucket.gets)

    def test_a_dry_run_counts_and_writes_nothing(self):
        counts = self.run_backfill(dry_run=True)
        self.assertEqual(counts["stale"], self.eligible)
        self.assertEqual((self.bucket.puts, self.bucket.gets), ([], []))

    def test_a_prefix_narrows_the_walk(self):
        self.run_backfill(prefix="other/")
        self.assertEqual(self.thumbs_of(), [".thumbs/users/pilar/other/20240101_000000.jpg.jpg"])

    def test_the_per_user_bucket_layout_lands_where_the_lister_looks(self):
        self.bucket.objects = {k[len("users/pilar/"):]: v for k, v in self.bucket.objects.items() if k.startswith("users/pilar/")}
        C.backfill(Places(self.bucket, per_user=True), "files", ["pilar"], arguments(), pace(), self.lines.append)
        self.assertIn(".thumbs/camera/20240111_101010.jpg.jpg", self.bucket.objects)

    def test_timings_are_written_per_file(self):
        import io

        sink = io.StringIO()
        C.backfill(Places(self.bucket), "files", ["pilar"], arguments(limit=2), pace(), self.lines.append, timings=sink)
        rows = [r.split("\t") for r in sink.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "made")
        self.assertGreater(float(rows[0][2]), 0)

    def test_a_user_with_no_bucket_is_reported_and_skipped(self):
        class Nobody(Places):
            def at(self, bucket, key, ensure=False):
                from place import Unplaced
                raise Unplaced("nobody has no bucket yet")

        counts = C.backfill(Nobody(self.bucket), "files", ["nobody"], arguments(), pace(), self.lines.append)
        self.assertEqual(counts["seen"], 0)
        self.assertTrue(any("no bucket yet" in line for line in self.lines))


class Gentle(unittest.TestCase):
    def test_sleep_follows_cpu_so_the_share_holds(self):
        slept = []
        p = pace(cpu_percent=50, sleep=slept.append)
        p.after(0.2)
        p = pace(cpu_percent=25, sleep=slept.append)
        p.after(0.2)
        p = pace(cpu_percent=100, sleep=slept.append)
        p.after(0.2)
        self.assertEqual([round(s, 3) for s in slept], [0.2, 0.6])

    def test_a_hot_cpu_makes_it_wait_until_it_cools(self):
        temps = iter([91.0, 86.0, 79.0, 70.0])
        slept = []
        p = C.Pace(100, 80.0, sleep=slept.append, temp=lambda: next(temps))
        p.cool()
        self.assertEqual(slept, [C.PAUSE_SECONDS] * 2)
        self.assertEqual(p.hottest, 91.0)
        self.assertEqual(p.paused, 30)

    def test_a_stuck_sensor_does_not_stop_the_run_for_ever(self):
        slept = []
        p = C.Pace(100, 80.0, sleep=slept.append, temp=lambda: 99.0)
        p.cool()
        self.assertEqual(sum(slept), C.PAUSE_LIMIT_SECONDS)

    def test_no_sensor_means_no_guard(self):
        slept = []
        p = C.Pace(100, 80.0, sleep=slept.append, temp=lambda: None)
        p.cool()
        self.assertEqual((slept, p.hottest), ([], None))

    def test_cpu_sensors_are_read_from_sysfs_and_the_rest_ignored(self):
        root = tempfile.mkdtemp()

        def put(path, text):
            os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
            open(os.path.join(root, path), "w").write(text)

        put("class/hwmon/hwmon0/name", "nvme\n")
        put("class/hwmon/hwmon0/temp1_input", "88000\n")
        put("class/hwmon/hwmon1/name", "k10temp\n")
        put("class/hwmon/hwmon1/temp1_input", "54500\n")
        put("class/hwmon/hwmon1/temp3_input", "51000\n")
        put("class/thermal/thermal_zone0/type", "acpitz\n")
        put("class/thermal/thermal_zone0/temp", "99000\n")
        put("class/thermal/thermal_zone1/type", "x86_pkg_temp\n")
        put("class/thermal/thermal_zone1/temp", "61000\n")
        self.assertEqual(C.cpu_temp_c(root), 61.0)
        self.assertIsNone(C.cpu_temp_c(tempfile.mkdtemp()))


class Boundary(unittest.TestCase):
    def test_an_endpoint_off_the_box_is_refused_before_anything_runs(self):
        lines = []
        self.assertEqual(C.main(["backfill", "--user", "pilar", "--endpoint", "https://s3.kelliher.info"], out=lines.append), 2)
        self.assertTrue(any("does not leave the box" in line for line in lines))
        for ok in ("http://127.0.0.1:3900", "http://localhost:3900", "http://[::1]:3900"):
            self.assertTrue(C.loopback(C.urlsplit(ok).hostname), ok)
        for off in ("https://s3.kelliher.info", "http://10.0.0.5:3900", "http://169.254.169.254"):
            self.assertFalse(C.loopback(C.urlsplit(off).hostname or ""), off)

    def test_no_user_is_an_error(self):
        self.assertEqual(C.main(["backfill"], out=lambda line: None), 2)

    def test_the_process_cannot_connect_off_the_box_even_if_asked(self):
        code = (
            "import socket, sys\n"
            "sys.path.insert(0, %r)\n"
            "import thumbsctl\n"
            "thumbsctl.forbid_egress()\n"
            "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)\n"
            "socket.create_connection(srv.getsockname(), 1).close()\n"
            "try:\n"
            "    socket.create_connection(('93.184.216.34', 80), 1)\n"
            "except OSError as e:\n"
            "    print('refused', e.errno)\n"
        ) % os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.stdout.strip(), "refused 101", out.stderr)

    def test_the_run_settles_to_the_lowest_priority(self):
        code = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "import thumbsctl\n"
            "thumbsctl.settle_down()\n"
            "print(os.getpriority(os.PRIO_PROCESS, 0))\n"
        ) % os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.stdout.strip(), "19", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
