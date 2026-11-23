"""Release websites are read once, then only for what is new.

A local fake blog with many pages, newest first, that can publish new
posts and fail on purpose. Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import http.server
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-sync-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import library, sitesync, store  # noqa: E402

PER_PAGE = 10


class Blog:
    def __init__(self, count: int):
        # Newest first, like the real thing.
        self.posts = [self.make(n) for n in range(count, 0, -1)]
        self.hits: list[str] = []
        self.broken: set = set()

    @staticmethod
    def make(n: int, title: str = "") -> dict:
        return {"slug": f"game-{n}", "title": title or f"Game Number {n} – v1.{n}",
                "date": f"2026-{1 + n // 300:02d}-{1 + (n // 10) % 28:02d}"}

    def publish(self, n: int) -> None:
        self.posts.insert(0, self.make(n))

    def pages(self) -> int:
        return max(1, -(-len(self.posts) // PER_PAGE))

    def page(self, number: int) -> str:
        chunk = self.posts[(number - 1) * PER_PAGE: number * PER_PAGE]
        articles = "".join(f"""
<article class="post hentry"><h2 class="entry-title"><a href="/{p['slug']}/" rel="bookmark">{p['title']}</a></h2>
<time datetime="{p['date']}T10:00:00+00:00"></time>
<div class="entry-content"><p>Original Size: <strong>1 GB</strong><br>Repack Size: <strong>500 MB</strong></p>
<h3>Download Mirrors (Torrent)</h3><ul><li><a href="magnet:?xt=urn:btih:{'%040x' % (hash(p['slug']) & (2**160 - 1))}">magnet</a></li></ul>
</div></article>""" for p in chunk)
        links = "".join(f'<a class="page-numbers" href="/page/{n}/">{n}</a>'
                        for n in (1, 2, self.pages()))
        following = (f'<a class="next page-numbers" href="/page/{number + 1}/">Next</a>'
                     if number < self.pages() else "")
        return (f"<html><head><title>Fake Blog – games</title></head><body>{articles}"
                f"{links}{following}</body></html>")


BLOG = Blog(95)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        BLOG.hits.append(path)
        number = 1
        if path.startswith("/page/"):
            number = int(path.strip("/").split("/")[1])
        elif path != "/":
            self.send_response(404)
            self.end_headers()
            return
        if number in BLOG.broken or number > BLOG.pages():
            self.send_response(500 if number in BLOG.broken else 404)
            self.end_headers()
            return
        body = BLOG.page(number).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_idle(cid: str, limit: float = 30) -> None:
    deadline = time.time() + limit
    while sitesync.progress(cid) and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)


class SiteSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.source = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.patches = [mock.patch.object(sitesync, "PAGE_DELAY", 0),
                       mock.patch.object(sitesync, "RETRY_WAITS", (0.01,))]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.server.shutdown()

    def setUp(self):
        global BLOG
        BLOG.__init__(95)
        self.cid = f"c{time.time_ns() % 10**9}"

    def run_sync(self, limit: int, force: bool = True):
        started = sitesync.ensure(self.cid, self.source, limit,
                                  lambda post: store.site_game(post, self.cid), force=force)
        wait_idle(self.cid)
        return started

    def state(self) -> dict:
        return sitesync.load(self.cid, self.source)

    def test_whole_site_once_then_only_the_new(self):
        self.run_sync(sitesync.ALL_PAGES)
        state = self.state()
        self.assertTrue(state["complete"])
        self.assertEqual(len(state["games"]), 95)
        self.assertEqual(state["total_pages"], 10)
        self.assertEqual(state["games"][0]["name"], "Game Number 95")

        # Three new releases: one look at the front page is enough.
        for n in (96, 97, 98):
            BLOG.publish(n)
        BLOG.hits.clear()
        self.run_sync(sitesync.ALL_PAGES)
        self.assertEqual(BLOG.hits, ["/"])
        names = [g["name"] for g in self.state()["games"]]
        self.assertEqual(names[:4], ["Game Number 98", "Game Number 97", "Game Number 96",
                                     "Game Number 95"])
        self.assertEqual(len(names), 98)

        # Nothing new, and checked recently: not even the front page.
        BLOG.hits.clear()
        self.assertFalse(self.run_sync(sitesync.ALL_PAGES, force=False))
        self.assertEqual(BLOG.hits, [])

    def test_more_new_than_a_page_reads_on_until_it_meets_known_ones(self):
        self.run_sync(sitesync.ALL_PAGES)
        for n in range(96, 96 + 23):
            BLOG.publish(n)
        BLOG.hits.clear()
        self.run_sync(sitesync.ALL_PAGES)
        self.assertEqual(BLOG.hits, ["/", "/page/2/", "/page/3/"])
        self.assertEqual(len(self.state()["games"]), 95 + 23)

    def test_depth_limit_then_deeper_carries_on(self):
        self.run_sync(3)
        state = self.state()
        self.assertEqual((state["pages_read"], state["complete"], len(state["games"])),
                         (3, False, 30))
        BLOG.hits.clear()
        self.run_sync(sitesync.ALL_PAGES)
        self.assertEqual(BLOG.hits[0], "/")                      # new releases first
        self.assertNotIn("/page/2/", BLOG.hits)                  # pages already read are skipped
        self.assertIn("/page/4/", BLOG.hits)
        self.assertEqual(len(self.state()["games"]), 95)

    def test_interrupted_read_resumes_where_it_stopped(self):
        BLOG.broken = {6}
        self.run_sync(sitesync.ALL_PAGES)
        state = self.state()
        self.assertIn("HTTP 500", state["error"])
        self.assertEqual(state["pages_read"], 5)
        self.assertEqual(len(state["games"]), 50)
        BLOG.broken = set()
        BLOG.hits.clear()
        self.run_sync(sitesync.ALL_PAGES)
        self.assertNotIn("/page/5/", BLOG.hits)
        self.assertIn("/page/6/", BLOG.hits)
        self.assertTrue(self.state()["complete"])
        self.assertEqual(self.state()["error"], "")
        self.assertEqual(len(self.state()["games"]), 95)

    def test_an_updated_post_is_refreshed(self):
        self.run_sync(sitesync.ALL_PAGES)
        BLOG.posts[1] = Blog.make(94, title="Game Number 94 – v2.0")
        BLOG.publish(96)
        self.run_sync(sitesync.ALL_PAGES)
        game = next(g for g in self.state()["games"] if g["name"] == "Game Number 94")
        self.assertEqual(game["version"], "v2.0")
        self.assertEqual(sum(g["name"] == "Game Number 94" for g in self.state()["games"]), 1)

    def test_store_opens_at_once_and_the_shelf_fills_behind_it(self):
        library.save_settings({"store_catalogues": [{"id": self.cid, "source": self.source}],
                               "store_site_pages": 0})
        started = time.time()
        first = next(c for c in store.load_catalogues() if c["id"] == self.cid)
        self.assertLess(time.time() - started, 5)
        self.assertEqual(first["kind"], "site")
        self.assertEqual(len(first["games"]), 10)                # the front page, straight away
        wait_idle(self.cid)
        later = next(c for c in store.load_catalogues() if c["id"] == self.cid)
        self.assertEqual(len(later["games"]), 95)
        self.assertTrue(later["sync"]["complete"])
        # Removing the catalogue removes what was kept for it.
        store.save_store_settings({"catalogues": []})
        self.assertFalse(sitesync.has_state(self.cid))

    def test_reading_everything_again_refreshes_and_drops_what_is_gone(self):
        self.run_sync(sitesync.ALL_PAGES)
        BLOG.posts[80] = dict(BLOG.posts[80], title="Game Number 15 – v9.9")   # deep in the site
        removed = BLOG.posts.pop(50)
        sitesync.rescan(self.cid, self.source)
        shown_meanwhile = len(self.state()["games"])
        self.run_sync(sitesync.ALL_PAGES, force=True)
        games = self.state()["games"]
        self.assertEqual(shown_meanwhile, 95)                 # the shelf stayed while it read
        self.assertEqual(next(g for g in games if g["name"] == "Game Number 15")["version"], "v9.9")
        self.assertFalse(any(g["page"].rstrip("/").endswith(removed["slug"]) for g in games))
        self.assertEqual(len(games), 94)
        self.assertFalse(self.state()["rescan"])

    def test_page_depth_setting(self):
        self.assertEqual(store._site_pages({}), store.SITE_PAGES_DEFAULT)
        self.assertEqual(store._site_pages({"store_site_pages": 0}), sitesync.ALL_PAGES)
        self.assertEqual(store._site_pages({"store_site_pages": 825}), 825)


if __name__ == "__main__":
    unittest.main()
