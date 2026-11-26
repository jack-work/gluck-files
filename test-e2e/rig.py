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
os.environ["UPLOAD_ACL_DB"] = os.path.join(tmp, "acl.db")
os.environ["UPLOAD_ORIGIN"] = "http://127.0.0.1"
os.environ["UPLOAD_PAGE_DIR"] = os.path.join(ROOT, "upload-page")
os.environ["SAVE_PAGE_DIR"] = os.path.join(ROOT, "save-page")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
sys.path.insert(0, os.path.join(ROOT, "lister"))

import lister  # noqa: E402
import save  # noqa: E402
import upload  # noqa: E402
from fakebucket import FakeBucket, invented  # noqa: E402
from flask import Flask, Response, request  # noqa: E402
from waitress import serve  # noqa: E402

save.BATCH_BYTES = 6 * 1024 * 1024
OBJECTS = invented()
BUCKET = FakeBucket(OBJECTS)


def at(bucket, key, ensure=False):
    return BUCKET, "u-pilar", key[len("users/pilar/"):]


upload.PLACES.at = at
lister.PLACES.at = at
lister.PLACES.outer = lambda bucket, key: "users/pilar/" if key.startswith("users/pilar/") else ""
for perm in ("read", "list"):
    upload.ACL.grant("user:pilar", "users/pilar/", perm, "rig")


def csp(name):
    text = open(os.path.join(ROOT, "lib", name)).read()
    return re.findall(r'>Content-Security-Policy "([^"]+)"', text)


SAVE_POLICY = csp("upload-policy.nix")[0]
VIEW_POLICY = csp("media-document-policy.nix")[0]
LISTING_POLICY = re.search(r'"Content-Security-Policy" = "([^"]+)"', open(os.path.join(ROOT, "lib", "media-headers.nix")).read()).group(1)

edge = Flask(__name__)


@edge.get("/users/pilar/photos/<path:rel>")
def obj(rel):
    if request.headers.get("Remote-User") != "pilar":
        return Response("denied", status=403)
    data = OBJECTS.get("photos/" + rel)
    if data is None:
        return Response("", status=404)
    ctype = "image/jpeg" if rel.lower().endswith("jpg") else "video/mp4" if rel.endswith("mp4") else "application/octet-stream"
    return Response(data, mimetype=ctype, headers={"Cache-Control": "private, no-store"})


@edge.get("/_rig/objects")
def listing_of_objects():
    import hashlib
    return {k[len("photos/"):]: hashlib.sha256(v).hexdigest() for k, v in OBJECTS.items()}


@lister.app.after_request
def lister_policy(resp):
    path = request.path
    if path == "/_files/view":
        resp.headers["Content-Security-Policy"] = VIEW_POLICY
    elif resp.mimetype == "text/html":
        resp.headers["Content-Security-Policy"] = LISTING_POLICY.replace("sandbox; ", "")
    return resp


@upload.app.after_request
def upload_policy(resp):
    if request.path.startswith(("/_save/", "/_upload/")):
        resp.headers["Content-Security-Policy"] = SAVE_POLICY
    return resp


def wsgi(environ, start):
    path = environ["PATH_INFO"]
    if path.startswith(("/_save/", "/_upload/")):
        app = upload.app
    elif path.startswith("/_rig/") or (path.startswith("/users/") and not path.endswith("/")):
        app = edge
    else:
        app = lister.app
    return app.wsgi_app(environ, start)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    print("rig on", port, flush=True)
    serve(wsgi, host="127.0.0.1", port=port, threads=8)
