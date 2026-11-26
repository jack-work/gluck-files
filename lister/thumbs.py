import ctypes
import logging
import os
import platform
import queue
import resource
import subprocess
import sys
import threading

from botocore.exceptions import ClientError

THUMB_DIR = ".thumbs/"
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")
VIDEO_TYPES = (".mp4", ".mov", ".m4v", ".3gp")
HEIF_TYPES = (".heic", ".heif")
RENDERER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbrender.py")
FFMPEG = os.environ.get("THUMBS_FFMPEG", "ffmpeg")
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
RENDER_SECONDS = int(os.environ.get("THUMBS_RENDER_SECONDS", "60"))
POSTER_SECONDS = int(os.environ.get("THUMBS_POSTER_SECONDS", "90"))
PRESIGN_SECONDS = 180
EDGE = 320
EXIT_BAD_IMAGE = 3
IOPRIO_NUMBERS = {"x86_64": 251, "aarch64": 30}

BAD_VIDEO_MARKERS = (
    "Invalid data found when processing input",
    "moov atom not found",
    "does not contain any stream",
    "Output file does not contain any stream",
)

log = logging.getLogger("gluck-files-thumbs")


class RenderFailed(Exception):
    def __init__(self, why, permanent):
        super().__init__(why)
        self.permanent = permanent


def kind_of(name):
    low = name.lower()
    if low.endswith(IMAGE_TYPES):
        return "image"
    if low.endswith(VIDEO_TYPES):
        return "video"
    return None


def thumb_key(inner):
    return THUMB_DIR + inner + ".jpg"


def hidden(inner):
    return any(part.startswith(".") for part in inner.split("/"))


def wanted(inner):
    return kind_of(inner) is not None and not hidden(inner)


def bare(etag):
    return (etag or "").strip('"')


def lower_priority():
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except OSError:
        pass
    number = IOPRIO_NUMBERS.get(platform.machine())
    if number is None:
        return
    try:
        ctypes.CDLL(None, use_errno=True).syscall(number, 1, threading.get_native_id(), 3 << 13)
    except (OSError, AttributeError):
        pass


def clean_env():
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR") or k.startswith(("THUMBS_", "NIX_"))}
    keep["PYTHONNOUSERSITE"] = "1"
    return keep


def render_image(data, heif=False):
    cmd = [sys.executable, RENDERER] + (["--heif"] if heif else [])
    try:
        done = subprocess.run(cmd, input=data, capture_output=True, timeout=RENDER_SECONDS, env=clean_env())
    except subprocess.TimeoutExpired:
        raise RenderFailed("render timed out", False)
    if done.returncode == 0 and done.stdout:
        return done.stdout
    why = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit {done.returncode}"]
    raise RenderFailed(why[0][:200], done.returncode == EXIT_BAD_IMAGE)


def poster_frame(source, ffmpeg=None):
    ffmpeg = ffmpeg or FFMPEG
    whitelist = "http,https,tcp,tls" if source.startswith(("http://", "https://")) else "file"
    scale = f"scale=w='min({EDGE},iw)':h='min({EDGE},ih)':force_original_aspect_ratio=decrease"
    for seek in ("1", "0"):
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
               "-protocol_whitelist", whitelist, "-f", "mov", "-ss", seek, "-i", source,
               "-frames:v", "1", "-an", "-sn", "-dn", "-vf", scale, "-f", "image2pipe", "-c:v", "png", "pipe:1"]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env())
        try:
            resource.prlimit(proc.pid, resource.RLIMIT_CPU, (POSTER_SECONDS, POSTER_SECONDS))
        except (OSError, ValueError):
            pass
        try:
            out, err = proc.communicate(timeout=POSTER_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RenderFailed("poster timed out", False)
        if proc.returncode == 0 and out and len(out) <= MAX_FRAME_BYTES:
            return out
        if proc.returncode != 0:
            lines = err.decode("utf-8", "replace").strip().splitlines()
            why = lines[-1][:200] if lines else f"ffmpeg exit {proc.returncode}"
            raise RenderFailed(why, proc.returncode > 0 and any(m in why for m in BAD_VIDEO_MARKERS))
    raise RenderFailed("no video frame", True)


def render_video(source, ffmpeg=None):
    return render_image(poster_frame(source, ffmpeg))


def head_of(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def current_thumb(found, etag, size):
    meta = found.get("Metadata") or {}
    return meta.get("src-etag") == etag and meta.get("src-size") == str(size)


def store(client, bucket, inner, body, etag, size, failed=""):
    meta = {"src-etag": etag, "src-size": str(size)}
    if failed:
        meta["failed"] = failed.encode("ascii", "replace").decode()[:120]
    client.put_object(Bucket=bucket, Key=thumb_key(inner), Body=body, ContentType="image/jpeg", Metadata=meta)


def ensure(client, bucket, inner, source=None, force=False, retry_failed=False, ffmpeg=None):
    kind = kind_of(inner)
    if kind is None or hidden(inner):
        return "unsupported"
    if source is None:
        head = head_of(client, bucket, inner)
        if head is None:
            return "gone"
        source = (bare(head.get("ETag")), head.get("ContentLength", 0))
    etag, size = source
    if not size:
        return "empty"
    if not force:
        found = head_of(client, bucket, thumb_key(inner))
        if found is not None and current_thumb(found, etag, size):
            if found.get("ContentLength", 0) > 0:
                return "current"
            if not retry_failed:
                return "marked"
    try:
        if kind == "video":
            url = client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": inner}, ExpiresIn=PRESIGN_SECONDS)
            jpeg = render_video(url, ffmpeg)
        else:
            if size > MAX_IMAGE_BYTES:
                raise RenderFailed("too large to thumbnail", True)
            data = client.get_object(Bucket=bucket, Key=inner)["Body"].read()
            jpeg = render_image(data, inner.lower().endswith(HEIF_TYPES))
    except RenderFailed as e:
        log.warning("thumb failed for %s: %s", inner, e)
        if e.permanent:
            store(client, bucket, inner, b"", etag, size, failed=str(e))
            return "marked"
        return "failed"
    store(client, bucket, inner, jpeg, etag, size)
    return "made"


class Worker:
    def __init__(self, resolve, limit=512, make=ensure):
        self.resolve = resolve
        self.make = make
        self.jobs = queue.Queue(maxsize=limit)
        self.started = False
        self.lock = threading.Lock()

    def submit(self, key):
        if not wanted(key):
            return False
        try:
            self.jobs.put_nowait(key)
        except queue.Full:
            log.warning("thumb queue full, %s left to the backfill", key)
            return False
        with self.lock:
            if not self.started:
                self.started = True
                threading.Thread(target=self.run, name="thumbs", daemon=True).start()
        return True

    def run(self):
        lower_priority()
        while True:
            key = self.jobs.get()
            try:
                client, bucket, inner = self.resolve(key)
                log.info("thumb %s %s", self.make(client, bucket, inner), key)
            except Exception:
                log.exception("thumb job for %s", key)
            finally:
                self.jobs.task_done()

    def drain(self):
        self.jobs.join()
