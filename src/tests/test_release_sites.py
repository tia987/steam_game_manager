"""Release websites as store catalogues, end to end, against a local fake.

Nothing here touches the internet. A small HTTP server plays a release
blog (two pages of posts, with a news post mixed in) and three file
hosts that behave the ways real ones do:

* ``/h1/`` answers with a page whose script names the file's address;
* ``/h2/`` wants its download form submitted, after a short countdown,
  and only then redirects to the file - and only with its cookie;
* ``/h3/`` is down.

Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import http.server
import io
import os
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from pathlib import Path

TEMP = tempfile.mkdtemp(prefix="nsgm-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP           # keep settings out of the real profile
os.environ["APPDATA"] = TEMP

from nsgm import download, hosters, sitecatalog, store  # noqa: E402

FILES: dict[str, bytes] = {}


def _zip_with_setup() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Glacier Rookery/setup.exe", b"MZ" + b"\0" * 64)
        archive.writestr("Glacier Rookery/data-1.bin", b"x" * 1000)
    return buffer.getvalue()


def _zip_of(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


FILES["glacier.zip"] = _zip_with_setup()
FILES["aqueduct.part1.rar"] = b"Rar!part-one" * 500
FILES["aqueduct.part2.rar"] = b"Rar!part-two" * 500
FILES["optional-bonus-soundtrack.bin"] = b"music" * 100
FILES["selective-french.bin"] = b"fr" * 10
FILES["selective-english.bin"] = b"en" * 10


def post(slug: str, title: str, files: list[str], magnet: str = "", size: str = "1.1 GB") -> str:
    def block(prefix: str, label: str, style: str) -> str:
        links = "".join(
            f'<a href="{prefix(name)}" rel="nofollow">{name}</a><br>' for name in files)
        return (f'<li><a href="/paste/?0123456789abcdef#key">Filehoster: {label}</a> {style}'
                f'<div class="su-spoiler"><div class="su-spoiler-title">Click to show direct links'
                f'</div><div class="su-spoiler-content">{links}</div></div></li>')

    mirrors = (block(lambda n: f"/h3/{slug}/{n}", "DeadHost", "(down)")
               + block(lambda n: f"/h1/{slug}/{n}", "ScriptHost", "(fast)")
               + block(lambda n: f"/h2/{slug}#{n}", "FormHost", "(slow)"))
    torrent = (f'<h3>Download Mirrors (Torrent)</h3><ul><li><a href="/t/{slug}/">Tracker</a> | '
               f'[<a href="{magnet}">magnet</a>]</li></ul>') if magnet else ""
    return f"""
<article id="post-{slug}" class="post hentry">
  <header class="entry-header">
    <h1 class="entry-title"><a href="/{slug}/" rel="bookmark">{title}</a></h1>
    <time class="entry-date" datetime="2026-11-23T20:00:00+03:00">23/11/2026</time>
  </header>
  <div class="entry-content">
    <p><img class="alignleft" width="150" src="/covers/{slug}.jpg"><br>
    Genres/Tags: <a href="/tag/adventure/">Adventure</a>, <a href="/tag/3d/">3D</a><br>
    Company: <strong>Open Folk</strong><br>
    Languages: <strong>ENG/MULTI6</strong><br>
    Original Size: <strong>{size}</strong><br>
    Repack Size: <strong>600/700 MB</strong></p>
    <h3>Download Mirrors (Direct Links)</h3>
    <ul>{mirrors}</ul>
    {torrent}
    <h3>Screenshots (Click to enlarge)</h3>
    <p><a href="/shots/{slug}/1.jpg"><img src="/shots/{slug}/1.jpg" width="178" height="100"></a></p>
    <div class="su-spoiler"><div class="su-spoiler-title">Game Description</div>
      <div class="su-spoiler-content">A small open game about {slug}.<p>It is free.</p>
      <p><b>Game Features</b></p><ul><li>Feature one</li></ul></div></div>
  </div>
</article>"""


NEWS = """
<article id="post-news" class="post hentry">
  <h1 class="entry-title"><a href="/upcoming/" rel="bookmark">Upcoming Repacks</a></h1>
  <div class="entry-content"><p>Coming soon: lots.</p>
  <p><a href="/h1/patch/patch-1.0.1.rar">patch-1.0.1.rar</a></p></div>
</article>"""

MAGNET = ("magnet:?xt=urn:btih:88A7BA710CC76D8A08B30D7DA185845155809131"
          "&amp;dn=Glacier+Rookery&amp;tr=udp%3A%2F%2Ftracker.example%3A6969")

PAGES = {
    "/": "<html><head><title>Fake Releases – open games</title></head><body>"
         + NEWS
         + post("glacier", "Glacier Rookery – v1.5",
                ["glacier.zip", "optional-bonus-soundtrack.bin"], MAGNET)
         + '<a class="next page-numbers" href="/page/2/">Next</a></body></html>',
    "/page/2/": "<html><body>"
                + post("aqueduct", "Aqueduct 117: Civitas Nova – Gold Edition, v3.0.17 + 6 DLCs",
                       ["aqueduct.part1.rar", "aqueduct.part2.rar", "selective-french.bin",
                        "selective-english.bin"], size="20.5 GB")
                + "</body></html>",
}
PAGES["/glacier/"] = "<html><body>" + post(
    "glacier", "Glacier Rookery – v1.5", ["glacier.zip", "optional-bonus-soundtrack.bin"],
    MAGNET) + post("aqueduct", "Related: Aqueduct", ["aqueduct.part1.rar"]) + "</body></html>"
PAGES["/aqueduct/"] = PAGES["/page/2/"]
PAGES["/broken/"] = PAGES["/"].replace('href="/page/2/"', 'href="/nowhere/"')

HITS: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, kind: str = "text/html", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        HITS.append(path)
        if path in PAGES:
            return self.send(200, PAGES[path].encode())
        parts = path.strip("/").split("/")
        if parts[0] == "h3":
            return self.send(503, b"<html>down</html>")
        if parts[0] == "h1":
            name = parts[-1]
            page = (f'<html><body><button id="go">Download</button><script>'
                    f'document.getElementById("go").onclick=function(){{'
                    f'window.open("http://{self.headers["Host"]}/files/tok123/{name}")}};'
                    f'</script></body></html>')
            return self.send(200, page.encode())
        if parts[0] == "h2":
            page = ('<html><body><span id="countdown">1</span> seconds'
                    '<form method="post" action="/h2/go"><input type="hidden" name="op" value="download2">'
                    f'<input type="hidden" name="id" value="{parts[1]}">'
                    '<input type="submit" name="method_free" value="Free Download"></form></body></html>')
            return self.send(200, page.encode(), extra={"Set-Cookie": "visit=ok; Path=/"})
        if parts[0] == "files":
            name = urllib.parse.unquote(parts[-1])
            if name not in FILES:
                return self.send(404, b"no")
            data = FILES[name]
            start = 0
            ranged = self.headers.get("Range")
            if ranged and ranged.startswith("bytes="):
                start = int(ranged[6:].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])
            return
        self.send(404, b"<html>missing</html>")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        HITS.append("POST " + self.path)
        if self.path == "/h2/go" and "visit=ok" in (self.headers.get("Cookie") or "") \
                and fields.get("op") == ["download2"]:
            slug = fields["id"][0]
            name = {"glacier": "glacier.zip"}.get(slug, "")
            # The fragment named the file; the form only knows the id.
            name = name or self.server.wanted.get(slug, "")
            self.send_response(302)
            self.send_header("Location", f"/files/x/{name}")
            self.end_headers()
            return
        self.send(403, b"<html>no cookie</html>")


class ReleaseSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.wanted = {}
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_listing_reads_every_page_and_skips_news(self):
        found = sitecatalog.read_site(self.base + "/", pages=5)
        self.assertEqual(found["name"], "Fake Releases")
        names = [p["name"] for p in found["posts"]]
        self.assertEqual(names, ["Glacier Rookery", "Aqueduct 117: Civitas Nova – Gold Edition"])
        glacier, aqueduct = found["posts"]
        self.assertEqual(glacier["version"], "v1.5")
        self.assertEqual(aqueduct["version"], "v3.0.17")
        self.assertEqual(aqueduct["extras"], "+ 6 DLCs")
        self.assertEqual(aqueduct["art_name"], "Aqueduct 117: Civitas Nova")
        self.assertEqual(glacier["tags"], ["Adventure", "3D"])
        self.assertEqual(glacier["company"], "Open Folk")
        self.assertEqual(glacier["download_size"], 600 * 1024 ** 2)
        self.assertTrue(glacier["cover"].endswith("/covers/glacier.jpg"))
        self.assertIn("small open game", glacier["blurb"])
        self.assertNotIn("Feature one", glacier["blurb"])

    def test_a_later_page_failing_keeps_what_was_read(self):
        found = sitecatalog.read_site(self.base + "/broken/", pages=5)
        self.assertEqual([p["name"] for p in found["posts"]], ["Glacier Rookery"])

    def test_mirrors_files_and_torrents(self):
        release = sitecatalog.read_one(self.base + "/glacier/")
        self.assertEqual(release["name"], "Glacier Rookery")
        downloads = release["downloads"]
        self.assertEqual([m["label"] for m in downloads["mirrors"]],
                         ["DeadHost", "ScriptHost", "FormHost"])
        files = downloads["mirrors"][2]["files"]
        self.assertEqual([(f["name"], f["optional"]) for f in files],
                         [("glacier.zip", False), ("optional-bonus-soundtrack.bin", True)])
        self.assertEqual(len(downloads["torrents"]), 1)
        self.assertEqual(downloads["torrents"][0]["hash"], "88a7ba710cc76d8a08b30d7da185845155809131")
        # An encrypted paste and a screenshot are neither files nor mirrors.
        everything = [f["url"] for m in downloads["mirrors"] for f in m["files"]]
        self.assertFalse(any("/paste/" in u or "/shots/" in u for u in everything))

    def test_selective_languages(self):
        self.assertTrue(sitecatalog.is_optional("selective-french.bin"))
        self.assertFalse(sitecatalog.is_optional("selective-english.bin"))
        self.assertFalse(sitecatalog.is_optional("selective-french.bin", "french"))

    def test_hosts(self):
        found = hosters.resolve(f"{self.base}/h1/glacier/glacier.zip", "glacier.zip")
        self.assertTrue(found["url"].endswith("/files/tok123/glacier.zip"))
        found = hosters.resolve(f"{self.base}/h2/glacier#glacier.zip", "glacier.zip")
        self.assertTrue(found["url"].endswith("/files/x/glacier.zip"))
        self.assertIn("visit=ok", found["headers"].get("Cookie", ""))
        with self.assertRaises(hosters.HosterError):
            hosters.resolve(f"{self.base}/h3/glacier/glacier.zip", "glacier.zip")

    def test_source_box_accepts_the_site(self):
        checked = store.test_source(self.base + "/")
        self.assertEqual(checked["kind"], "site")
        self.assertEqual(checked["count"], 2)
        self.assertEqual(checked["pages"], 2)

    def _game(self, slug: str) -> dict:
        posts = sitecatalog.read_site(self.base + "/", pages=5)["posts"]
        chosen = next(p for p in posts if p["url"].endswith(f"/{slug}/"))
        game = store.site_game(chosen, "cat")
        self.assertTrue(store.installable(game))
        return game

    def test_release_downloads_from_working_mirrors_and_resumes(self):
        game = self._game("aqueduct")
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        job = store.StoreJob(id=game["id"], name=game["name"])
        settings = {"store_site_method": "direct"}
        target = store._download_release(job, game, game["release"]["mirrors"], folder, settings)
        names = sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
        self.assertEqual(names, ["aqueduct.part1.rar", "aqueduct.part2.rar",
                                 "selective-english.bin"])
        self.assertEqual((target / "aqueduct.part2.rar").read_bytes(), FILES["aqueduct.part2.rar"])

        # A second attempt finds everything done and fetches nothing.
        HITS.clear()
        store._download_release(job, game, game["release"]["mirrors"], folder, settings)
        self.assertFalse([h for h in HITS if h.startswith("/files/")])

        # A file cut off half way is continued, not restarted.
        (target / "aqueduct.part1.rar").unlink()
        partial = target / "aqueduct.part1.rar.part"
        partial.write_bytes(FILES["aqueduct.part1.rar"][:1000])
        store._download_release(job, game, game["release"]["mirrors"], folder, settings)
        self.assertEqual((target / "aqueduct.part1.rar").read_bytes(), FILES["aqueduct.part1.rar"])

    def test_archive_with_setup_is_unpacked_before_installing(self):
        game = self._game("glacier")
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        job = store.StoreJob(id=game["id"], name=game["name"])
        settings = {}
        result, shared = store._fetch_release(job, game, settings, folder)
        self.assertIsNone(shared)
        ready = Path(result["path"])
        self.assertEqual(ready.name, store.UNPACKED)
        self.assertTrue((ready / "setup.exe").is_file())
        self.assertFalse((Path(result["tidy"]) / "glacier.zip").exists())
        self.assertFalse((Path(result["tidy"]) / "optional-bonus-soundtrack.bin").exists())
        self.assertFalse((ready / "optional-bonus-soundtrack.bin").exists())
        # Asked again, it remembers it has already done all of that.
        HITS.clear()
        again, _ = store._fetch_release(job, game, settings, folder)
        self.assertEqual(again["path"], result["path"])
        self.assertFalse([h for h in HITS if h.startswith("/files/")])

    def test_loose_files_join_the_setup_program(self):
        target = Path(tempfile.mkdtemp(dir=TEMP)) / "Game"
        target.mkdir()
        (target / "aa-optional-videos.zip").write_bytes(_zip_of({"videos/intro.bik": b"v"}))
        (target / "game.zip").write_bytes(_zip_of({"Game/setup.exe": b"MZ", "Game/data-1.bin": b"d"}))
        (target / "selective-english.bin").write_bytes(b"en")
        job = store.StoreJob(id="x", name="Game")
        ready = store._prepare_release(job, target, {})
        self.assertTrue((ready / "setup.exe").is_file())        # the release, not the extra
        self.assertTrue((ready / "selective-english.bin").is_file())
        self.assertTrue((ready / "videos" / "intro.bik").is_file())
        self.assertEqual(sorted(p.name for p in target.iterdir()), [store.UNPACKED])

    def test_fixed_names_keep_volume_names(self):
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        (folder / "a.part1.rar").write_bytes(b"old")
        got = download.download(f"{self.base}/files/t/aqueduct.part1.rar", folder,
                                name="a.part1.rar")
        self.assertEqual(Path(got["path"]).name, "a.part1.rar")
        self.assertEqual(download.clean_file_name("../x?.rar"), "x_.rar")


if __name__ == "__main__":
    unittest.main()
