"""Installer side programs, the store's own file check, and post-install tidying.

Run from the ``src`` folder::

    python -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="nsgm-extras-test-")
os.environ["XDG_CONFIG_HOME"] = TEMP
os.environ["APPDATA"] = TEMP

from nsgm import autoinstall, filecheck, store, winclean  # noqa: E402


class SideProgramTests(unittest.TestCase):
    def test_what_counts_as_an_extra(self):
        for name in ("QuickSFV.EXE", "quicksfv.exe"):
            self.assertEqual(autoinstall.side_program(name), "checker", name)
        for name in ("VC_redist.x64.exe", "vcredist_x86.exe", "DXSETUP.exe", "dxwebsetup.exe",
                     "oalinst.exe", "PhysX_9.21.0713_SystemSoftware.exe", "UE4PrereqSetup_x64.exe",
                     "windowsdesktop-runtime-8.0.4-win-x64.exe", "NDP472-KB4054530-x86-x64.exe"):
            self.assertEqual(autoinstall.side_program(name), "redist", name)
        for name in ("setup.exe", "setup.tmp", "unarc.exe", "Game.exe", "cmd.exe", "msiexec.exe"):
            self.assertEqual(autoinstall.side_program(name), "", name)

    def test_extras_are_stopped_only_when_asked(self):
        class Tree:
            root = 1
            names = {1: "setup.exe", 2: "setup.tmp", 3: "DXSETUP.exe", 4: "QuickSFV.EXE"}
            stopped: list = []

            def pids(self):
                return {1, 2, 3, 4}

            def stop_branch(self, pid):
                self.stopped.append(pid)
                return True

        queue = autoinstall.Queue.__new__(autoinstall.Queue)
        queue._note = lambda task, text: None
        task = autoinstall.Task(id="t", path="x", skip_redists=False, skip_checkers=True)
        tree = Tree()
        queue._stop_side_programs(task, tree, set())
        self.assertEqual(tree.stopped, [4])                      # the checker only
        tree.stopped = []
        task.skip_redists = True
        handled: set = set()
        queue._stop_side_programs(task, tree, handled)
        queue._stop_side_programs(task, tree, handled)          # once each, not every pass
        self.assertEqual(sorted(tree.stopped), [3, 4])


class FileCheckTests(unittest.TestCase):
    def test_reports_missing_and_changed_files(self):
        game = Path(tempfile.mkdtemp(dir=TEMP)) / "Game"
        (game / "bin").mkdir(parents=True)
        (game / "bin" / "game.exe").write_bytes(b"good")
        (game / "data.pak").write_bytes(b"changed")
        (game / "MD5").mkdir()
        good = hashlib.md5(b"good").hexdigest()
        (game / "MD5" / "game.md5").write_text(
            f"; made by a checker\n{good} *bin\\game.exe\n{hashlib.md5(b'original').hexdigest()} "
            f"*data.pak\n{good} *gone.dll\n", "utf-8")
        (game / "extra.sfv").write_text(f"bin/game.exe {zlib.crc32(b'good'):08x}\n", "utf-8")
        result = filecheck.check(game)
        self.assertEqual(result["checked"], 4)
        self.assertEqual(result["missing"], ["gone.dll"])
        self.assertEqual(result["bad"], ["data.pak"])

    def test_never_looks_outside_the_game(self):
        root = Path(tempfile.mkdtemp(dir=TEMP))
        (root / "secret.txt").write_bytes(b"x")
        game = root / "Game"
        game.mkdir()
        (game / "list.md5").write_text(f"{hashlib.md5(b'x').hexdigest()} *../secret.txt\n", "utf-8")
        self.assertEqual(filecheck.check(game)["missing"], ["../secret.txt"])


class TidyTests(unittest.TestCase):
    def test_run_as_admin_word_is_taken_out_and_nothing_else(self):
        self.assertEqual(winclean.without_admin("~ RUNASADMIN"), "")
        self.assertEqual(winclean.without_admin("~ RUNASADMIN HIGHDPIAWARE"), "~ HIGHDPIAWARE")
        self.assertEqual(winclean.without_admin("~ HIGHDPIAWARE"), "~ HIGHDPIAWARE")

    def test_only_shortcuts_made_during_the_install_are_removed(self):
        desk = Path(tempfile.mkdtemp(dir=TEMP))
        old = desk / "Mine.lnk"
        old.write_bytes(b"old")
        with mock.patch.object(winclean, "IS_WINDOWS", True), \
                mock.patch.object(winclean, "desktops", return_value=[desk]):
            watch = winclean.DesktopWatch()
            time.sleep(0.01)
            (desk / "Game.lnk").write_bytes(b"new")
            (desk / "Game website.url").write_bytes(b"new")
            (desk / "notes.txt").write_bytes(b"new")
            removed = watch.tidy()
        self.assertEqual(sorted(Path(p).name for p in removed), ["Game website.url", "Game.lnk"])
        self.assertTrue(old.exists())
        self.assertTrue((desk / "notes.txt").exists())


class StoreOptionsTests(unittest.TestCase):
    def test_extras_off_by_default_and_checker_always_skipped(self):
        game = {"id": "g1", "name": "Game", "catalogue": "c", "page": "https://x/"}
        with mock.patch.object(store, "find_game", return_value=game), \
                mock.patch.object(store, "_run_job"):
            job = store.install("g1")
        self.assertEqual((job.extras, job.verify), (False, False))
        target = Path(tempfile.mkdtemp(dir=TEMP))
        task = mock.Mock(id="t", target_dir=str(target), result={"games": []})
        with mock.patch.object(store.autoinstall.QUEUE, "add", return_value=task) as add, \
                mock.patch.object(store, "_follow", return_value=task), \
                mock.patch.object(store, "_change_state"):
            store._install_and_record(job, game, {}, Path(TEMP), {"path": TEMP, "tidy": ""},
                                      None, "")
        self.assertTrue(add.call_args.kwargs["skip_redists"])
        self.assertTrue(add.call_args.kwargs["skip_checkers"])

    def test_chosen_extras_and_check_for_one_game(self):
        game = {"id": "g2", "name": "Game", "catalogue": "c", "page": "https://x/"}
        with mock.patch.object(store, "find_game", return_value=game), \
                mock.patch.object(store, "_run_job"):
            job = store.install("g2", extras=True, verify=True)
        target = Path(tempfile.mkdtemp(dir=TEMP))
        (target / "a.bin").write_bytes(b"a")
        (target / "files.md5").write_text(f"{hashlib.md5(b'a').hexdigest()} *a.bin\n", "utf-8")
        task = mock.Mock(id="t", target_dir=str(target), result={"games": []})
        with mock.patch.object(store.autoinstall.QUEUE, "add", return_value=task) as add, \
                mock.patch.object(store, "_follow", return_value=task), \
                mock.patch.object(store, "_change_state"):
            store._install_and_record(job, game, {}, Path(TEMP), {"path": TEMP, "tidy": ""},
                                      None, "")
        self.assertFalse(add.call_args.kwargs["skip_redists"])
        self.assertIn("All 1 files checked out", job.message)


if __name__ == "__main__":
    unittest.main()


class PlainPathTests(unittest.TestCase):
    def test_titles_become_plain_short_folder_names(self):
        from nsgm import placement
        name = placement.plain_folder_name("Aqueduct 117: Civitas Nova – Gold Edition, v3.0 + 6 DLCs")
        self.assertTrue(name.isascii())
        self.assertNotIn(":", name)
        self.assertLessEqual(len(name), 60)
        self.assertEqual(placement.plain_folder_name("Pokémon’s Café™ — Deluxe"), "Pokemon's Cafe - Deluxe")
        self.assertEqual(placement.plain_folder_name("日本語"), "Game")

    def test_an_old_download_folder_is_carried_over(self):
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        old = folder / "Aqueduct 117_ Civitas Nova – Gold Edition"
        (old / "torrent").mkdir(parents=True)
        (old / "torrent" / "fg-01.bin").write_bytes(b"data")
        home = store._game_home(folder, "Aqueduct 117: Civitas Nova – Gold Edition")
        self.assertTrue(str(home.name).isascii())
        self.assertTrue((home / "torrent" / "fg-01.bin").is_file())
        self.assertFalse(old.exists())

    def test_installer_runs_through_short_paths_when_needed(self):
        queue = autoinstall.Queue.__new__(autoinstall.Queue)
        queue._note = lambda task, text: None
        task = autoinstall.Task(id="t", path="x")
        target = Path(tempfile.mkdtemp(dir=TEMP)) / "Jäätelö"
        shorts = {"C:/Lataukset/Peli/setup.exe": "C:/LATAUK~1/Peli/setup.exe"}
        with mock.patch.object(autoinstall.winproc, "short_path",
                               side_effect=lambda p: shorts.get(p, "C:/JAATEL~1" if "Jäätelö" in p else p)):
            info = queue._plain_paths(task, {"source": "C:/Lataukset/Peli/setup.exe"}, target)
        self.assertEqual(info["source"], "C:/Lataukset/Peli/setup.exe")   # already plain
        self.assertEqual(info["run_target"], "C:/JAATEL~1")
        self.assertTrue(target.is_dir())

    def test_waits_for_data_files_that_are_still_held(self):
        queue = autoinstall.Queue.__new__(autoinstall.Queue)
        queue._check = lambda task: None
        task = autoinstall.Task(id="t", path="x")
        folder = Path(tempfile.mkdtemp(dir=TEMP))
        (folder / "fg-01.bin").write_bytes(b"x")
        real_open = open
        attempts = []

        def flaky(path, mode="r", *args, **kwargs):
            if str(path).endswith("fg-01.bin") and len(attempts) < 2:
                attempts.append(1)
                raise PermissionError(13, "in use")
            return real_open(path, mode, *args, **kwargs)
        with mock.patch("builtins.open", flaky), mock.patch.object(autoinstall.time, "sleep"):
            queue._wait_for_data(task, folder)
        self.assertEqual(len(attempts), 2)
        (folder / "fg-01.bin").unlink()
        (folder / "fg-02.bin").write_bytes(b"x")
        with mock.patch("builtins.open", side_effect=PermissionError(13, "in use")), \
                mock.patch.object(autoinstall.time, "sleep"):
            with self.assertRaises(RuntimeError) as raised:
                queue._wait_for_data(task, folder, patience=0)
        self.assertIn("fg-02.bin", str(raised.exception))


class FinishedTorrentTests(unittest.TestCase):
    def test_built_in_downloader_lets_go_before_the_installer_starts(self):
        content = Path(tempfile.mkdtemp(dir=TEMP)) / "Game"
        content.mkdir()
        (content / "setup.exe").write_bytes(b"MZ")
        (content / "fg-01.bin").write_bytes(b"x")
        client = mock.Mock(leech_only=True)
        got = store.TorrentResult(content, client, "a" * 40, ours=True)
        forgotten = []
        with mock.patch.object(store, "_torrent_download", return_value=got), \
                mock.patch.object(store, "_forget_torrent",
                                  side_effect=lambda c, h, delete_files=False: forgotten.append(
                                      (h, delete_files))):
            result, shared = store._release_by_torrent(
                store.StoreJob(id="g", name="Game"), {"name": "Game"},
                [{"url": "magnet:?xt=urn:btih:" + "a" * 40}], content.parent, {}, False)
        self.assertEqual(forgotten, [("a" * 40, False)])        # files kept, torrent let go
        self.assertEqual(result["path"], str(content))


class FinishPageTests(unittest.TestCase):
    """The installer's last page: the wizard is closed, so its exit code never runs."""

    class Tree:
        def __init__(self, refuse=False):
            self.refuse = refuse
            self.names = {41: "setup.tmp"}
            self.stopped = []

        def pids(self):
            return {40, 41}

        def stop_branch(self, pid):
            self.stopped.append(pid)
            return not self.refuse

    def task(self):
        return autoinstall.Task(id="t", path="setup.exe", name="Game")

    def test_closes_the_wizard_itself(self):
        tree, task = self.Tree(), self.task()
        with mock.patch.object(autoinstall.embed, "finish_owner", return_value=41), \
                mock.patch.object(autoinstall.embed, "finish_unticked") as untick:
            closed, tried = autoinstall.QUEUE._close_at_finish(task, tree, False)
        self.assertTrue(closed)
        self.assertEqual(tree.stopped, [41])
        untick.assert_not_called()          # Finish is never pressed
        self.assertIn("closed setup.tmp", task.log[-1])
        self.assertEqual(task.warning, "")

    def test_without_the_rights_it_unticks_and_says_so(self):
        tree, task = self.Tree(refuse=True), self.task()
        with mock.patch.object(autoinstall.embed, "finish_owner", return_value=41), \
                mock.patch.object(autoinstall.embed, "finish_unticked", return_value={
                    "found": True, "unticked": ["Visit website"], "pressed": True}) as untick:
            closed, tried = autoinstall.QUEUE._close_at_finish(task, tree, False)
            self.assertTrue(closed and tried)
            untick.assert_called_once()
            # Asked again later: never unticks (or presses Finish) twice.
            autoinstall.QUEUE._close_at_finish(task, tree, True)
            untick.assert_called_once()
        self.assertIn("Run as administrator", task.warning)


class InnoTempTests(unittest.TestCase):
    def test_only_new_folders_go(self):
        temp = Path(tempfile.mkdtemp(dir=TEMP))
        (temp / "is-OLD.tmp").mkdir()
        (temp / "keep-me").mkdir()
        with mock.patch.dict(os.environ, {"TEMP": str(temp)}):
            before = winclean.inno_temp_dirs()
            (temp / "is-NEW.tmp").mkdir()
            (temp / "is-NEW.tmp" / "ISDone.dll").write_bytes(b"x")
            self.assertEqual(winclean.remove_new_inno_temp(before, settle=0), ["is-NEW.tmp"])
        self.assertEqual(sorted(p.name for p in temp.iterdir()), ["is-OLD.tmp", "keep-me"])
