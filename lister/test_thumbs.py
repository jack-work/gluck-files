#!/usr/bin/env python3
"""Small copies of photos and video posters: rendering, the ensure contract, the upload queue.

The renderer runs in a child process with a clean environment; these tests go
through that process, so what passes here is what the upload hook and the
backfill do.
"""

import hashlib
import http.server
import io
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import thumbs  # noqa: E402

WORK = tempfile.TemporaryDirectory()


def jpeg_bytes(size=(4000, 3000), orientation=None, quality=90, color=None, corner=None, extra=None, icc=None):
    im = Image.new("RGB", size, color or (90, 120, 160))
    if corner:
        im.paste(corner, (0, 0, 40, 40))
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    if extra:
        exif[0x010F] = "Camera Maker"
        exif[0x8825] = {1: "N", 2: (40.0, 41.0, 42.0), 3: "W", 4: (73.0, 59.0, 1.0)}
    buf = io.BytesIO()
    kw = {"quality": quality}
    if orientation or extra:
        kw["exif"] = exif
    if icc:
        kw["icc_profile"] = icc
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def heic_bytes(size=(800, 600), color=(200, 40, 40)):
    import pillow_heif

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "HEIF", quality=60)
    return buf.getvalue()


def fix(v):
    return struct.pack(">i", round(v * 65536))


def pad(b):
    return b + b"\0" * (-len(b) % 4)


def xyz(x, y, z):
    return b"XYZ \0\0\0\0" + fix(x) + fix(y) + fix(z)


def wide_gamut_profile():
    curve = b"curv\0\0\0\0" + struct.pack(">IH", 1, int(2.2 * 256))
    name = b"Test wide gamut\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(name)) + name + struct.pack(">II", 0, 0) + struct.pack(">HB", 0, 0) + b"\0" * 67
    tags = [(b"desc", desc), (b"cprt", b"text\0\0\0\0none\0"), (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
            (b"rXYZ", xyz(0.5151, 0.2412, -0.0011)), (b"gXYZ", xyz(0.2920, 0.6922, 0.0419)),
            (b"bXYZ", xyz(0.1571, 0.0666, 0.7841)), (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]
    table, body = struct.pack(">I", len(tags)), b""
    start = 128 + 4 + 12 * len(tags)
    for sig, data in tags:
        table += sig + struct.pack(">II", start + len(body), len(data))
        body += pad(data)
    head = struct.pack(">I4sI4s4s4s", 128 + len(table) + len(body), b"lcms", 0x02200000, b"mntr", b"RGB ", b"XYZ ")
    head += struct.pack(">6H", 2026, 11, 26, 12, 0, 0) + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
    head += fix(0.9642) + fix(1.0) + fix(0.8249) + b"\0" * 48
    return head + table + body


def segments(jpeg):
    found, i = [], 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker == 0xDA:
            break
        length = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        found.append((marker, jpeg[i + 4:i + 2 + length]))
        i += 2 + length
    return found


def opened(data):
    return Image.open(io.BytesIO(data))


def ffmpeg_make(path, graph, seconds, size="320x240", faststart=False):
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph.format(size=size), "-t", str(seconds),
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "5"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    subprocess.run(cmd + [path], check=True)
    with open(path, "rb") as f:
        return f.read()


def red_then_blue(seconds=3):
    path = os.path.join(WORK.name, f"rb{seconds}.mp4")
    graph = "color=c=red:s={size}:r=10:d=1[a];color=c=blue:s={size}:r=10:d=%d[b];[a][b]concat=n=2:v=1:a=0" % (seconds - 1)
    return ffmpeg_make(path, graph, seconds)


def short_red():
    path = os.path.join(WORK.name, "short.mp4")
    return ffmpeg_make(path, "color=c=red:s={size}:r=10:d=0.5", 0.5)


def is_blue(px):
    return px[2] > 150 and px[0] < 100


def is_red(px):
    return px[0] > 150 and px[2] < 100


class Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class Bucket:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.meta = {}
        self.stamp = {}
        self.clock = 0
        self.puts = []
        self.gets = []
        self.heads = 0
        self.dir = tempfile.mkdtemp(dir=WORK.name)

    def etag(self, key):
        return '"%s"' % hashlib.md5(self.objects[key]).hexdigest()

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None, **_):
        self.objects[Key] = bytes(Body)
        self.meta[Key] = dict(Metadata or {})
        self.clock += 1
        self.stamp[Key] = self.clock
        self.puts.append(Key)
        return {}

    def put_source(self, key, data):
        self.objects[key] = data
        self.meta.pop(key, None)
        self.clock += 1
        self.stamp[key] = self.clock

    def head_object(self, Bucket, Key, **_):
        self.heads += 1
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ETag": self.etag(Key), "ContentLength": len(self.objects[Key]), "Metadata": dict(self.meta.get(Key, {})),
                "LastModified": datetime(2026, 11, 26, tzinfo=timezone.utc)}

    def get_object(self, Bucket, Key, **_):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        self.gets.append(Key)
        return {"Body": Body(self.objects[Key]), "ETag": self.etag(Key), "ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None, Delimiter=None, **_):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if Delimiter:
            keys = [k for k in keys if Delimiter not in k[len(Prefix):]]
        start = int(ContinuationToken or 0)
        window = keys[start:start + min(MaxKeys, 40)]
        more = start + len(window) < len(keys)
        return {"Contents": [{"Key": k, "Size": len(self.objects[k]), "ETag": self.etag(k),
                              "LastModified": datetime(2026, 11, 26, 1, 0, self.stamp.get(k, 0) % 60, tzinfo=timezone.utc)}
                             for k in window],
                "IsTruncated": more, "NextContinuationToken": str(start + len(window)) if more else None}

    def generate_presigned_url(self, op, Params, ExpiresIn=60):
        path = os.path.join(self.dir, hashlib.md5(Params["Key"].encode()).hexdigest() + ".mp4")
        with open(path, "wb") as f:
            f.write(self.objects[Params["Key"]])
        return path


class RenderImage(unittest.TestCase):
    def test_long_edge_is_320_and_the_shape_is_kept(self):
        out = thumbs.render_image(jpeg_bytes((4000, 3000)))
        self.assertEqual(opened(out).size, (320, 240))
        self.assertEqual(opened(thumbs.render_image(jpeg_bytes((1000, 2500)))).size, (128, 320))

    def test_a_small_picture_is_not_enlarged(self):
        self.assertEqual(opened(thumbs.render_image(jpeg_bytes((100, 50)))).size, (100, 50))

    def test_output_is_a_jpeg_at_quality_70(self):
        out = thumbs.render_image(jpeg_bytes((1600, 1200)))
        im = opened(out)
        self.assertEqual(im.format, "JPEG")
        reference = io.BytesIO()
        Image.new("RGB", (64, 64)).save(reference, "JPEG", quality=70)
        self.assertEqual(im.quantization[0][:], opened(reference.getvalue()).quantization[0][:])

    def test_exif_orientation_is_applied(self):
        corner = (255, 0, 0)
        for orientation, where in ((1, (0, 0)), (6, (-1, 0)), (3, (-1, -1)), (8, (0, -1))):
            im = opened(thumbs.render_image(jpeg_bytes((1600, 1200), orientation=orientation, corner=corner)))
            w, h = im.size
            self.assertEqual(max(w, h), 320)
            self.assertEqual(w < h, orientation in (6, 8), orientation)
            x = 4 if where[0] == 0 else w - 5
            y = 4 if where[1] == 0 else h - 5
            self.assertTrue(is_red(im.getpixel((x, y))), (orientation, im.getpixel((x, y))))

    def test_metadata_is_stripped(self):
        source = jpeg_bytes((1600, 1200), orientation=6, extra=True, icc=wide_gamut_profile())
        self.assertIn(b"Exif", source)
        out = thumbs.render_image(source)
        self.assertEqual([m for m, _ in segments(out) if m in (0xE1, 0xE2, 0xED, 0xFE)], [])
        self.assertNotIn(b"Exif", out)
        self.assertNotIn(b"GPS", out)
        self.assertNotIn(b"Camera Maker", out)
        self.assertNotIn(b"ICC_PROFILE", out)
        self.assertEqual(dict(opened(out).getexif()), {})

    def test_wide_gamut_is_converted_to_srgb_before_the_profile_goes(self):
        color = (200, 100, 50)
        wide = opened(thumbs.render_image(jpeg_bytes((400, 300), color=color, icc=wide_gamut_profile()))).getpixel((50, 50))
        plain = opened(thumbs.render_image(jpeg_bytes((400, 300), color=color))).getpixel((50, 50))
        self.assertGreater(max(abs(a - b) for a, b in zip(wide, plain)), 8)
        self.assertLess(max(abs(a - b) for a, b in zip(plain, color)), 6)

    def test_heic_is_decoded(self):
        im = opened(thumbs.render_image(heic_bytes((1600, 1200)), heif=True))
        self.assertEqual((im.format, im.size), ("JPEG", (320, 240)))
        self.assertTrue(is_red(im.getpixel((100, 100))))

    def test_png_alpha_lands_on_white_and_other_formats_render(self):
        png = io.BytesIO()
        Image.new("RGBA", (200, 100), (0, 0, 0, 0)).save(png, "PNG")
        self.assertEqual(opened(thumbs.render_image(png.getvalue())).getpixel((10, 10)), (255, 255, 255))
        for fmt, mode in (("GIF", "P"), ("WEBP", "RGB"), ("PNG", "P"), ("PNG", "L")):
            buf = io.BytesIO()
            Image.new(mode, (500, 400)).save(buf, fmt)
            self.assertEqual(opened(thumbs.render_image(buf.getvalue())).size, (320, 256), fmt)

    def test_a_cut_off_jpeg_still_gets_a_picture(self):
        whole = jpeg_bytes((2000, 1500), quality=95)
        self.assertEqual(max(opened(thumbs.render_image(whole[:len(whole) * 3 // 4])).size), 320)

    def test_what_is_not_a_photo_is_a_permanent_failure(self):
        bmp = io.BytesIO()
        Image.new("RGB", (10, 10)).save(bmp, "BMP")
        for data in (b"", b"junk", jpeg_bytes((50, 50))[:100], bmp.getvalue()):
            with self.assertRaises(thumbs.RenderFailed) as caught:
                thumbs.render_image(data)
            self.assertTrue(caught.exception.permanent, data[:8])

    def test_a_decompression_bomb_is_refused(self):
        buf = io.BytesIO()
        Image.new("1", (15000, 15000)).save(buf, "PNG")
        with self.assertRaises(thumbs.RenderFailed) as caught:
            thumbs.render_image(buf.getvalue())
        self.assertTrue(caught.exception.permanent)

    def test_the_child_sees_no_credentials(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "do-not-leak"
        try:
            env = thumbs.clean_env()
        finally:
            del os.environ["AWS_SECRET_ACCESS_KEY"]
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual([k for k in env if k.startswith("AWS")], [])


class RenderVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = red_then_blue()

    def poster(self, data, name="v.mp4"):
        path = os.path.join(WORK.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return thumbs.render_video(path)

    def test_the_poster_is_the_frame_at_one_second(self):
        im = opened(self.poster(self.video))
        self.assertEqual(im.format, "JPEG")
        self.assertTrue(is_blue(im.getpixel((100, 100))), im.getpixel((100, 100)))

    def test_a_video_shorter_than_a_second_gives_its_first_frame(self):
        im = opened(self.poster(short_red(), "short.mp4"))
        self.assertTrue(is_red(im.getpixel((100, 100))))

    def test_the_poster_fits_320(self):
        path = os.path.join(WORK.name, "big.mp4")
        data = ffmpeg_make(path, "color=c=green:s={size}:r=10:d=2", 2, size="1280x720")
        self.assertEqual(opened(self.poster(data, "big2.mp4")).size, (320, 180))

    def test_a_script_dressed_as_a_video_reads_nothing(self):
        """ffmpeg picks its demuxer by content: an ffconcat file named .mp4
        pulls in its neighbours unless the demuxer is forced to mov."""
        Image.new("RGB", (64, 64), (255, 0, 0)).save(os.path.join(WORK.name, "secret.jpg"))
        with self.assertRaises(thumbs.RenderFailed) as caught:
            self.poster(b"ffconcat version 1.0\nfile 'secret.jpg'\n", "evil.mp4")
        self.assertTrue(caught.exception.permanent)

    def test_a_broken_video_is_a_permanent_failure(self):
        for data in (b"", b"not a video at all" * 20, self.video[:200]):
            with self.assertRaises(thumbs.RenderFailed) as caught:
                self.poster(data, "bad.mp4")
            self.assertTrue(caught.exception.permanent, data[:8])

    def test_a_url_is_read_by_ranges_not_downloaded_whole(self):
        path = os.path.join(WORK.name, "long.mp4")
        data = ffmpeg_make(path, "testsrc2=s={size}:r=25:d=20", 20, size="640x480")
        self.assertGreater(len(data), 600_000)
        served = {"bytes": 0, "ranged": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def respond(self, head_only):
                start, end = 0, len(data) - 1
                spec = self.headers.get("Range")
                if spec:
                    served["ranged"] += 1
                    a, _, b = spec.split("=")[1].partition("-")
                    start = int(a)
                    end = int(b) if b else end
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "video/mp4")
                self.end_headers()
                if not head_only:
                    self.wfile.write(data[start:end + 1])
                    served["bytes"] += end - start + 1

            def do_GET(self):
                self.respond(False)

            def do_HEAD(self):
                self.respond(True)

        class Quiet(http.server.ThreadingHTTPServer):
            def handle_error(self, *_):
                pass

        server = Quiet(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            im = opened(thumbs.render_video(f"http://127.0.0.1:{server.server_address[1]}/long.mp4"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(max(im.size), 320)
        self.assertGreater(served["ranged"], 0)
        self.assertLess(served["bytes"], len(data) * 0.9, served)


class Ensure(unittest.TestCase):
    def setUp(self):
        self.b = Bucket({"users/pilar/camera/20240512_101010.jpg": jpeg_bytes((1600, 1200)),
                         "users/pilar/camera/20240513_101010.heic": heic_bytes((800, 600))})
        self.key = "users/pilar/camera/20240512_101010.jpg"

    def ensure(self, key=None, **kw):
        return thumbs.ensure(self.b, "files", key or self.key, **kw)

    def test_the_thumb_lands_beside_the_object_and_carries_the_source_etag(self):
        self.assertEqual(self.ensure(), "made")
        tkey = ".thumbs/users/pilar/camera/20240512_101010.jpg.jpg"
        self.assertEqual(opened(self.b.objects[tkey]).size, (320, 240))
        self.assertEqual(self.b.meta[tkey], {"src-etag": hashlib.md5(self.b.objects[self.key]).hexdigest(),
                                             "src-size": str(len(self.b.objects[self.key]))})

    def test_heic_gets_its_thumb(self):
        key = "users/pilar/camera/20240513_101010.heic"
        self.assertEqual(self.ensure(key), "made")
        self.assertEqual(opened(self.b.objects[".thumbs/" + key + ".jpg"]).size, (320, 240))

    def test_a_second_pass_changes_nothing(self):
        self.ensure()
        self.b.puts.clear()
        self.b.gets.clear()
        self.assertEqual(self.ensure(), "current")
        self.assertEqual((self.b.puts, self.b.gets), ([], []))

    def test_a_replaced_source_gets_a_new_thumb(self):
        self.ensure()
        old = self.b.objects[".thumbs/" + self.key + ".jpg"]
        self.b.put_source(self.key, jpeg_bytes((1600, 1200), color=(250, 10, 10)))
        self.assertEqual(self.ensure(), "made")
        new = self.b.objects[".thumbs/" + self.key + ".jpg"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.b.meta[".thumbs/" + self.key + ".jpg"]["src-etag"], hashlib.md5(self.b.objects[self.key]).hexdigest())

    def test_the_thumb_etag_changes_when_the_picture_does(self):
        self.ensure()
        before = self.b.etag(".thumbs/" + self.key + ".jpg")
        self.b.put_source(self.key, jpeg_bytes((1600, 1200), color=(250, 10, 10)))
        self.ensure()
        self.assertNotEqual(before, self.b.etag(".thumbs/" + self.key + ".jpg"))

    def test_the_listing_etag_spares_the_source_head(self):
        etag, size = hashlib.md5(self.b.objects[self.key]).hexdigest(), len(self.b.objects[self.key])
        self.assertEqual(self.ensure(source=(etag, size)), "made")
        self.assertEqual(self.ensure(source=(etag, size)), "current")
        self.assertEqual(self.b.heads, 2)

    def test_what_is_not_a_photo_or_not_there_is_left_alone(self):
        self.b.put_source("users/pilar/notes.txt", b"hello")
        self.b.put_source("users/pilar/.hidden.jpg", jpeg_bytes((50, 50)))
        self.b.put_source("users/pilar/.thumbs/x.jpg", jpeg_bytes((50, 50)))
        self.b.put_source("users/pilar/empty.jpg", b"")
        self.assertEqual(self.ensure("users/pilar/notes.txt"), "unsupported")
        self.assertEqual(self.ensure("users/pilar/.hidden.jpg"), "unsupported")
        self.assertEqual(self.ensure("users/pilar/.thumbs/x.jpg"), "unsupported")
        self.assertEqual(self.ensure("users/pilar/empty.jpg"), "empty")
        self.assertEqual(self.ensure("users/pilar/missing.jpg"), "gone")
        self.assertEqual(self.b.puts, [])

    def test_a_corrupt_photo_leaves_a_marker_and_is_not_retried(self):
        key = "users/pilar/camera/broken.jpg"
        self.b.put_source(key, b"\xff\xd8\xff garbage")
        self.assertEqual(self.ensure(key), "marked")
        marker = ".thumbs/" + key + ".jpg"
        self.assertEqual(self.b.objects[marker], b"")
        self.assertIn("failed", self.b.meta[marker])
        self.b.gets.clear()
        self.assertEqual(self.ensure(key), "marked")
        self.assertEqual(self.b.gets, [])
        self.assertEqual(self.ensure(key, retry_failed=True), "marked")
        self.assertEqual(self.b.gets, [key])

    def test_a_marker_gives_way_when_the_source_is_replaced(self):
        key = "users/pilar/camera/broken.jpg"
        self.b.put_source(key, b"\xff\xd8\xff garbage")
        self.ensure(key)
        self.b.put_source(key, jpeg_bytes((800, 600)))
        self.assertEqual(self.ensure(key), "made")
        self.assertGreater(len(self.b.objects[".thumbs/" + key + ".jpg"]), 0)

    def test_a_transient_failure_writes_nothing(self):
        original = thumbs.render_image

        def busy(*_a, **_k):
            raise thumbs.RenderFailed("timed out", False)

        thumbs.render_image = busy
        try:
            self.assertEqual(self.ensure(), "failed")
        finally:
            thumbs.render_image = original
        self.assertEqual(self.b.puts, [])
        self.assertEqual(self.ensure(), "made")

    def test_a_video_gets_a_poster(self):
        key = "users/pilar/camera/20240514_101010.mp4"
        self.b.put_source(key, red_then_blue())
        self.assertEqual(self.ensure(key), "made")
        im = opened(self.b.objects[".thumbs/" + key + ".jpg"])
        self.assertTrue(is_blue(im.getpixel((100, 100))))

    def test_the_user_bucket_layout_uses_the_same_rule(self):
        self.assertEqual(self.ensure("camera/20240512_101010.jpg"), "gone")
        self.b.put_source("camera/20240512_101010.jpg", jpeg_bytes((800, 600)))
        self.assertEqual(self.ensure("camera/20240512_101010.jpg"), "made")
        self.assertIn(".thumbs/camera/20240512_101010.jpg.jpg", self.b.objects)


class Kinds(unittest.TestCase):
    def test_the_lister_and_the_generator_agree_on_what_has_a_thumb(self):
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        import save

        for name in ("a.jpg", "A.JPG", "a.jpeg", "a.png", "a.webp", "a.gif", "a.heic", "a.HEIF", "a.mp4", "a.MOV", "a.m4v", "a.3gp",
                     "a.pdf", "a.txt", "a.mp3", "a.svg", "a.jpg.txt", "jpg", ".jpg"):
            want = save.kind_of(name)
            self.assertEqual(thumbs.kind_of(name), want if want in ("image", "video") else None, name)


class Hook(unittest.TestCase):
    def worker(self, make, limit=512, resolve=None):
        return thumbs.Worker(resolve or (lambda key: (None, "files", key)), limit=limit, make=make)

    def test_submit_returns_before_the_work_is_done(self):
        gate, started = threading.Event(), threading.Event()

        def slow(client, bucket, inner):
            started.set()
            gate.wait(5)
            return "made"

        w = self.worker(slow)
        self.assertTrue(w.submit("users/pilar/uploads/a.jpg"))
        self.assertTrue(started.wait(5))
        self.assertEqual(w.jobs.unfinished_tasks, 1)
        gate.set()
        w.drain()

    def test_the_work_runs_on_the_lowest_priority(self):
        seen = {}

        def make(client, bucket, inner):
            seen["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
            seen["thread"] = threading.current_thread().name
            return "made"

        w = self.worker(make)
        w.submit("users/pilar/uploads/a.jpg")
        w.drain()
        self.assertEqual(seen, {"nice": 19, "thread": "thumbs"})

    def test_names_without_a_thumb_are_not_queued(self):
        w = self.worker(lambda *a: "made")
        for name in ("a.txt", "a.pdf", ".a.jpg", "users/p/.thumbs/a.jpg"):
            self.assertFalse(w.submit(name), name)
        self.assertEqual(w.jobs.unfinished_tasks, 0)

    def test_a_full_queue_drops_the_job_and_does_not_raise(self):
        gate = threading.Event()
        w = self.worker(lambda *a: gate.wait(5) and "made", limit=2)
        results = [w.submit(f"users/p/{i}.jpg") for i in range(8)]
        self.assertIn(False, results)
        gate.set()
        w.drain()

    def test_a_job_that_raises_does_not_stop_the_next(self):
        done = queue.Queue()

        def make(client, bucket, inner):
            if inner.endswith("bad.jpg"):
                raise RuntimeError("boom")
            done.put(inner)
            return "made"

        w = self.worker(make)
        w.submit("users/p/bad.jpg")
        w.submit("users/p/good.jpg")
        w.drain()
        self.assertEqual(done.get_nowait(), "users/p/good.jpg")


def tearDownModule():
    WORK.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
