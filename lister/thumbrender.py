#!/usr/bin/env python3
import io
import os
import resource
import struct
import sys

EDGE = 320
QUALITY = 70
MAX_PIXELS = 100_000_000
FORMATS = ("JPEG", "MPO", "PNG", "WEBP", "GIF", "HEIF")
EXIT_BAD_IMAGE = 3
EXIT_TRANSIENT = 4
CPU_SECONDS = int(os.environ.get("THUMBS_CPU_SECONDS", "60"))
ADDRESS_MB = int(os.environ.get("THUMBS_ADDRESS_MB", "1536"))


def limit_self():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_MB << 20, ADDRESS_MB << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def srgb(im, ImageCms):
    raw = im.info.get("icc_profile")
    if not raw:
        return im
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(raw))
        if "srgb" in (ImageCms.getProfileDescription(source) or "").lower():
            return im
        return ImageCms.profileToProfile(im, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
    except (ImageCms.PyCMSError, OSError, ValueError):
        return im


def flattened(im, Image):
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return im if im.mode == "RGB" else im.convert("RGB")


def jpeg_from(data, heif=False):
    from PIL import Image, ImageCms, ImageFile, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    if heif:
        import pillow_heif

        pillow_heif.register_heif_opener()
    im = Image.open(io.BytesIO(data))
    if im.format not in FORMATS:
        raise ValueError(f"not a photo format: {im.format}")
    if im.format in ("JPEG", "MPO"):
        im.draft("RGB", (EDGE, EDGE))
    im = ImageOps.exif_transpose(im)
    if im.mode == "P":
        im = im.convert("RGBA")
    im.thumbnail((EDGE, EDGE), Image.LANCZOS, reducing_gap=2.0)
    profile = im.info.get("icc_profile")
    im = flattened(im, Image)
    if profile:
        im.info["icc_profile"] = profile
        im = srgb(im, ImageCms)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()


def decode_errors():
    from PIL import Image

    return (Image.UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, EOFError,
            struct.error)


def main(argv):
    limit_self()
    data = sys.stdin.buffer.read()
    try:
        out = jpeg_from(data, "--heif" in argv)
    except MemoryError:
        sys.stderr.write("out of memory\n")
        return EXIT_TRANSIENT
    except decode_errors() as e:
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        return EXIT_BAD_IMAGE
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
