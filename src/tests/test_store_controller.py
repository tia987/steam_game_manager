"""Store settings that have to work with only a controller, and streaming.

Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-controller-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import library, store, torrentclient  # noqa: E402


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.a = str(Path(tempfile.mkdtemp(dir=TEMP)))
        self.b = str(Path(tempfile.mkdtemp(dir=TEMP)))
        library.save_settings({"library_folders": [
            {"path": self.a, "kind": "auto", "reserve_gb": 0, "enabled": True},
            {"path": self.b, "kind": "auto", "reserve_gb": 0, "enabled": True}]})

    def paths(self):
        return [f["path"] for f in store.library_overview()["folders"]]

    def test_put_first_switch_off_and_remove(self):
        store.change_library("first", self.b)
        self.assertEqual(self.paths(), [self.b, self.a])
        store.change_library("toggle", self.a)
        folders = store.library_overview()["folders"]
        self.assertFalse(folders[1]["enabled"])
        store.change_library("remove", self.a)
        self.assertEqual(self.paths(), [self.b])
        self.assertTrue(Path(self.a).is_dir())             # nothing on disk is deleted

    def test_install_to_a_chosen_folder_only(self):
        with self.assertRaises(store.StoreError):
            store.install("whatever", target="/not/a/library")
        game = {"id": "g1", "name": "Game", "catalogue": "c", "page": "https://x/"}
        with mock.patch.object(store, "find_game", return_value=game), \
                mock.patch.object(store, "_run_job"):
            job = store.install("g1", target=self.b)
        self.assertEqual(job.target, self.b)
        task = mock.Mock(id="t", target_dir="", result={"games": []})
        with mock.patch.object(store.autoinstall.QUEUE, "add", return_value=task) as add, \
                mock.patch.object(store, "_follow", return_value=task), \
                mock.patch.object(store, "_change_state"):
            store._install_and_record(job, game, {}, Path(TEMP),
                                      {"path": TEMP, "tidy": ""}, None, "")
        self.assertEqual(add.call_args.kwargs["target_root"], self.b)


class TorrentSignInTests(unittest.TestCase):
    def test_sign_in_is_tried_before_it_is_kept(self):
        library.save_settings({"torrent_client": {"kind": "transmission",
                                                  "url": "http://127.0.0.1:9091"}})
        with mock.patch.object(torrentclient.Transmission, "version",
                               side_effect=torrentclient.TorrentClientError(
                                   "Transmission refused the username or password.")):
            with self.assertRaises(store.StoreError):
                store.torrent_login("me", "wrong")
        self.assertFalse(library.load_settings()["torrent_client"].get("password"))
        with mock.patch.object(torrentclient.Transmission, "version", return_value="4.0.6"):
            found = store.torrent_login("me", "right")
        saved = library.load_settings()
        self.assertEqual(saved["torrent_client"]["password"], "right")
        self.assertEqual(saved["store_torrent_engine"], "app")
        self.assertIn("Signed in", found["message"])


class DefaultsTests(unittest.TestCase):
    def test_playstation_symbols_newest_first_and_muted_installers_by_default(self):
        settings = store.store_settings({})
        self.assertEqual(settings["glyphs"], "playstation")
        self.assertEqual(settings["sort"], "newest")
        self.assertTrue(settings["mute_installers"])
        self.assertEqual(store.store_settings({"store_glyphs": "auto"})["glyphs"], "playstation")
        saved = store.save_store_settings({"sort": "name", "mute_installers": False, "glyphs": "auto"})
        self.assertEqual((saved["sort"], saved["mute_installers"]), ("name", False))
        self.assertEqual(saved["glyphs"], "auto")              # picked on purpose: kept

    def test_catalogue_dates_are_kept_for_sorting(self):
        parsed = store.parse_catalogue(b'[{"name": "A", "site": "https://a.example", '
                                       b'"date": "2026-11-20"}, {"name": "B", '
                                       b'"site": "https://b.example", "date": "soon"}]', "c")
        self.assertEqual([g["date"] for g in parsed["games"]], ["2026-11-20", ""])

    def test_muting_is_a_no_op_off_windows(self):
        from nsgm import winaudio
        if os.name != "nt":
            self.assertIsNone(winaudio.mute_while(mock.Mock(pids=lambda: {1})))


class StreamingTests(unittest.TestCase):
    def test_sunshine_is_recognised_unless_steam_started_it(self):
        with mock.patch.dict(os.environ, {"SUNSHINE_APP_NAME": "Game Store"}, clear=False):
            for key in ("SteamGameId", "SteamClientLaunch", "SteamAppId"):
                os.environ.pop(key, None)
            self.assertTrue(store.started_by_sunshine())
            with mock.patch.dict(os.environ, {"SteamGameId": "123"}):
                self.assertFalse(store.started_by_sunshine())

    def test_streamed_store_restarts_steam_without_leaving(self):
        spawned = []
        with mock.patch.object(store, "STREAMING", True), \
                mock.patch.object(store, "STORE_WINDOW", True), \
                mock.patch.object(store, "_spawn_detached", lambda c, cwd: spawned.append(c)), \
                mock.patch.dict(store._REFRESHING, {"at": 0}):
            result = store.refresh_steam(relaunch=True)
        self.assertFalse(result["leaving"])
        self.assertIn("--steam-background", spawned[0])
        self.assertNotIn("--relaunch-store", spawned[0])

    def test_sunshine_command_opens_the_store(self):
        command = store.sunshine_command()
        self.assertIn("--store", command)
        self.assertIn("--streaming", command)


if __name__ == "__main__":
    unittest.main()
