#!/usr/bin/env python3
"""The listing and the media viewer render the reader's light/dark choice from
the zanni-scheme cookie that settings.kelliher.info sets, and nothing for any
other value, so a forged cookie cannot reach the markup. The listing is
rendered through the BUILT template (LISTER_TEMPLATE_BUILT), not the stub."""
import os
import unittest
from unittest.mock import Mock

from test_gateway import Base, L

L.PAGE_TEMPLATE = L.app.jinja_env.from_string(open(os.environ["LISTER_TEMPLATE_BUILT"], encoding="utf-8").read())


class Scheme(Base):
    def page(self, path, cookie):
        if cookie is None:
            self.c.delete_cookie("zanni-scheme", domain="files.kelliher.info")
        else:
            self.c.set_cookie("zanni-scheme", cookie, domain="files.kelliher.info")
        r = self.get(path)
        self.assertEqual(r.status_code, 200, r.data[:200])
        return r, r.get_data(as_text=True)

    def head(self, body):
        return body.split("<head>", 1)[0] if "<head>" in body else body.split("<meta", 1)[0]

    def test_listing_follows_the_os_without_a_cookie(self):
        self.grant_dad()
        r, body = self.page("/dad/", None)
        self.assertNotIn("data-scheme", self.head(body))
        self.assertIn("color-scheme: light dark", body)
        self.assertIn("Cookie", r.headers.get("Vary", ""))

    def test_listing_pins_light_and_dark(self):
        self.grant_dad()
        for s in ("light", "dark"):
            r, body = self.page("/dad/", s)
            self.assertIn(f'<html lang="en" data-scheme="{s}">', body)

    def test_listing_ignores_anything_else(self):
        self.grant_dad()
        for bad in ("system", "", '"><script>x</script>', "Dark"):
            r, body = self.page("/dad/", bad)
            self.assertNotIn("data-scheme=", self.head(body))
            self.assertNotIn("<script>x", body)

    def test_viewer_pins_and_ignores(self):
        self.grant_dad()
        original = L.s3.head_object
        L.s3.head_object = Mock(return_value={"ContentType": "audio/mpeg"})
        try:
            r, body = self.page("/_files/view?uri=%2Fdad%2Fnotes.mp3", "dark")
            self.assertIn('<html data-scheme="dark">', body)
            self.assertIn("Cookie", r.headers.get("Vary", ""))
            r, body = self.page("/_files/view?uri=%2Fdad%2Fnotes.mp3", "x")
            self.assertNotIn("data-scheme=", self.head(body))
        finally:
            L.s3.head_object = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
