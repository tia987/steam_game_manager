"""Uninstall installed programs, then clean up what they leave behind.

Windows' own Programs and Features runs the uninstaller and stops there.
Uninstallers routinely leave save folders, config directories and
registry keys in place, which is why "clean removal" usually means a
second manual sweep. This module does that sweep, but proposes rather
than assumes: every leftover is shown with its size and has to be ticked
before anything is deleted.

Deletion is the one genuinely destructive thing this whole application
does, so ``check_deletable`` gates every path and is deliberately
paranoid. It refuses anything that is not a strict descendant of a known
application data root, anything shallow, and anything reached through a
junction or symlink.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path, PureWindowsPath

IS_WINDOWS = platform.system() == "Windows"

UNINSTALL_KEYS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

# Folders an application may legitimately have left something in. A
# candidate leftover must sit strictly inside one of these.
def data_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                "ProgramFiles", "ProgramFiles(x86)", "PUBLIC"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    home = Path.home()
    roots += [home / "Saved Games", home / "Documents", home / ".config",
              home / ".local/share", home / ".steam"]
    seen, unique = set(), []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


# Never delete these, whatever a heuristic suggests.
def protected_paths() -> set[str]:
    guarded: set[str] = set()
    for root in data_roots():
        guarded.add(str(root).lower().rstrip("\\/"))
    home = Path.home()
    extras = [home, home / "Desktop", home / "Downloads", home / "Documents",
              home / "Pictures", home / "Videos", home / "Music", home / "AppData",
              home / "AppData/Roaming", home / "AppData/Local", home / "AppData/LocalLow"]
    for var in ("SystemRoot", "windir", "SystemDrive", "USERPROFILE"):
        value = os.environ.get(var)
        if value:
            extras.append(Path(value))
    for path in extras:
        guarded.add(str(path).lower().rstrip("\\/"))
    guarded.discard("")
    return guarded


def _norm(path: Path) -> str:
    return str(path).replace("/", "\\").lower().rstrip("\\") if IS_WINDOWS else str(path).rstrip("/")


def _is_reparse(path: Path) -> bool:
    """Junctions and symlinks are followed by shutil.rmtree on some paths."""
    try:
        if path.is_symlink():
            return True
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def check_deletable(path: str | Path) -> tuple[bool, str]:
    """Decide whether one folder may be removed. Refusal is the default."""
    try:
        target = Path(path)
    except (TypeError, ValueError):
        return False, "not a usable path"

    if not target.is_absolute():
        return False, "not an absolute path"
    if not target.exists():
        return False, "no longer there"
    if _is_reparse(target):
        return False, "is a shortcut or junction, deleting it could reach elsewhere"

    flat = _norm(target)
    if flat in protected_paths():
        return False, "is a system or user folder"

    parts = [p for p in re.split(r"[\\/]+", flat) if p and not p.endswith(":")]
    if len(parts) < 2:
        return False, "sits too close to the drive root"

    for root in data_roots():
        root_flat = _norm(root)
        if flat == root_flat:
            return False, "is an application data root itself"
        if flat.startswith(root_flat + ("\\" if IS_WINDOWS else "/")):
            depth = len([p for p in flat[len(root_flat):].split("\\" if IS_WINDOWS else "/") if p])
            if depth < 1:
                return False, "sits directly at a data root"
            return True, ""
    return False, "is outside the folders applications install into"


def folder_size(path: Path, cap: int = 40000) -> int:
    total = files = 0
    try:
        for current, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:
                    pass
                files += 1
                if files > cap:
                    return total
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------
# reading the installed program list
# --------------------------------------------------------------------------

def _reg_children(hive_name: str, subkey: str):
    if not IS_WINDOWS:
        return
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(hives[hive_name], subkey, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as child:
                        values, vi = {}, 0
                        while True:
                            try:
                                key, value, _ = winreg.EnumValue(child, vi)
                            except OSError:
                                break
                            values[key] = value
                            vi += 1
                        yield name, values
                except OSError:
                    continue
        break


def _install_date(raw: str) -> str:
    text = str(raw or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def list_programs() -> list[dict]:
    """Everything Programs and Features would show, plus what we need to act."""
    found: dict[str, dict] = {}
    for hive, subkey in UNINSTALL_KEYS:
        for key_name, values in _reg_children(hive, subkey):
            name = str(values.get("DisplayName", "")).strip()
            if not name:
                continue
            if values.get("SystemComponent") == 1:
                continue
            if values.get("ParentKeyName") or values.get("ParentDisplayName"):
                continue
            if str(values.get("ReleaseType", "")).lower() in ("update", "hotfix", "securityupdate"):
                continue
            uninstall = str(values.get("UninstallString", "")).strip()
            quiet = str(values.get("QuietUninstallString", "")).strip()
            if not uninstall and not quiet:
                continue
            identifier = f"{hive}|{subkey}|{key_name}"
            found.setdefault(identifier, {
                "id": identifier,
                "name": name,
                "publisher": str(values.get("Publisher", "")).strip(),
                "version": str(values.get("DisplayVersion", "")).strip(),
                "installed": _install_date(values.get("InstallDate", "")),
                "size_kb": int(values.get("EstimatedSize") or 0),
                "location": str(values.get("InstallLocation", "")).strip().strip('"'),
                "uninstall": uninstall,
                "quiet": quiet,
                "can_be_quiet": bool(quiet) or bool(uninstall_command(
                    {"uninstall": uninstall}, silent=True)),
                "key": key_name,
                "hive": hive,
                "subkey": subkey,
            })
    programs = sorted(found.values(), key=lambda p: p["name"].lower())
    return programs


# --------------------------------------------------------------------------
# leftovers
# --------------------------------------------------------------------------

STOPWORDS = {"the", "inc", "llc", "ltd", "gmbh", "corp", "corporation", "co",
             "software", "games", "game", "studios", "studio", "entertainment",
             "interactive", "limited", "sa", "ab", "srl", "bv"}


def _tokens(text: str) -> list[str]:
    words = re.split(r"[^A-Za-z0-9]+", str(text or ""))
    return [w.lower() for w in words if len(w) > 2 and w.lower() not in STOPWORDS]


def _matches(folder_name: str, needles: list[str]) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", folder_name.lower())
    if len(flat) < 3:
        return False
    for needle in needles:
        target = re.sub(r"[^a-z0-9]", "", needle)
        if len(target) >= 4 and (target in flat or flat in target):
            return True
    return False


def find_leftovers(program: dict) -> list[dict]:
    """Folders that look like they belong to a program that has been removed.

    Only ever proposes. Everything returned still has to pass
    ``check_deletable`` and be ticked by the person before it goes.
    """
    needles = _tokens(program.get("name", ""))
    publisher = _tokens(program.get("publisher", ""))
    if not needles and not publisher:
        return []
    full_name = re.sub(r"[^a-z0-9]", "", program.get("name", "").lower())
    if len(full_name) >= 4:
        needles.append(full_name)

    candidates: list[Path] = []

    location = program.get("location", "")
    if location and Path(location).is_dir():
        candidates.append(Path(location))

    for root in data_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if _matches(child.name, needles):
                candidates.append(child)
            elif publisher and _matches(child.name, publisher):
                # A publisher folder only counts if the program's own
                # folder sits inside it, otherwise we would offer to
                # delete every other title by the same studio.
                try:
                    inner = [g for g in child.iterdir()
                             if g.is_dir() and _matches(g.name, needles)]
                except OSError:
                    inner = []
                candidates.extend(inner)

    results: list[dict] = []
    seen: set[str] = set()
    for path in candidates:
        flat = _norm(path)
        if flat in seen:
            continue
        seen.add(flat)
        ok, reason = check_deletable(path)
        results.append({
            "path": str(path),
            "size": folder_size(path) if ok else 0,
            "deletable": ok,
            "reason": reason,
        })
    results.sort(key=lambda r: -r["size"])
    return results


def remove_leftovers(paths: list[str]) -> list[dict]:
    """Delete only what passes the guard, and report every outcome."""
    outcomes = []
    for raw in paths:
        ok, reason = check_deletable(raw)
        if not ok:
            outcomes.append({"path": raw, "removed": False, "reason": reason})
            continue
        try:
            shutil.rmtree(raw)
            outcomes.append({"path": raw, "removed": True, "reason": ""})
        except OSError as exc:
            outcomes.append({"path": raw, "removed": False,
                             "reason": f"could not be removed ({exc.strerror or exc})"})
    return outcomes


# --------------------------------------------------------------------------
# running the uninstaller
# --------------------------------------------------------------------------

def uninstall_command(program: dict, silent: bool = False) -> str:
    """The command line that removes a program, quietly if asked and possible.

    Running it is ``autoinstall.run_uninstaller``'s job, which holds the
    uninstaller's window inside the app and waits for every process it
    starts. Not silent by default: the uninstaller's own screens are where
    its questions get answered.

    Few programs register a quiet command, but the common makers' quiet
    switches are known, so one is derived where the maker is clear.
    """
    normal = str(program.get("uninstall") or "").strip()
    quiet = str(program.get("quiet") or "").strip()
    if not silent:
        return normal or quiet
    if quiet:
        return quiet
    if not normal:
        return ""
    if "msiexec" in normal.lower():
        return re.sub(r"(?i)/[IX]\s*\{", "/X{", normal) + " /qn /norestart"
    if re.search(r"(?i)unins\d+\.exe", normal):
        return normal + " /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
    if re.search(r'(?i)[\\/]uninst(all)?\.exe"?\s*$', normal):
        return normal + " /S"
    return ""


def still_installed(program: dict) -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    path = f"{program.get('subkey')}\\{program.get('key')}"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hives[program.get("hive", "HKLM")], path, 0,
                                winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False
