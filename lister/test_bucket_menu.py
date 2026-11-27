#!/usr/bin/env python3
"""The bucket menu in the listing: what it offers, where it links, what it marks.

The ACL is real sqlite, Garage is the gateway test's stand-in, and the listing is
rendered through the BUILT template (LISTER_TEMPLATE_BUILT), or the source
template when run by hand.
"""

import os
import unittest
from html.parser import HTMLParser

from test_bucket_api import ADMIN, Asking
from test_gateway import OBJECTS, L

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.environ.get("LISTER_TEMPLATE_BUILT") or os.path.join(HERE, "template.html.in")

CAMERA = (
    "users/pilar/camera/20240512_101010.jpg",
    "users/pilar/camera/20240513_101010.jpg",
    "users/ctkelliher/camera/20240601_090000.jpg",
    "users/admin/notes/todo.txt",
    "photos/2024/20240101_120000.jpg",
    "users/xavier/photos/20240101_100000.jpg",
    "users/xavier/docs/a.txt",
)


class Menu(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.links, self.chip, self.current, self.details, self.scripts = [], None, [], 0, 0
        self._in_menu = False
        self._in_nav = False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.scripts += 1
        if tag == "nav" and a.get("aria-label") == "Buckets":
            self._in_nav = True
        if tag == "details" and self._in_nav:
            self.details += 1
            self._in_menu = True
        if tag == "a" and self._in_menu:
            self.links.append((a.get("data-bucket"), a.get("href")))
            if a.get("aria-current") == "page":
                self.current.append(a.get("data-bucket"))
        elif tag == "a" and self._in_nav:
            self.chip = (a.get("href"), a.get("aria-current"), a.get("class"))

    def handle_endtag(self, tag):
        if tag == "details":
            self._in_menu = False
        if tag == "nav":
            self._in_nav = False


class InTheListing(Asking):
    def setUp(self):
        super().setUp()
        self.template = L.PAGE_TEMPLATE
        with open(TEMPLATE, encoding="utf-8") as fh:
            L.PAGE_TEMPLATE = L.app.jinja_env.from_string(fh.read())
        for key in CAMERA:
            OBJECTS[key] = 1000
        L.ACL.grant("user:pilar", "users/pilar/", "read", "t")
        L.ACL.grant("user:pilar", "users/pilar/", "list", "t")
        for p in ("read", "list", "write"):
            L.ACL.grant("group:files-admin", "", p, "t")
            L.ACL.grant("user:admin", "users/admin/", p, "t")
            L.ACL.grant("user:admin", "users/ctkelliher/", p, "t")
        L.ACL.grant("user:dad", "dad/", "read", "t")
        L.ACL.grant("user:dad", "dad/", "list", "t")

    def tearDown(self):
        L.PAGE_TEMPLATE = self.template
        for key in CAMERA:
            OBJECTS.pop(key, None)
        super().tearDown()

    def page(self, path, user, groups=""):
        r = self.ask(path, user, groups)
        self.assertEqual(r.status_code, 200, (path, user, r.data[:200]))
        return r.get_data(as_text=True), Menu(r.get_data(as_text=True))

    def test_the_menu_lists_every_bucket_with_a_link_that_is_a_listing_url(self):
        _, m = self.page("/users/admin/", *ADMIN)
        self.assertEqual(m.details, 1)
        self.assertEqual(m.links, [("home", "/users/admin/"), ("ctkelliher", "/users/ctkelliher/"), ("shared", "/")])

    def test_the_home_bucket_is_in_the_nav_by_default_wherever_you_are(self):
        for path in ("/", "/users/ctkelliher/", "/users/ctkelliher/camera/", "/photos/", "/users/admin/notes/"):
            _, m = self.page(path, *ADMIN)
            self.assertEqual(m.chip[0], "/users/admin/", path)

    def test_the_bucket_you_are_in_is_marked_and_only_that_one(self):
        for path, want in (("/users/admin/", "home"), ("/users/admin/notes/", "home"), ("/", "shared"), ("/photos/", "shared"),
                           ("/users/ctkelliher/", "ctkelliher"), ("/users/ctkelliher/camera/", "ctkelliher")):
            _, m = self.page(path, *ADMIN)
            self.assertEqual(m.current, [want], path)
            self.assertEqual(m.chip[1], "page" if want == "home" else None, path)

    def test_every_link_in_the_menu_opens_for_the_one_who_sees_it(self):
        for user, groups in (ADMIN, ("pilar", ""), ("dad", "")):
            _, m = self.page(L.buckets_for(user, [g for g in groups.split(",") if g])[0]["root_url"], user, groups)
            for id, href in m.links:
                r = self.ask(href, user, groups)
                self.assertEqual(r.status_code, 200, (user, id, href))
                self.assertIn(b"aria-current=\"page\"", r.data)

    def test_the_menu_and_the_api_offer_the_same_urls(self):
        for user, groups in (ADMIN, ("pilar", ""), ("dad", "")):
            api = self.ask("/_files/api/buckets", user, groups).get_json()["buckets"]
            _, m = self.page(api[0]["root_url"], user, groups)
            offered = [h for _, h in m.links] or [m.chip[0]]
            self.assertEqual(offered, [b["root_url"] for b in api])

    def test_a_home_only_user_sees_her_home_and_no_menu_to_choose_from(self):
        body, m = self.page("/users/pilar/", "pilar")
        self.assertEqual((m.details, m.links, m.chip[0], m.chip[1]), (0, [], "/users/pilar/", "page"))
        self.assertIn(">Your files</a>", body)

    def test_a_root_home_marks_itself_everywhere_but_inside_a_granted_home(self):
        L.ACL.revoke("user:admin", "users/admin/")
        for path, want in (("/", "home"), ("/photos/", "home"),
                           ("/users/ctkelliher/", "ctkelliher")):
            body, m = self.page(path, *ADMIN)
            self.assertEqual(m.current, [want], path)
            self.assertNotIn(">Shared<", body)
        _, m = self.page("/", *ADMIN)
        self.assertEqual(m.links, [("home", "/"), ("ctkelliher", "/users/ctkelliher/")])
        self.assertEqual(m.chip[0], "/")

    def test_a_shared_only_user_gets_the_shared_tree_as_the_default(self):
        _, m = self.page("/dad/", "dad")
        self.assertEqual((m.details, m.chip[0], m.chip[1]), (0, "/", "page"))

    def test_rows_only_below_a_home_open_a_listing_of_those_folders_from_the_root_url(self):
        L.ACL.grant("user:friend", "users/xavier/photos/", "read", "t")
        L.ACL.grant("user:friend", "users/xavier/photos/", "list", "t")
        L.ACL.grant("user:friend", "users/xavier/docs/", "list", "t")
        found = L.buckets_for("friend", [])
        self.assertEqual([(b["id"], b["root_url"]) for b in found], [("xavier", "/users/xavier/")])
        r = self.ask("/users/xavier/", "friend", "")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn('href="/users/xavier/photos/"', page)
        self.assertIn('href="/users/xavier/docs/"', page)
        self.assertNotIn('href="/users/xavier/other/"', page)
        for folder in ("photos", "docs"):
            self.assertEqual(self.ask(f"/users/xavier/{folder}/", "friend", "").status_code, 200, folder)
        self.assertEqual(self.ask("/users/xavier/secret/", "friend", "").status_code, 404)
        _, m = self.page("/users/xavier/", "friend")
        self.assertEqual((m.details, m.chip[0], m.chip[1]), (0, "/users/xavier/", "page"))

    def test_nobody_is_shown_a_bucket_they_hold_no_row_for(self):
        body, m = self.page("/users/pilar/", "pilar")
        for gone in ("ctkelliher", "/users/admin/", "Shared", "home-"):
            self.assertNotIn(gone, body.split("<nav class=\"p-crumbs\"")[0].split("<main")[1], gone)
        body, m = self.page("/dad/", "dad")
        self.assertNotIn("/users/", body.split("<nav class=\"p-crumbs\"")[0].split("<main")[1])

    def test_a_listing_a_caller_may_not_open_stays_a_404_with_no_menu(self):
        for path in ("/users/ctkelliher/", "/users/admin/", "/wfh/"):
            r = self.ask(path, "pilar", "")
            self.assertEqual(r.status_code, 404, path)
            self.assertNotIn(b"Buckets", r.data)

    def test_the_page_stays_script_free_and_names_no_garage_bucket(self):
        for user, groups in (ADMIN, ("pilar", "")):
            body, m = self.page("/users/admin/" if user == "admin" else "/users/pilar/", user, groups)
            self.assertEqual(m.scripts, 0)
            self.assertNotIn("u-admin", body)
            self.assertNotIn("u-pilar", body)
            self.assertNotIn("u-ctkelliher", body)

    def test_a_label_that_is_markup_cannot_reach_the_page(self):
        saved = L.buckets_for
        L.buckets_for = lambda user, groups: [
            {"id": "home", "label": "<b onmouseover=x>Your files</b>", "root_url": "/users/pilar/\"><i>", "access": ["read"], "kind": "home"},
            {"id": "shared", "label": "Sha\"red", "root_url": "/", "access": ["read"], "kind": "shared"}]
        try:
            body, m = self.page("/users/pilar/", "pilar")
        finally:
            L.buckets_for = saved
        nav = body.split("<main")[1].split("</nav>")[0]
        self.assertNotIn("<b onmouseover", nav)
        self.assertNotIn("<i>", nav)


if __name__ == "__main__":
    unittest.main(verbosity=2)
