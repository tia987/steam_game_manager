"""Closing the store when Moonlight disconnects, and from outside with --quit-store.

Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-stream-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import library, running, server, store, streamwatch  # noqa: E402


class LogTailTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(dir=TEMP)) / "sunshine.log"
        self.path.write_text("[old]: Info: CLIENT DISCONNECTED\n", "utf-8")

    def append(self, text):
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def test_only_lines_written_after_it_started(self):
        tail = streamwatch.LogTail(self.path)
        self.assertEqual(tail.lines(), [])
        self.append("[a]: Info: CLIENT CONNECTED\r\n[b]: half a li")
        self.assertEqual(tail.lines(), ["[a]: Info: CLIENT CONNECTED"])
        self.append("ne\n")
        self.assertEqual(tail.lines(), ["[b]: half a line"])

    def test_a_restarted_log_is_read_from_the_top(self):
        tail = streamwatch.LogTail(self.path)
        self.path.write_text("x: CLIENT DISCONNECTED\n", "utf-8")
        self.assertEqual(tail.lines(), ["x: CLIENT DISCONNECTED"])


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(dir=TEMP)) / "sunshine.log"
        self.path.write_text("[start]: Info: CLIENT CONNECTED\n", "utf-8")
        self.now = [1000.0]
        self.closed = []
        self.busy = False
        self.choice = "finish"
        self.watcher = streamwatch.Watcher(
            streamwatch.LogTail(self.path), lambda: self.closed.append(self.now[0]),
            lambda: self.busy, lambda: self.choice, clock=lambda: self.now[0])

    def log(self, line):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(f"[2026:11:25:20:00:00]: Info: {line}\n")

    def later(self, seconds):
        self.now[0] += seconds
        return self.watcher.step()

    def test_closes_once_the_client_stays_away(self):
        self.assertFalse(self.later(1))
        self.log("CLIENT DISCONNECTED")
        self.assertFalse(self.later(1))
        self.assertFalse(self.later(streamwatch.GRACE - 2))
        self.assertTrue(self.later(2))
        self.assertEqual(len(self.closed), 1)
        self.assertTrue(self.later(5))
        self.assertEqual(len(self.closed), 1, "closed only once")

    def test_coming_back_in_time_keeps_it(self):
        self.log("CLIENT DISCONNECTED")
        self.later(1)
        self.log("CLIENT CONNECTED")
        self.assertFalse(self.later(streamwatch.GRACE * 3))
        self.assertEqual(self.closed, [])

    def test_finish_waits_for_downloads(self):
        self.busy = True
        self.log("CLIENT DISCONNECTED")
        self.later(1)
        self.assertFalse(self.later(streamwatch.GRACE * 5))
        self.busy = False
        self.assertTrue(self.later(1))

    def test_close_does_not_wait(self):
        self.busy, self.choice = True, "close"
        self.log("CLIENT DISCONNECTED")
        self.later(1)
        self.assertTrue(self.later(streamwatch.GRACE))

    def test_stay_never_closes(self):
        self.choice = "stay"
        self.log("CLIENT DISCONNECTED")
        self.later(1)
        self.assertFalse(self.later(streamwatch.GRACE * 10))
        self.choice = "close"          # changed in Settings: applies straight away
        self.assertTrue(self.later(1))


class FindLogTests(unittest.TestCase):
    def test_chosen_file_and_newest_found(self):
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        chosen = folder / "mine.log"
        self.assertIsNone(streamwatch.find_log(str(chosen)))
        chosen.write_text("", "utf-8")
        self.assertEqual(streamwatch.find_log(str(chosen)), chosen)
        old, new = folder / "a.log", folder / "b.log"
        old.write_text("", "utf-8")
        new.write_text("", "utf-8")
        os.utime(old, (1, 1))
        with mock.patch.object(streamwatch, "log_candidates", return_value=[old, new]):
            self.assertEqual(streamwatch.find_log(), new)


class SettingTests(unittest.TestCase):
    def test_default_and_saving(self):
        library.save_settings({"store_disconnect": None})
        self.assertEqual(store.store_settings()["disconnect"], "finish")
        self.assertEqual(store.save_store_settings({"disconnect": "close"})["disconnect"], "close")
        self.assertEqual(store.save_store_settings({"disconnect": "nonsense"})["disconnect"], "close")

    def test_quit_command_names_the_flag(self):
        self.assertTrue(store.sunshine_quit_command().endswith("--quit-store"))
        self.assertIn("--streaming", store.sunshine_command())


class QuitStoreTests(unittest.TestCase):
    """A real server on a spare port; its shutdown only stops that server."""

    def start(self, mode="store"):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        stopped = threading.Event()

        def fake_shutdown(delay=0.0):
            stopped.set()
            threading.Thread(target=lambda: (httpd.shutdown(), httpd.server_close()),
                             daemon=True).start()

        patch_shutdown = mock.patch.object(server, "shutdown", fake_shutdown)
        patch_mode = mock.patch.dict(server.STATE, {"mode": mode})
        patch_shutdown.start()
        patch_mode.start()
        self.addCleanup(patch_shutdown.stop)
        self.addCleanup(patch_mode.stop)
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close())
                        if not stopped.is_set() else None)
        return port, stopped

    def setUp(self):
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        patch = mock.patch.object(running, "_folder", lambda: folder)
        patch.start()
        self.addCleanup(patch.stop)

    def test_closes_the_store_from_its_note(self):
        port, stopped = self.start()
        running.announce(port, server.TOKEN, "store")
        self.assertEqual(running.quit_store(wait=5), 0)
        self.assertTrue(stopped.is_set())
        self.assertEqual(running.notes(), [])

    def test_leaves_the_game_manager_alone(self):
        port, stopped = self.start("manager")
        running.announce(port, server.TOKEN, "manager")
        self.assertEqual(running.quit_store(wait=1), 0)
        self.assertFalse(stopped.is_set())

    def test_a_wrong_token_is_refused(self):
        port, stopped = self.start()
        with mock.patch.object(running, "_force", return_value=False):
            running.announce(port, "not-the-token", "store")
            self.assertEqual(running.quit_store(wait=1), 1)
        self.assertFalse(stopped.is_set())

    def test_found_without_a_note(self):
        port, stopped = self.start()
        with mock.patch.object(running, "PORTS", [port]):
            self.assertEqual(running.quit_store(wait=5), 0)
        self.assertTrue(stopped.is_set())

    def test_nothing_running(self):
        with mock.patch.object(running, "PORTS", []):
            self.assertEqual(running.quit_store(wait=1), 0)


if __name__ == "__main__":
    unittest.main()
