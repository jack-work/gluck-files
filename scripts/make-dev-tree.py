#!/usr/bin/env python3
"""Invented photo tree for a files dev instance: dated phone-style names, small
real JPEGs and MP4s, nothing from anyone's phone.  usage: make-dev-tree.py OUT
Copy OUT/photos/ into the dev user's bucket under photos/keep/ (and all/)."""
import os
import random
import shutil
import subprocess
import sys

out = sys.argv[1]
scenes = [os.path.expanduser(f"~/dev/zanni-files-mockups/examples/files-mockups/.thumbs/{i}.jpg") for i in range(12)]
r = random.Random(11)
cam = os.path.join(out, "photos", "keep", "DCIM", "Camera")
os.makedirs(cam, exist_ok=True)
n = 0
for month, days in (("202411", range(1, 27)), ("202410", range(1, 28, 2)), ("202407", range(4, 20))):
    for d in days:
        for i in range(r.randint(1, 3)):
            name = f"{month}{d:02d}_{9 + i * 3:02d}{r.randint(0, 59):02d}00.jpg"
            shutil.copy(scenes[r.randrange(12)], os.path.join(cam, name))
            n += 1
for stamp in ("20241103_181500", "20241111_120000", "20241020_150000"):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12:duration=3",
                    "-pix_fmt", "yuv420p", os.path.join(cam, stamp + ".mp4")], check=True)
shutil.copy(scenes[3], os.path.join(cam, "IMG_0042.JPG"))
print(f"{n} photos, 3 videos in {cam}")
