"""The store's BitTorrent route and its behaviour with many downloads at once.

A scripted fake stands in for qBittorrent/Transmission, so these run
offline and in seconds. Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-torrent-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import store, torrentclient  # noqa: E402

HASH = "88a7ba710cc76d8a08b30d7da185845155809131"
MAGNET = f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Glacier+Rookery+%28v1.5%2C+%2B+Bonus+OST%29"
FILES = ["Glacier Rookery/setup.exe", "Glacier Rookery/fg-01.bin",
         "Glacier Rookery/fg-optional-bonus-soundtrack.bin",
         "Glacier Rookery/fg-selective-french.bin", "Glacier Rookery/fg-selective-english.bin"]


class FakeClient:
    """Goes through the states a real client would, one per status call."""

    label = "FakeTorrent"

    def __init__(self, script: list[str], present: bool = False, down_for: int = 0,
                 files_supported: bool = True):
        self.script = list(script)      # states in order; the last repeats
        self.present = present
        self.added: dict = {}
        self.removed: list = []
        self.skipped: list = []
        self.down_for = down_for
        self.files_supported = files_supported
        self.calls = 0
        self.save_path = ""

    def add(self, magnet="", torrent=b"", save_path="", tag=""):
        self.present = True
        self.save_path = save_path
        self.added = {"magnet": magnet, "save_path": save_path, "tag": tag}

    def status(self, hashes):
        self.calls += 1
        if self.down_for > 0:
            self.down_for -= 1
            raise torrentclient.TorrentClientError("FakeTorrent is not answering.")
        if not self.present:
            return []
        state = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        progress = 1.0 if state == "done" else (0.0 if state in ("fetching details", "queued")
                                                else 0.5)
        if state == "done":
            self._write_files()
        return [{"hash": HASH, "name": "Glacier Rookery", "progress": progress, "state": state,
                 "speed": 1024 if state == "downloading" else 0, "eta": 60, "size": 100,
                 "path": self.save_path, "content": os.path.join(self.save_path, "Glacier Rookery"),
                 "error": ""}]

    def _write_files(self):
        for name in FILES:
            target = Path(self.save_path) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(b"MZ" if name.endswith(".exe") else b"data")

    def files(self, info_hash):
        if not self.files_supported:
            raise torrentclient.TorrentClientError("qBittorrent answered HTTP 404.")
        return [{"index": i, "name": n, "size": 10} for i, n in enumerate(FILES)]

    def skip(self, info_hash, indexes):
        self.skipped = list(indexes)

    def remove(self, hashes, delete_files=False):
        self.removed.append((tuple(hashes), delete_files))
        self.present = False


class TorrentRouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=TEMP))
        self.patches = [mock.patch.object(store, "POLL_SECONDS", 0.001),
                        mock.patch.object(store, "CLIENT_GRACE", 5)]
        for patch in self.patches:
            patch.start()
        store._change_state(lambda state: state.__setitem__("torrents", {}))

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def use(self, client):
        return mock.patch.object(store, "torrent_client", lambda *a, **k: client)

    def game(self) -> dict:
        return {"id": "g1", "name": "Glacier Rookery", "catalogue": "c", "page": "https://x/",
                "download_size": 0, "size": 0,
                "release": {"mirrors": [], "torrents": [{"url": MAGNET, "label": "t", "name": ""}]}}

    def test_optional_files_are_left_out_and_cleaned_up(self):
        client = FakeClient(["fetching details", "downloading", "downloading", "done"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            result, got = store._release_by_torrent(job, self.game(), [{"url": MAGNET}],
                                                     self.folder / "Glacier Rookery", {}, False)
        # The release name says "Bonus OST", which must not be mistaken for a soundtrack-only download.
        self.assertTrue(got.ours)
        self.assertEqual(client.added["tag"], store.TORRENT_TAG)
        self.assertEqual(sorted(client.skipped), [2, 3])        # bonus OST and French
        content = Path(result["path"])
        self.assertTrue((content / "setup.exe").is_file())
        self.assertTrue((content / "fg-selective-english.bin").is_file())
        self.assertFalse((content / "fg-optional-bonus-soundtrack.bin").exists())
        self.assertFalse((content / "fg-selective-french.bin").exists())
        self.assertIn(HASH, store.load_state()["torrents"])

    def test_someone_elses_torrent_is_never_removed_or_changed(self):
        client = FakeClient(["downloading", "downloading"], present=True)
        client.save_path = str(self.folder / "theirs")
        job = store.StoreJob(id="g1", name="Glacier Rookery")

        def cancel_soon():
            time.sleep(0.05)
            job.cancel = True
        threading.Thread(target=cancel_soon).start()
        with self.use(client):
            with self.assertRaises(store.Stopped):
                store._torrent_download(job, MAGNET, self.folder, {}, check_name=False,
                                        skip=lambda n: True)
        self.assertEqual(client.removed, [])
        self.assertEqual(client.skipped, [])
        self.assertEqual(client.added, {})

    def test_cancelling_our_torrent_removes_it_with_its_files(self):
        client = FakeClient(["downloading"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        threading.Timer(0.05, lambda: setattr(job, "cancel", True)).start()
        with self.use(client):
            with self.assertRaises(store.Stopped):
                store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        self.assertEqual(client.removed, [((HASH,), True)])
        self.assertNotIn(HASH, store.load_state().get("torrents", {}))

    def test_ownership_survives_a_restart(self):
        first = FakeClient(["downloading"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        threading.Timer(0.05, lambda: setattr(job, "cancel", True)).start()
        # Simulate the app closing mid-download: the job stops without tidying.
        with self.use(first), mock.patch.object(store, "_forget_torrent", lambda *a, **k: None):
            with self.assertRaises(store.Stopped):
                store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        # After the restart the torrent is already in the client, and still the store's.
        again = FakeClient(["downloading", "done"], present=True)
        again.save_path = str(self.folder)
        with self.use(again):
            got = store._torrent_download(store.StoreJob(id="g1", name="x"), MAGNET,
                                          self.folder, {}, check_name=False)
        self.assertTrue(got.ours)

    def test_queue_time_does_not_count_against_patience(self):
        client = FakeClient(["queued"] * 400 + ["downloading", "done"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False,
                                          patience=store.Patience(details=0.01, stalled=0.01))
        self.assertTrue(got.path.is_dir())

    def test_a_torrent_nobody_shares_is_given_up_when_there_is_another_way(self):
        client = FakeClient(["fetching details"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            with self.assertRaises(store.StoreError) as raised:
                store._torrent_download(job, MAGNET, self.folder, {}, check_name=False,
                                        patience=store.Patience(details=0.005, stalled=60))
        self.assertIn("details", str(raised.exception))
        self.assertEqual(client.removed, [((HASH,), True)])     # its partial files go too

    def test_stalled_torrent_is_given_up_only_with_patience(self):
        client = FakeClient(["stalled"] * 50 + ["done"])
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        self.assertTrue(got.path.exists())                       # no patience: it waited

    def test_client_restarting_is_waited_out(self):
        client = FakeClient(["downloading", "done"], down_for=20)
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False)
        self.assertTrue(got.path.exists())

    def test_clients_that_cannot_choose_files_still_work(self):
        client = FakeClient(["fetching details", "downloading", "done"], files_supported=False)
        job = store.StoreJob(id="g1", name="Glacier Rookery")
        with self.use(client):
            got = store._torrent_download(job, MAGNET, self.folder, {}, check_name=False,
                                          skip=lambda n: "optional" in n)
        self.assertTrue((got.path / "fg-optional-bonus-soundtrack.bin").exists())

    def test_install_leaves_someone_elses_torrent_alone(self):
        client = FakeClient(["done"], present=True)
        client.save_path = str(self.folder)
        got = store.TorrentResult(self.folder / "Glacier Rookery", client, HASH, ours=False)
        client._write_files()
        task = mock.Mock(id="t", target_dir="", result={"games": []})
        with mock.patch.object(store.autoinstall.QUEUE, "add", return_value=task), \
                mock.patch.object(store, "_follow", return_value=task), \
                mock.patch.object(store, "_change_state"):
            store._install_and_record(store.StoreJob(id="g1", name="x"), self.game(), {},
                                      self.folder, {"path": str(got.path), "tidy": str(self.folder)},
                                      got, "")
        self.assertEqual(client.removed, [])
        self.assertTrue((self.folder / "Glacier Rookery" / "setup.exe").exists())


class TransmissionStateTests(unittest.TestCase):
    def status(self, **fields) -> dict:
        client = torrentclient.Transmission()
        item = {"hashString": HASH, "name": "G", "percentDone": 0.3, "status": 4,
                "rateDownload": 100, "eta": 5, "downloadDir": "/d", "totalSize": 9, "error": 0,
                "errorString": "", "metadataPercentComplete": 1, "peersSendingToUs": 3}
        item.update(fields)
        with mock.patch.object(client, "_call", return_value={"torrents": [item]}):
            return client.status([HASH])[0]

    def test_tracker_errors_are_not_fatal(self):
        self.assertEqual(self.status(error=2, errorString="Tracker gave HTTP 404")["state"],
                         "downloading")
        self.assertEqual(self.status(error=3, errorString="No space left")["state"], "error")

    def test_magnet_without_details_and_stalled(self):
        self.assertEqual(self.status(metadataPercentComplete=0.2)["state"], "fetching details")
        self.assertEqual(self.status(rateDownload=0, peersSendingToUs=0)["state"], "stalled")


class ManyDownloadsTests(unittest.TestCase):
    def test_direct_downloads_take_turns(self):
        running, peak = [0], [0]
        lock = threading.Lock()

        def work(n):
            job = store.StoreJob(id=f"j{n}", name="g")
            with store._direct_slot(job, {"store_parallel_downloads": 2}):
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                time.sleep(0.05)
                with lock:
                    running[0] -= 1
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 2)

    def test_waiting_for_a_turn_can_be_cancelled(self):
        holder = store.StoreJob(id="holder", name="g")
        waiting = store.StoreJob(id="waiting", name="g")
        with store._direct_slot(holder, {"store_parallel_downloads": 1}):
            threading.Timer(0.05, lambda: setattr(waiting, "cancel", True)).start()
            with self.assertRaises(store.Stopped):
                with store._direct_slot(waiting, {"store_parallel_downloads": 1}):
                    pass

    def test_space_set_aside_by_other_downloads_counts(self):
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        gb = 1024 ** 3
        with mock.patch.object(store.drives, "space",
                               return_value={"ok": True, "free": 30 * gb, "total": 100 * gb}):
            first = store.StoreJob(id="a", name="a")
            second = store.StoreJob(id="b", name="b")
            store._reserve(first, folder / "a", 20 * gb)
            with self.assertRaises(store.StoreError) as raised:
                store._reserve(second, folder / "b", 20 * gb)
            self.assertIn("set aside", str(raised.exception))
            store._release_reservation(first)
            store._reserve(second, folder / "b", 20 * gb)
            store._release_reservation(second)


if __name__ == "__main__":
    unittest.main()
