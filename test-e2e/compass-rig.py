import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
tmp = tempfile.mkdtemp()
ZANNI = os.environ.get("ZANNI_DIR", os.path.expanduser("~/dev/zanni"))


def built_template():
    src = open(os.path.join(ROOT, "lister", "template.html.in")).read()
    fonts = gesso = ""
    try:
        sys.path.insert(0, os.path.join(ZANNI, "examples", "files-mockups"))
        import lib
        fonts, gesso = lib.fonts_css(), open(os.path.join(ZANNI, "assets", "gesso", "gesso.css")).read()
    except Exception:
        pass
    if fonts:
        fonts += ":root{--fontpack-display:'Jacquard 24',serif;--fontpack-prose:'EB Garamond',Georgia,serif;--fontpack-mono:'VT323',ui-monospace,monospace}"
    src = src.replace("<!-- zanni:fontpack -->", f"<style>{fonts}</style>").replace("<!-- zanni:gesso -->", f"<style>{gesso}</style>")
    path = os.path.join(tmp, "template.html")
    open(path, "w").write(src)
    return path


os.environ["LISTER_TEMPLATE"] = built_template()
os.environ["LISTER_BUCKETS"] = "files"
os.environ["LISTER_ROOT_DOMAIN"] = ".kelliher.info"
os.environ["LISTER_ACL_DB"] = os.path.join(tmp, "acl.db")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
sys.path.insert(0, os.path.join(ROOT, "lister"))

import lister  # noqa: E402
from fakebucket import FakeBucket  # noqa: E402
from flask import request  # noqa: E402
from place import home_owner  # noqa: E402
from waitress import serve  # noqa: E402

HOMES = {
    "pilar": {"camera/20240512_101010.jpg": 400, "camera/20240513_101010.jpg": 400, "notes/shopping.txt": 40},
    "admin": {"camera/20260101_090000.jpg": 400, "papers/draft.pdf": 900},
    "ctkelliher": {"camera/20250626_111217.jpg": 400, "camera/20250627_093000.heic": 400, "trips/denny/20250628_120000.jpg": 400},
    "zoe": {"camera/20240101_100000.jpg": 400},
}
SHARED = {"dad/notes.txt": 12, "dad/Mitchell-1997.pdf": 5000, "family/20240704_150000.jpg": 400, "wfh/Rental_Agreement.pdf": 900}


def blobs(table, base=""):
    return {base + k: bytes(n) for k, n in table.items()}


SHARED_BUCKET = FakeBucket(blobs(SHARED), page=1000)
HOME_BUCKETS = {o: FakeBucket(blobs(t), page=1000) for o, t in HOMES.items()}


def at(bucket, key, ensure=False):
    owner = home_owner(key)
    if owner in HOME_BUCKETS:
        return HOME_BUCKETS[owner], "u-" + owner, key[len(f"users/{owner}/"):]
    return SHARED_BUCKET, bucket, key


lister.PLACES.at = at
lister.PLACES.outer = lambda bucket, key: f"users/{home_owner(key)}/" if home_owner(key) in HOME_BUCKETS else ""

ACL = lister.ACL
for g in ACL.list_grants():
    ACL.revoke(g["subject"], g["path"], g["perm"])
for who, perms in {
    "pilar": ("read", "list", "write"),
    "admin": ("read", "list", "write"),
    "ctkelliher": ("read", "list", "write"),
    "zoe": ("read", "list", "write"),
}.items():
    for p in perms:
        ACL.grant(f"user:{who}", f"users/{who}/", p, "rig")
for p in ("read", "list", "write"):
    ACL.grant("group:files-admin", "", p, "rig")
    ACL.grant("user:admin", "users/ctkelliher/", p, "rig")
    ACL.grant("user:admin", "users/pilar/camera/", p, "rig")
for p in ("read", "list"):
    ACL.grant("user:dad", "dad/", p, "rig")
    ACL.grant("user:zoe", "family/", p, "rig")
    ACL.grant("user:zoe", "users/pilar/camera/", p, "rig")
ACL.grant("user:scribe", "inbox/", "write", "rig")
ACL.grant("user:friend", "users/zoe/", "write", "rig")

LISTING_POLICY = re.search(r'"Content-Security-Policy" = "([^"]+)"', open(os.path.join(ROOT, "lib", "media-headers.nix")).read()).group(1)


@lister.app.after_request
def lister_policy(resp):
    if resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = LISTING_POLICY.replace("sandbox; ", "")
    return resp


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8793
    print("compass rig on", port, flush=True)
    serve(lister.app, host="127.0.0.1", port=port, threads=8)
