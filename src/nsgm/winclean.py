"""Undo two things installers do that nobody asked for, on Windows.

* **"Run as administrator" on the game.** Many repack installers tick the
  compatibility setting that makes the game ask for administrator rights
  every time it starts - a Windows prompt in the middle of Big Picture,
  which a controller over Moonlight cannot answer. Windows keeps that
  setting per program under ``AppCompatFlags\\Layers``; the ``RUNASADMIN``
  word is taken out for every program inside the game's folder, and any
  other compatibility settings there are left as they were.
* **Desktop shortcuts.** The desktops are looked at before the installer
  runs and again after, and shortcuts that appeared in between are
  removed. Nothing that was there before is touched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
SHORTCUT_TYPES = (".lnk", ".url")


def _inside(path: str, folder: str) -> bool:
    try:
        child = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(folder))
    except (TypeError, ValueError):
        return False
    return child.startswith(root.rstrip("\\/") + os.sep)


def without_admin(flags: str) -> str:
    """A Layers value with RUNASADMIN taken out; "" when nothing else is left."""
    words = [w for w in (flags or "").split() if w.upper() != "RUNASADMIN"]
    if not [w for w in words if w != "~"]:
        return ""
    return " ".join(words)


def clear_run_as_admin(folder: str) -> list[str]:
    """Take "run as administrator" off every program under ``folder``.

    Returns the programs changed. The per-user setting always can be; the
    all-users one only when this app runs as administrator, and is left
    alone otherwise.
    """
    if not IS_WINDOWS or not folder:
        return []
    import winreg
    changed: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        # The Layers key is shared between 32- and 64-bit programs, so one
        # view of it is all of it.
        for view in (0,):
            try:
                key = winreg.OpenKey(hive, LAYERS, 0,
                                     winreg.KEY_READ | winreg.KEY_SET_VALUE | view)
            except OSError:
                continue
            try:
                index, entries = 0, []
                while True:
                    try:
                        entries.append(winreg.EnumValue(key, index))
                    except OSError:
                        break
                    index += 1
                for program, flags, kind in entries:
                    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                        continue
                    if not _inside(program, folder) or "RUNASADMIN" not in str(flags).upper():
                        continue
                    kept = without_admin(str(flags))
                    try:
                        if kept:
                            winreg.SetValueEx(key, program, 0, kind, kept)
                        else:
                            winreg.DeleteValue(key, program)
                        if program not in changed:
                            changed.append(program)
                    except OSError:
                        pass
            finally:
                winreg.CloseKey(key)
    return changed


def desktops() -> list[Path]:
    """This user's desktop (wherever OneDrive moved it) and the shared one."""
    if not IS_WINDOWS:
        return []
    import winreg
    found: list[Path] = []
    places = [
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "Desktop"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders", "Common Desktop"),
    ]
    for hive, path, name in places:
        try:
            with winreg.OpenKey(hive, path) as key:
                value = os.path.expandvars(str(winreg.QueryValueEx(key, name)[0]))
            if value and Path(value).is_dir():
                found.append(Path(value))
        except OSError:
            continue
    for fallback in (Path(os.environ.get("USERPROFILE", "")) / "Desktop",
                     Path(os.environ.get("PUBLIC", "")) / "Desktop"):
        if str(fallback) not in (".", "Desktop") and fallback.is_dir() and fallback not in found:
            found.append(fallback)
    return found


def shortcuts() -> dict[str, float]:
    """Every shortcut on the desktops now, with when it was last changed."""
    out: dict[str, float] = {}
    for desktop in desktops():
        try:
            for item in desktop.iterdir():
                if item.suffix.lower() in SHORTCUT_TYPES and item.is_file():
                    out[str(item)] = item.stat().st_mtime
        except OSError:
            continue
    return out


def remove_new_shortcuts(before: dict[str, float], since: float) -> list[str]:
    """Delete desktop shortcuts that were not there before ``since``. Returns them."""
    removed: list[str] = []
    for path, changed in shortcuts().items():
        if path in before or changed < since - 5:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass                         # the shared desktop needs administrator rights
    return removed


class DesktopWatch:
    """Remember the desktops before an install; tidy them after."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and IS_WINDOWS
        self.started = time.time()
        self.before = shortcuts() if self.enabled else {}

    def tidy(self) -> list[str]:
        if not self.enabled:
            return []
        return remove_new_shortcuts(self.before, self.started)
