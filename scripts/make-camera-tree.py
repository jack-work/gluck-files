#!/usr/bin/env python3
"""A synthetic ~2000-item camera folder, flat, in her naming pattern.

usage: make-camera-tree.py OUT [--thumbs] [--count N]

OUT/camera/ gets JPEG, HEIC and MP4 files named YYYYMMDD_HHMMSS.ext (some
_NNN and (N) variants), a few IMG_NNNN.JPG and uuid.jpg names, in mixed sizes.
Every file is unique (random trailer bytes) so hashes differ. With --thumbs,
OUT/.thumbs/camera/<name>.jpg are written at 320 px long edge for all but
about 3 percent (those are the 'pending' tiles). Invented pictures only."""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

args = [a for a in sys.argv[1:] if not a.startswith("--")]
out = args[0]
count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 2000
if "--count" in sys.argv:
    args = [a for a in args if a != str(count)]
thumbs = "--thumbs" in sys.argv
r = random.Random(2026)
here = os.path.expanduser("~/dev/zanni-files-mockups/examples/files-mockups/.thumbs")
work = tempfile.mkdtemp()
cam = os.path.join(out, "camera")
os.makedirs(cam, exist_ok=True)


def run(*a):
    subprocess.run(a, check=True, stderr=subprocess.DEVNULL)


bases = []
for i in range(12):
    src = os.path.join(here, f"{i}.jpg")
    for tag, size, q in (("a", "1024x768", 78), ("b", "2048x1536", 72), ("c", "640x480", 80)):
        jp = os.path.join(work, f"{i}{tag}.jpg")
        run("magick", src, "-resize", size + "!", "-attenuate", "0.4", "+noise", "Gaussian", "-quality", str(q), jp)
        bases.append(("jpg", jp))
    hp = os.path.join(work, f"{i}.heic")
    run("magick", src, "-resize", "1024x768!", "-quality", "60", hp)
    bases.append(("heic", hp))
mp4s = []
for i, dur in enumerate((2, 3, 4, 6)):
    p = os.path.join(work, f"v{i}.mp4")
    run("ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={320 + i * 160}x{240 + i * 120}:rate=12:duration={dur}",
        "-pix_fmt", "yuv420p", p)
    mp4s.append(p)
jpgs = [b for b in bases if b[0] == "jpg"]
heics = [b for b in bases if b[0] == "heic"]

names = set()
plan = []


def stamp():
    year = r.choices(range(2016, 2027), weights=(1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 7))[0]
    month = r.randint(1, 11 if year == 2026 else 12)
    return f"{year}{month:02d}{r.randint(1, 26 if month == 11 else 28):02d}_{r.randint(6, 22):02d}{r.randint(0, 59):02d}{r.randint(0, 59):02d}"


while len(plan) < count - 10:
    base = stamp()
    for suffix in ("", "", "", "_001", "(1)"):
        kind = r.choices(("jpg", "heic", "mp4"), weights=(86, 12, 2))[0]
        ext = {"jpg": "jpg", "heic": "heic", "mp4": "mp4"}[kind]
        name = f"{base}{suffix}.{ext}"
        if name in names or len(plan) >= count - 10:
            continue
        names.add(name)
        plan.append((name, kind))
        if r.random() < 0.5:
            break
for i in range(6):
    plan.append((f"IMG_{r.randint(1000, 9999)}.JPG", "jpg"))
for i in range(4):
    plan.append((f"{uuid.UUID(int=r.getrandbits(128))}.jpg", "jpg"))

total = 0
for name, kind in plan:
    if kind == "jpg":
        src = r.choice(jpgs)[1]
    elif kind == "heic":
        src = r.choice(heics)[1]
    else:
        src = r.choice(mp4s)
    dst = os.path.join(cam, name)
    shutil.copy(src, dst)
    with open(dst, "ab") as f:
        f.write(r.randbytes(r.randint(16, 4096)))
    total += os.path.getsize(dst)
    if thumbs and r.random() > 0.03:
        td = os.path.join(out, ".thumbs", "camera")
        os.makedirs(td, exist_ok=True)
        tsrc = os.path.join(here, f"{r.randrange(12)}.jpg")
        run("magick", tsrc, "-resize", "320x320!", "-quality", "70", "-strip", os.path.join(td, name + ".jpg"))
shutil.rmtree(work)
print(f"{len(plan)} files, {total / 1e6:.0f} MB in {cam}")
