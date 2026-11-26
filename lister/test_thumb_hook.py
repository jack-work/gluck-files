#!/usr/bin/env python3
"""The upload hook: a finished upload gets its thumb, and the upload never waits for it."""

import os
import sys
import tempfile
import threading
import unittest

TMP = tempfile.mkdtemp()
os.environ.update({
    "UPLOAD_ACL_DB": os.path.join(TMP, "acl.db"), "UPLOAD_ORIGIN": "https://files.kelliher.info",
    "UPLOAD_PAGE_DIR": TMP, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_EC2_METADATA_DISABLED": "true",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload  # noqa: E402
import thumbs  # noqa: E402
from test_thumbs import Bucket, jpeg_bytes, opened  # noqa: E402


class Multipart(Bucket):
    def __init__(self):
        super().__init__()
        self.parts = {}

    def create_multipart_upload(self, Bucket, Key, **_):
        self.parts["u1"] = {}
        return {"UploadId": "u1"}

    def list_parts(self, Bucket, Key, UploadId, **_):
        if UploadId not in self.parts:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        return {"Parts": [{"PartNumber": n, "ETag": f'"{n}"', "Size": len(d)} for n, d in sorted(self.parts[UploadId].items())],
                "IsTruncated": False}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.parts[UploadId][PartNumber] = Body
        return {"ETag": f'"{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.put_source(Key, b"".join(self.parts[UploadId][p["PartNumber"]] for p in MultipartUpload["Parts"]))


class Hook(unittest.TestCase):
    def setUp(self):
        self.bucket = Multipart()
        self.original_place = upload.place
        upload.place = lambda key, ensure=False: (self.bucket, "files", key)
        for g in upload.ACL.list_grants():
            upload.ACL.revoke(g["subject"], g["path"], g["perm"])
        upload.ACL.grant("user:pilar", "users/pilar/", "write", "fixture")
        upload.app.config["TESTING"] = True
        self.http = upload.app.test_client()
        self.made = []
        self.gate = threading.Event()
        self.gate.set()
        self.original_make = upload.THUMBS.make

        def make(client, bucket, inner):
            self.gate.wait(10)
            result = self.original_make(client, bucket, inner)
            self.made.append((inner, result))
            return result

        upload.THUMBS.make = make

    def tearDown(self):
        upload.THUMBS.drain()
        upload.place = self.original_place
        upload.THUMBS.make = self.original_make

    def post(self, path, **body):
        return self.http.post(path, json=body, headers={"Remote-User": "pilar", "Origin": "https://files.kelliher.info"})

    def upload_file(self, name, data):
        started = self.post("/_upload/api/start", name=name, size=len(data)).get_json()
        put = self.http.put(f"/_upload/api/part?key={started['key']}&id={started['id']}&n=1", data=data,
                            headers={"Remote-User": "pilar", "Origin": "https://files.kelliher.info"})
        self.assertEqual(put.status_code, 200)
        return started["key"], self.post("/_upload/api/complete", key=started["key"], id=started["id"], size=len(data))

    def test_a_finished_photo_gets_its_thumb(self):
        data = jpeg_bytes((1600, 1200))
        key, done = self.upload_file("20260101_120000.jpg", data)
        self.assertEqual(done.status_code, 200)
        upload.THUMBS.drain()
        self.assertEqual(self.made, [(key, "made")])
        self.assertEqual(opened(self.bucket.objects[".thumbs/" + key + ".jpg"]).size, (320, 240))

    def test_the_upload_does_not_wait_for_the_thumb(self):
        self.gate.clear()
        key, done = self.upload_file("20260101_120001.jpg", jpeg_bytes((800, 600)))
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.get_json(), {"key": key, "size": done.get_json()["size"]})
        self.assertNotIn(".thumbs/" + key + ".jpg", self.bucket.objects)
        self.gate.set()
        upload.THUMBS.drain()
        self.assertIn(".thumbs/" + key + ".jpg", self.bucket.objects)

    def test_a_broken_thumb_job_leaves_the_upload_complete(self):
        key, done = self.upload_file("20260101_120002.jpg", b"\xff\xd8\xff not really")
        self.assertEqual(done.status_code, 200)
        upload.THUMBS.drain()
        self.assertIn(key, self.bucket.objects)

    def test_a_queue_that_cannot_take_the_job_does_not_fail_the_upload(self):
        def explode(key):
            raise RuntimeError("queue gone")

        original = upload.THUMBS.submit
        upload.THUMBS.submit = explode
        try:
            key, done = self.upload_file("20260101_120003.jpg", jpeg_bytes((800, 600)))
        finally:
            upload.THUMBS.submit = original
        self.assertEqual(done.status_code, 200)

    def test_a_document_is_not_queued(self):
        key, done = self.upload_file("scan.pdf", b"%PDF-1.4 fake")
        self.assertEqual(done.status_code, 200)
        upload.THUMBS.drain()
        self.assertEqual(self.made, [])

    def test_a_refused_completion_queues_nothing(self):
        data = jpeg_bytes((800, 600))
        started = self.post("/_upload/api/start", name="a.jpg", size=len(data)).get_json()
        done = self.post("/_upload/api/complete", key=started["key"], id=started["id"], size=len(data))
        self.assertEqual(done.status_code, 409)
        upload.THUMBS.drain()
        self.assertEqual(self.made, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
