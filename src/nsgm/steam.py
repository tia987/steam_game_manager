"""Locate Steam, enumerate its local accounts, and rewrite shortcuts.vdf."""

from __future__ import annotations

import os
import platform
import shutil
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import vdf

STEAM_ID64_BASE = 76561197960265728
BACKUPS_KEPT = 12

# Fields Steam writes for every shortcut. Order matches the client's own
# output, which keeps diffs against a Steam-written file readable.
SHORTCUT_DEFAULTS: dict = {
    "appid": 0,
    "AppName": "",
    "Exe": "",
    "StartDir": "",
    "icon": "",
    "ShortcutPath": "",
    "LaunchOptions": "",
    "IsHidden": 0,
    "AllowDesktopConfig": 1,
    "AllowOverlay": 1,
    "OpenVR": 0,
    "Devkit": 0,
    "DevkitGameID": "",
    "DevkitOverrideAppID": 0,
    "LastPlayTime": 0,
    "FlatpakAppID": "",
    "tags": {},
}


# --------------------------------------------------------------------------
# shortcut identifiers
# --------------------------------------------------------------------------

def quote_exe(path: str) -> str:
    """Steam stores executable paths wrapped in double quotes."""
    path = path.strip()
    if path.startswith('"') and path.endswith('"') and len(path) > 1:
        return path
    return f'"{path}"'


def parent_dir(path: str) -> str:
    """Directory part of a path, honouring both separator styles.

    pathlib resolves separators for the *host* OS, so a Windows path
    inspected on Linux (or a wine prefix path) would otherwise lose its
    parent. Steam keeps the trailing separator, so we do too.
    """
    raw = path.strip().strip('"')
    cut = max(raw.rfind("/"), raw.rfind("\\"))
    if cut < 0:
        return ""
    sep = raw[cut]
    head = raw[:cut]
    if not head or head.endswith(":"):
        head += sep
        return head
    return head + sep


def shortcut_id(exe: str, app_name: str) -> int:
    """The 32 bit id Steam derives from a shortcut.

    ``exe`` must be byte-for-byte the string stored in the ``Exe`` field,
    quotes included, or the artwork filenames will not line up with what
    the client looks for.
    """
    return zlib.crc32((exe + app_name).encode("utf-8")) | 0x80000000


def signed32(value: int) -> int:
    """Reinterpret an unsigned 32 bit id as the signed value stored in the file."""
    return struct.unpack("<i", struct.pack("<I", value & 0xFFFFFFFF))[0]


def unsigned32(value: int) -> int:
    return struct.unpack("<I", struct.pack("<i", value))[0] if value < 0 else value


def run_game_id(short_id: int) -> int:
    """The 64 bit id used by ``steam://rungameid/`` links."""
    return (short_id << 32) | 0x02000000


# --------------------------------------------------------------------------
# installation discovery
# --------------------------------------------------------------------------

def _windows_steam_root() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                for name in ("SteamPath", "InstallPath"):
                    try:
                        value = winreg.QueryValueEx(handle, name)[0]
                    except FileNotFoundError:
                        continue
                    candidate = Path(value)
                    if candidate.exists():
                        return candidate
        except OSError:
            continue
    return None


def find_steam_root() -> Path | None:
    """Return the Steam installation directory, or None if it cannot be found."""
    override = os.environ.get("NSGM_STEAM_ROOT")
    if override and Path(override).exists():
        return Path(override)

    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        found = _windows_steam_root()
        if found:
            return found
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Steam")
    elif system == "Darwin":
        candidates.append(Path.home() / "Library/Application Support/Steam")
    else:
        home = Path.home()
        candidates += [
            home / ".steam/steam",
            home / ".steam/root",
            home / ".local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",
            Path("/usr/lib/steam"),
        ]

    for candidate in candidates:
        if (candidate / "userdata").exists() or (candidate / "steamapps").exists():
            return candidate
    return None


def steam_is_running() -> bool:
    """Best effort check. Writing shortcuts.vdf under a live client loses data."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                capture_output=True, text=True, timeout=8,
            ).stdout.lower()
            return "steam.exe" in out
        return subprocess.run(
            ["pgrep", "-x", "steam"], capture_output=True, timeout=8
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

@dataclass
class SteamUser:
    id3: str
    name: str
    userdata: Path
    most_recent: bool = False
    known: bool = False        # Steam has signed in as this account
    has_data: bool = False     # the folder holds real configuration

    @property
    def id64(self) -> int:
        return int(self.id3) + STEAM_ID64_BASE

    @property
    def config_dir(self) -> Path:
        return self.userdata / "config"

    @property
    def shortcuts_file(self) -> Path:
        return self.config_dir / "shortcuts.vdf"

    @property
    def grid_dir(self) -> Path:
        return self.config_dir / "grid"

    def to_json(self) -> dict:
        return {
            "id3": self.id3,
            "id64": str(self.id64),
            "name": self.name,
            "most_recent": self.most_recent,
            "shortcuts": str(self.shortcuts_file),
        }


def library_folders(steam_root: Path) -> list[Path]:
    """Every disk Steam keeps games on, not just the one it is installed to."""
    found = [steam_root / "steamapps"]
    manifest = steam_root / "steamapps" / "libraryfolders.vdf"
    if manifest.exists():
        try:
            parsed = vdf.text_loads(manifest.read_text("utf-8", "replace"))
            entries = parsed.get("libraryfolders") or parsed.get("LibraryFolders") or {}
            for key, value in entries.items():
                path = value.get("path") if isinstance(value, dict) else value
                if isinstance(path, str) and path.strip():
                    candidate = Path(path) / "steamapps"
                    if candidate.is_dir() and candidate not in found:
                        found.append(candidate)
        except (OSError, ValueError):
            pass
    return [f for f in found if f.is_dir()]


def installed_apps(steam_root: Path) -> dict[str, str]:
    """Games Steam already has, by title.

    Read from the appmanifest files Steam writes for each installed game.
    Without this, a scan of installed programs offers back the Steam
    library itself, which is the one thing that does not need adding.
    """
    apps: dict[str, str] = {}
    for folder in library_folders(steam_root):
        try:
            manifests = list(folder.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for manifest in manifests:
            try:
                parsed = vdf.text_loads(manifest.read_text("utf-8", "replace"))
            except (OSError, ValueError):
                continue
            state = parsed.get("AppState") or {}
            name = str(state.get("name", "")).strip()
            app_id = str(state.get("appid", "")).strip()
            if name:
                apps[name.lower()] = app_id
    return apps


def list_users(steam_root: Path) -> list[SteamUser]:
    """Every account with a local userdata folder, most recent first."""
    names: dict[str, tuple[str, bool]] = {}
    login_file = steam_root / "config" / "loginusers.vdf"
    if login_file.exists():
        try:
            parsed = vdf.text_loads(login_file.read_text("utf-8", "replace"))
            for id64, info in (parsed.get("users") or {}).items():
                if not isinstance(info, dict):
                    continue
                id3 = str(int(id64) - STEAM_ID64_BASE)
                label = info.get("PersonaName") or info.get("AccountName") or id3
                names[id3] = (label, info.get("MostRecent") == "1")
        except (OSError, ValueError):
            pass

    users: list[SteamUser] = []
    userdata = steam_root / "userdata"
    if userdata.is_dir():
        for entry in sorted(userdata.iterdir()):
            if not entry.is_dir() or not entry.name.isdigit() or entry.name == "0":
                continue
            known = entry.name in names
            # An account Steam has never signed in as still leaves a folder
            # behind. Naming it "Account 1719572816" made a leftover look
            # like a real choice, and it sorted ahead of the actual one.
            label, recent = names.get(entry.name, (f"Unused account {entry.name}", False))
            user = SteamUser(entry.name, label, entry, recent)
            user.known = known
            user.has_data = (entry / "config" / "localconfig.vdf").exists() or \
                            (entry / "config" / "shortcuts.vdf").exists()
            users.append(user)

    # Most recently signed in first, then accounts Steam actually knows,
    # then ones with any local data, then by name.
    users.sort(key=lambda u: (not u.most_recent, not u.known, not u.has_data, u.name.lower()))
    return users


# --------------------------------------------------------------------------
# shortcuts.vdf
# --------------------------------------------------------------------------

def read_shortcuts(user: SteamUser) -> list[dict]:
    """Existing shortcuts as a list, in file order."""
    path = user.shortcuts_file
    if not path.exists():
        return []
    try:
        parsed = vdf.binary_loads(path.read_bytes())
    except vdf.VDFError:
        return []
    entries = parsed.get("shortcuts") or parsed.get("Shortcuts") or {}
    if not isinstance(entries, dict):
        return []
    ordered = sorted(entries.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
    return [dict(v) for _, v in ordered if isinstance(v, dict)]


def backup_shortcuts(user: SteamUser) -> Path | None:
    """Copy the current file aside before touching it. Keeps the last dozen."""
    src = user.shortcuts_file
    if not src.exists():
        return None
    dest = src.with_name(f"shortcuts.vdf.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(src, dest)
    old = sorted(src.parent.glob("shortcuts.vdf.*.bak"))
    for stale in old[:-BACKUPS_KEPT]:
        try:
            stale.unlink()
        except OSError:
            pass
    return dest


def write_shortcuts(user: SteamUser, entries: list[dict], backup: bool = True) -> Path | None:
    """Rewrite shortcuts.vdf atomically. Returns the backup path if one was made."""
    user.config_dir.mkdir(parents=True, exist_ok=True)
    made = backup_shortcuts(user) if backup else None

    payload = {"shortcuts": {str(i): entry for i, entry in enumerate(entries)}}
    blob = vdf.binary_dumps(payload)

    tmp = user.shortcuts_file.with_suffix(".vdf.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, user.shortcuts_file)
    return made


def build_shortcut(
    app_name: str,
    exe: str,
    start_dir: str = "",
    launch_options: str = "",
    icon: str = "",
    tags: list[str] | None = None,
    allow_overlay: bool = True,
) -> dict:
    """Assemble one shortcut record with a correctly derived appid."""
    quoted_exe = quote_exe(exe)
    if not start_dir:
        start_dir = parent_dir(exe)
    entry = dict(SHORTCUT_DEFAULTS)
    entry["tags"] = {str(i): t for i, t in enumerate(tags or [])}
    entry.update(
        {
            "appid": signed32(shortcut_id(quoted_exe, app_name)),
            "AppName": app_name,
            "Exe": quoted_exe,
            "StartDir": quote_exe(start_dir) if start_dir else "",
            "icon": icon,
            "LaunchOptions": launch_options,
            "AllowOverlay": 1 if allow_overlay else 0,
        }
    )
    return entry


def entry_short_id(entry: dict) -> int:
    """The unsigned 32 bit id for an existing shortcut record."""
    stored = entry.get("appid")
    if isinstance(stored, int) and stored != 0:
        return unsigned32(stored)
    return shortcut_id(str(entry.get("Exe", "")), str(entry.get("AppName", "")))
