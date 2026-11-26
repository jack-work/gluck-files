import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
tmp = tempfile.mkdtemp()
os.environ["UPLOAD_ORIGIN"] = "http://127.0.0.1"
os.environ["UPLOAD_PAGE_DIR"] = os.path.join(ROOT, "upload-page")
os.environ["SAVE_PAGE_DIR"] = os.path.join(ROOT, "save-page")
os.environ["UPLOAD_ACL_DB"] = os.path.join(tmp, "acl.db")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
sys.path.insert(0, os.path.join(ROOT, "lister"))

import save  # noqa: E402
import upload  # noqa: E402
from fakebucket import FakeBucket, invented  # noqa: E402
from flask import Flask, Response, request  # noqa: E402
from waitress import serve  # noqa: E402

save.BATCH_BYTES = 6 * 1024 * 1024
OBJECTS = invented()
BUCKET = FakeBucket(OBJECTS)
upload.PLACES.at = lambda bucket, key, ensure=False: (BUCKET, "u-pilar", key[len("users/pilar/"):])
for perm in ("read", "list"):
    upload.ACL.grant("user:pilar", "users/pilar/", perm, "rig")

with open(os.path.join(ROOT, "lib", "upload-policy.nix")) as f:
    POLICY = re.search(r'@save_page path /_save/\*\s*header @save_page \{\s*>Content-Security-Policy "([^"]+)"', f.read()).group(1)

edge = Flask(__name__)


@edge.after_request
def policy(resp):
    if request.path.startswith("/_save/"):
        resp.headers["Content-Security-Policy"] = POLICY
    return resp


@edge.get("/users/pilar/photos/<path:rel>")
def obj(rel):
    if request.headers.get("Remote-User") != "pilar":
        return Response("denied", status=403)
    data = OBJECTS.get("photos/" + rel)
    if data is None:
        return Response("", status=404)
    return Response(data, mimetype="application/octet-stream", headers={"Cache-Control": "private, no-store"})


@edge.get("/_rig/objects")
def listing():
    import hashlib
    return {k[len("photos/"):]: hashlib.sha256(v).hexdigest() for k, v in OBJECTS.items()}


def wsgi(environ, start):
    def sr(status, headers, exc=None):
        return start(status, headers, exc)
    app = upload.app if environ["PATH_INFO"].startswith(("/_save/", "/_upload/")) else edge
    resp = app.wsgi_app(environ, sr)
    return resp


@upload.app.after_request
def policy_on_upload(resp):
    if request.path.startswith("/_save/"):
        resp.headers["Content-Security-Policy"] = POLICY
    return resp


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    print("rig on", port, flush=True)
    serve(wsgi, host="127.0.0.1", port=port, threads=8)
