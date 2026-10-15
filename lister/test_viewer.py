#!/usr/bin/env python3
from test_gateway import Base,L
import unittest
from unittest.mock import Mock

class Viewer(Base):
    def test_viewer_reads_metadata_but_not_object_bytes(self):
        self.grant_dad()
        original=L.s3.head_object
        L.s3.head_object=Mock(return_value={'ContentType':'audio/mpeg'})
        try:
            r=self.get('/_files/view?uri=%2Fdad%2Fnotes.mp3')
            self.assertEqual(r.status_code,200)
            self.assertIn(b'<audio controls',r.data)
            self.assertIn(b'src="/dad/notes.mp3"',r.data)
            self.assertNotIn(b'X-Amz-',r.data)
            L.s3.head_object.assert_called_once_with(Bucket='files',Key='dad/notes.mp3')
        finally:L.s3.head_object=original

    def test_no_identity_and_no_read_fail_before_metadata(self):
        original=L.s3.head_object
        L.s3.head_object=Mock(side_effect=AssertionError('must not inspect denied object'))
        try:
            self.assertEqual(self.get('/_files/view?uri=%2Fdad%2Fnotes.mp3',user=None).status_code,401)
            self.assertEqual(self.get('/_files/view?uri=%2Fdad%2Fnotes.mp3').status_code,403)
            L.s3.head_object.assert_not_called()
        finally:L.s3.head_object=original

    def test_active_types_are_not_embedded(self):
        self.grant_dad()
        original=L.s3.head_object
        try:
            for mime in ['text/html','image/svg+xml','application/pdf','audio/mpeg; text/html','audio/x-unknown','video/mp4 ']:
                L.s3.head_object=Mock(return_value={'ContentType':mime})
                r=self.get('/_files/view?uri=%2Fdad%2Fx.mp3')
                self.assertEqual(r.status_code,200,mime)
                self.assertNotIn(b'<audio',r.data)
                self.assertNotIn(b'<video',r.data)
                self.assertIn(b'?download=1" download>Download</a>',r.data)
        finally:L.s3.head_object=original

    def test_every_player_type_gets_a_player(self):
        self.grant_dad()
        original=L.s3.head_object
        try:
            for mime, tag in [('audio/ogg',b'<audio controls'),('video/webm',b'<video controls'),('video/quicktime',b'<video controls')]:
                L.s3.head_object=Mock(return_value={'ContentType':mime})
                r=self.get('/_files/view?uri=%2Fdad%2Fnotes.mp3')
                self.assertEqual(r.status_code,200,mime)
                self.assertIn(tag,r.data,mime)
        finally:L.s3.head_object=original

    def test_hostile_title_and_src_are_escaped(self):
        self.grant_dad()
        original=L.s3.head_object
        L.s3.head_object=Mock(return_value={'ContentType':'audio/mpeg'})
        try:
            r=self.get('/_files/view?uri=%2Fdad%2F%2522%253E%253Cscript%253Eevil%25C3%25A9.mp3')
            self.assertEqual(r.status_code,200)
            self.assertNotIn(b'<script>',r.data)
            self.assertIn(b'&lt;script&gt;',r.data)
            self.assertIn(b'src="/dad/%22%3E%3Cscript%3Eevil%C3%A9.mp3"',r.data)
        finally:L.s3.head_object=original

    def test_newline_in_name_is_rejected(self):
        self.grant_dad()
        self.assertEqual(self.get('/_files/view?uri=%2Fdad%2F%250Aevil.mp3').status_code,403)

if __name__=='__main__':unittest.main(verbosity=2)
