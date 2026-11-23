"""The built-in downloader, against a fake aria2 that speaks its JSON-RPC.

The fake follows aria2's documented behaviour for a magnet started with
--pause-metadata: a details stage, then the download itself added paused,
files chosen with changeOption("select-file"), then unpaused. Run from the
``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-engine-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import store, torrentclient, torrentengine  # noqa: E402

HASH = "88a7ba710cc76d8a08b30d7da185845155809131"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Glacier+Rookery"
NAMES = ["setup.exe", "fg-01.bin", "fg-optional-bonus-soundtrack.bin", "fg-selective-french.bin"]
SECRET = "s3cret"


class FakeAria2:
    def __init__(self):
        self.entries: dict = {}
        self.calls: list = []
        self.ticks = 0
        self.stalled = False

    def handle(self, method: str, params: list):
        if params[:1] != [f"token:{SECRET}"]:
            raise PermissionError("Unauthorized")
        params = params[1:]
        self.calls.append((method, params))
        self.advance()
        if method == "aria2.getVersion":
            return {"version": "1.37.0"}
        if method == "aria2.addUri":
            gid = "meta0001"
            self.entries[gid] = {"gid": gid, "status": "active", "infoHash": HASH,
                                 "dir": params[1]["dir"], "totalLength": "0",
                                 "completedLength": "0", "downloadSpeed": "0", "connections": "3",
                                 "files": [{"index": "1", "path": "[METADATA]Glacier", "length": "0"}],
                                 "bittorrent": {}, "age": 0}
            return gid
        if method in ("aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"):
            wanted = {"aria2.tellActive": ("active",), "aria2.tellWaiting": ("waiting", "paused"),
                      "aria2.tellStopped": ("complete", "error", "removed")}[method]
            return [dict(e) for e in self.entries.values() if e["status"] in wanted]
        if method == "aria2.getFiles":
            return self.entries[params[0]]["files"]
        if method == "aria2.changeOption":
            entry = self.entries[params[0]]
            if entry["status"] not in ("paused", "waiting"):
                raise ValueError("select-file can only change while paused")
            chosen = set(params[1]["select-file"].split(","))
            for item in entry["files"]:
                item["selected"] = "true" if item["index"] in chosen else "false"
            return "OK"
        if method == "aria2.unpause":
            self.entries[params[0]]["status"] = "active"
            return params[0]
        if method == "aria2.forceRemove":
            self.entries[params[0]]["status"] = "removed"
            return params[0]
        if method == "aria2.removeDownloadResult":
            self.entries.pop(params[0], None)
            return "OK"
        raise ValueError(f"unknown method {method}")

    def advance(self):
        """Time passes a little on every call, as it would."""
        for gid, entry in list(self.entries.items()):
            entry["age"] = entry.get("age", 0) + 1
            if gid == "meta0001" and entry["status"] == "active" and entry["age"] > 3:
                entry["status"] = "complete"
                entry["followedBy"] = ["real0001"]
                folder = entry["dir"]
                self.entries["real0001"] = {
                    "gid": "real0001", "status": "paused", "infoHash": HASH, "dir": folder,
                    "totalLength": "400", "completedLength": "0", "downloadSpeed": "0",
                    "connections": "0", "following": "meta0001",
                    "bittorrent": {"info": {"name": "Glacier Rookery"}},
                    "files": [{"index": str(i + 1), "length": "100", "selected": "true",
                               "path": os.path.join(folder, "Glacier Rookery", n)}
                              for i, n in enumerate(NAMES)], "age": 0}
            if gid == "real0001" and entry["status"] == "active" and not self.stalled:
                done = int(entry["completedLength"]) + 100
                entry.update(completedLength=str(min(done, 400)), downloadSpeed="1000",
                             connections="5")
                if done >= 400:
                    entry["status"] = "complete"
                    for item in entry["files"]:
                        if item.get("selected") != "false":
                            Path(item["path"]).parent.mkdir(parents=True, exist_ok=True)
                            Path(item["path"]).write_bytes(b"MZ data")


FAKE = FakeAria2()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            answer = {"id": request["id"], "jsonrpc": "2.0",
                      "result": FAKE.handle(request["method"], request["params"])}
            code = 200
        except Exception as exc:
            answer = {"id": request["id"], "jsonrpc": "2.0",
                      "error": {"code": 1, "message": str(exc)}}
            code = 400
        body = json.dumps(answer).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BuiltInEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FAKE.__init__()
        self.folder = Path(tempfile.mkdtemp(dir=TEMP))
        self.client = torrentclient.Aria2(self.port, SECRET)
        store._change_state(lambda state: state.__setitem__("torrents", {}))

    def test_wrong_secret_is_refused(self):
        with self.assertRaises(torrentclient.TorrentClientError):
            torrentclient.Aria2(self.port, "wrong").version()

    def test_magnet_downloads_without_optional_files_and_never_seeds(self):
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        skip = lambda name: "optional" in name or "selective-french" in name  # noqa: E731
        with mock.patch.object(store, "torrent_client", lambda *a, **k: self.client), \
                mock.patch.object(store, "POLL_SECONDS", 0.001):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False,
                                          skip=skip)
        changed = [p for m, p in FAKE.calls if m == "aria2.changeOption"]
        self.assertEqual(changed, [["real0001", {"select-file": "1,2"}]])
        # Chosen while paused, and only then let go.
        order = [m for m, _ in FAKE.calls]
        self.assertLess(order.index("aria2.changeOption"), order.index("aria2.unpause"))
        self.assertTrue((got.path / "setup.exe").is_file())
        self.assertFalse((got.path / "fg-optional-bonus-soundtrack.bin").exists())
        self.assertTrue(got.ours)
        self.assertTrue(got.client.leech_only)

    def test_nothing_to_leave_out_still_starts(self):
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with mock.patch.object(store, "torrent_client", lambda *a, **k: self.client), \
                mock.patch.object(store, "POLL_SECONDS", 0.001):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        self.assertNotIn("aria2.changeOption", [m for m, _ in FAKE.calls])
        self.assertTrue((got.path / "fg-optional-bonus-soundtrack.bin").is_file())

    def test_cancel_removes_the_download_and_its_files(self):
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        self.client.add(magnet=MAGNET, save_path=str(self.folder))
        for _ in range(6):
            self.client.status([HASH])           # until the download itself exists
        partial = self.folder / "Glacier Rookery" / "setup.exe"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"half")
        store._own_torrent(HASH, gid="g1")
        FAKE.stalled = True
        threading.Timer(0.05, lambda: setattr(job, "cancel", True)).start()
        with mock.patch.object(store, "torrent_client", lambda *a, **k: self.client), \
                mock.patch.object(store, "POLL_SECONDS", 0.01):
            with self.assertRaises(store.Stopped):
                store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        self.assertFalse((self.folder / "Glacier Rookery").exists())
        self.assertEqual(FAKE.entries, {})

    def test_deleting_never_leaves_the_download_folder(self):
        outside = Path(tempfile.mkdtemp(dir=TEMP)) / "keep.txt"
        outside.write_text("mine")
        torrentclient._delete_inside(str(self.folder), str(outside))
        torrentclient._delete_inside(str(self.folder), str(self.folder))
        self.assertTrue(outside.exists())
        self.assertTrue(self.folder.exists())


class FetchTests(unittest.TestCase):
    def fake_zip(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(torrentengine.ARIA2_MEMBER, b"MZ" + b"\0" * 100)
        return buffer.getvalue()

    def serve(self, blob: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = blob
        return mock.patch.object(torrentengine.urllib.request, "urlopen", return_value=response)

    def test_only_the_exact_release_is_ever_kept(self):
        blob = self.fake_zip()
        with mock.patch.object(torrentengine, "can_fetch", return_value=True), \
                mock.patch.object(torrentengine, "home", return_value=Path(tempfile.mkdtemp())):
            with self.serve(blob):
                with self.assertRaises(torrentengine.EngineError):
                    torrentengine.fetch()
            with self.serve(blob), mock.patch.object(torrentengine, "ARIA2_SHA256",
                                                     hashlib.sha256(blob).hexdigest()):
                path = Path(torrentengine.fetch())
            self.assertTrue(path.read_bytes().startswith(b"MZ"))

    def test_store_prefers_the_built_in_downloader(self):
        with mock.patch.object(torrentengine, "available", return_value=True):
            self.assertTrue(store.builtin_engine({}))
            self.assertFalse(store.builtin_engine({"store_torrent_engine": "app"}))
        with mock.patch.object(torrentengine, "available", return_value=False):
            self.assertFalse(store.builtin_engine({}))


if __name__ == "__main__":
    unittest.main()
