"""Find games that are already installed but are not in Steam.

Every scanner is a function returning a list of game dicts and is
registered in ``SCANNERS``. A scanner never raises: if a launcher is not
installed, or its data files moved, it returns an empty list and records
a note. That keeps one broken launcher from taking down a whole scan.

Each game dict carries two possible ways to start the title:

``exe`` / ``args``
    The direct executable. Fastest, and playtime tracking is exact.
``launcher_exe`` / ``launcher_args``
    Through the original launcher. Slower, but required by titles that
    check in with their storefront before they will run.

The UI picks between them; ``prefer_direct`` in the settings sets the
default.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Executables that live beside games but are never the game.
JUNK_EXE = re.compile(
    r"""(?ix)
    ^( unins.*|setup|install(er)?|update(r)?|patch(er)?|repair
     | vc_?redist.*|vcredist.*|dxsetup|dxwebsetup|directx.*|dotnet.*|oalinst
     | crashp?report(er)?.*|.*crashhandler.*|bugreport.*|.*_?debug
     | easyanticheat.*|eac.*setup|battleye.*|be_?service.*|.*_be
     | quickspin|touchup|cleanup|config(uration)?|settings|benchmark
     | activation|register|readme|manual|help|support.*|.*_launcher_helper
     | notification_helper|.*subprocess.*|.*helper|python.*|ffmpeg|7z.*
     )\.exe$
    """,
)

JUNK_DIR = re.compile(
    r"(?i)^(_?redist.*|_?commonredist|directx|dotnet|vcredist|support|"
    r"engine|binaries|tools|sdk|docs?|manual|soundtrack|extras?|dlc|"
    r"__installer|installers?)$"
)


def _note(notes: list[str], text: str) -> None:
    if text not in notes:
        notes.append(text)


def make_game(
    title: str,
    source: str,
    exe: str = "",
    args: str = "",
    start_dir: str = "",
    launcher_exe: str = "",
    launcher_args: str = "",
    icon: str = "",
    tags: list[str] | None = None,
    sgdb_hint: str = "",
    detail: str = "",
) -> dict:
    """Normalise one detected game into the shape the rest of the app expects."""
    return {
        "title": title.strip(),
        "source": source,
        "exe": str(exe),
        "args": args,
        "start_dir": str(start_dir),
        "launcher_exe": str(launcher_exe),
        "launcher_args": launcher_args,
        "icon": str(icon),
        "tags": tags or [],
        "sgdb_hint": sgdb_hint or title.strip(),
        # Where this came from, so a surprising entry can be traced back
        # to the folder or registry key that produced it.
        "detail": str(detail or start_dir or exe),
    }


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _reg_iter(hive_name: str, subkey: str):
    """Yield (name, {values}) for each child of a registry key. Windows only."""
    if not IS_WINDOWS:
        return
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    try:
        root = winreg.OpenKey(hives[hive_name], subkey)
    except OSError:
        return
    with root:
        index = 0
        while True:
            try:
                child_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, child_name) as child:
                    values = {}
                    vi = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(child, vi)
                        except OSError:
                            break
                        values[name] = value
                        vi += 1
                    yield child_name, values
            except OSError:
                continue


def _reg_value(hive_name: str, subkey: str, name: str) -> str | None:
    if not IS_WINDOWS:
        return None
    import winreg

    hives = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    try:
        with winreg.OpenKey(hives[hive_name], subkey) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def _score_exe(path: Path, folder_name: str) -> int:
    """Rank candidate executables so the real game beats its helpers."""
    stem = path.stem.lower()
    target = re.sub(r"[^a-z0-9]", "", folder_name.lower())
    flat = re.sub(r"[^a-z0-9]", "", stem)
    score = 0
    if flat == target:
        score += 120
    elif target and (flat in target or target in flat):
        score += 70
    if "win64" in str(path).lower() or "x64" in stem:
        score += 15
    if stem.endswith("shipping"):
        score += 40
    if "launcher" in stem:
        score += 10
    depth = len(path.parts)
    score -= depth
    try:
        score += min(int(path.stat().st_size / (4 * 1024 * 1024)), 40)
    except OSError:
        pass
    return score


def pick_executable(folder: Path, max_depth: int = 3) -> Path | None:
    """Best guess at the main executable inside a game folder."""
    suffixes = {".exe"} if IS_WINDOWS else {".exe", ".sh", ".x86_64", ".x86", ""}
    candidates: list[Path] = []
    root_depth = len(folder.parts)
    try:
        for current, dirs, files in os.walk(folder):
            here = Path(current)
            if len(here.parts) - root_depth >= max_depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not JUNK_DIR.match(d) and not d.startswith(".")]
            for name in files:
                candidate = here / name
                if candidate.suffix.lower() not in suffixes:
                    continue
                if IS_WINDOWS and JUNK_EXE.match(name):
                    continue
                if not IS_WINDOWS and candidate.suffix == "" and not os.access(candidate, os.X_OK):
                    continue
                candidates.append(candidate)
            if len(candidates) > 400:
                break
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: _score_exe(p, folder.name))


# --------------------------------------------------------------------------
# Windows launchers
# --------------------------------------------------------------------------

def scan_epic(notes: list[str]) -> list[dict]:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic/EpicGamesLauncher/Data/Manifests"
    if not base.is_dir():
        return []

    launcher = ""
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if not root:
            continue
        for arch in ("Win64", "Win32"):
            candidate = Path(root) / f"Epic Games/Launcher/Portal/Binaries/{arch}/EpicGamesLauncher.exe"
            if candidate.exists():
                launcher = str(candidate)
                break
        if launcher:
            break

    games = []
    for manifest in base.glob("*.item"):
        try:
            data = json.loads(manifest.read_text("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            continue
        categories = {str(c).lower() for c in data.get("AppCategories", [])}
        if categories and "games" not in categories:
            continue  # plugins, engine builds, asset packs
        install = data.get("InstallLocation") or ""
        exe_rel = data.get("LaunchExecutable") or ""
        title = data.get("DisplayName") or data.get("AppName") or manifest.stem
        if not install:
            continue
        full_exe = str(Path(install) / exe_rel) if exe_rel else ""
        url = (
            "com.epicgames.launcher://apps/"
            f"{data.get('CatalogNamespace', '')}%3A"
            f"{data.get('CatalogItemId', '')}%3A"
            f"{data.get('AppName', '')}?action=launch&silent=true"
        )
        games.append(
            make_game(
                title, "epic",
                exe=full_exe, start_dir=install,
                launcher_exe=launcher or full_exe,
                launcher_args="" if not launcher else url,
                tags=["Epic Games"],
            )
        )
    if not launcher and games:
        _note(notes, "Epic launcher executable not found; using game executables directly.")
    return games


def scan_gog(notes: list[str]) -> list[dict]:
    galaxy = ""
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if root:
            candidate = Path(root) / "GOG Galaxy/GalaxyClient.exe"
            if candidate.exists():
                galaxy = str(candidate)
                break

    games = []
    seen_ids: set[str] = set()
    skipped_dlc = 0
    for hive, path in (
        ("HKLM", r"SOFTWARE\WOW6432Node\GOG.com\Games"),
        ("HKLM", r"SOFTWARE\GOG.com\Games"),
    ):
        for game_id, values in _reg_iter(hive, path):
            # On 64-bit Windows these two keys are usually the same one seen
            # through registry redirection, so every title would appear twice.
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)

            # GOG gives each DLC pack its own key, pointing at the game it
            # extends. They are not separately launchable, and listing them
            # turns one game into a shelf full of near-duplicates.
            if str(values.get("dependsOn", "")).strip():
                skipped_dlc += 1
                continue

            title = values.get("gameName") or values.get("startMenu") or game_id
            install = values.get("path") or values.get("workingDir") or ""
            if install and not Path(install).is_dir():
                continue          # left behind after an uninstall

            exe = values.get("exe") or ""
            if exe and not Path(exe).is_absolute() and install:
                exe = str(Path(install) / exe)
            if exe and not Path(exe).exists():
                exe = ""
            if not exe and install:
                found = pick_executable(Path(install))
                exe = str(found) if found else ""
            if not exe:
                continue

            games.append(
                make_game(
                    title, "gog",
                    exe=exe, start_dir=install,
                    launcher_exe=galaxy or exe,
                    launcher_args=(
                        f'/command=runGame /gameId={game_id} /path="{install}"' if galaxy else ""
                    ),
                    tags=["GOG"],
                    detail=install or exe,
                )
            )
    if skipped_dlc:
        _note(notes, f"GOG: skipped {skipped_dlc} DLC entries that belong to games already listed.")
    return games


def scan_ubisoft(notes: list[str]) -> list[dict]:
    launcher_dir = _reg_value("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher", "InstallDir") or ""
    launcher = ""
    for name in ("upc.exe", "UbisoftConnect.exe", "UbisoftGameLauncher.exe"):
        if launcher_dir and (Path(launcher_dir) / name).exists():
            launcher = str(Path(launcher_dir) / name)
            break

    titles: dict[str, str] = {}
    for key, values in _reg_iter("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"):
        match = re.match(r"(?i)Uplay Install (\d+)", key)
        if match and values.get("DisplayName"):
            titles[match.group(1)] = values["DisplayName"]
    for key, values in _reg_iter("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
        match = re.match(r"(?i)Uplay Install (\d+)", key)
        if match and values.get("DisplayName"):
            titles.setdefault(match.group(1), values["DisplayName"])

    games = []
    for game_id, values in _reg_iter("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"):
        install = values.get("InstallDir") or ""
        if not install:
            continue
        install = install.replace("/", "\\")
        title = titles.get(game_id) or Path(install.rstrip("\\")).name
        found = pick_executable(Path(install))
        games.append(
            make_game(
                title, "ubisoft",
                exe=str(found) if found else "", start_dir=install,
                launcher_exe=launcher, launcher_args=f"uplay://launch/{game_id}/0",
                tags=["Ubisoft Connect"],
            )
        )
    if games and not launcher:
        _note(notes, "Ubisoft Connect not found; these need the launcher to start.")
    return games


def scan_ea(notes: list[str]) -> list[dict]:
    games: list[dict] = []
    seen: set[str] = set()
    launcher = ""
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if not root:
            continue
        for rel in ("Electronic Arts/EA Desktop/EA Desktop/EALauncher.exe",
                    "Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe",
                    "Origin/Origin.exe"):
            candidate = Path(root) / rel
            if candidate.exists():
                launcher = str(candidate)
                break
        if launcher:
            break

    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    for manifest in (program_data / "Origin/LocalContent").rglob("*.mfst"):
        try:
            text = manifest.read_text("utf-8", "replace")
        except OSError:
            continue
        match = re.search(r"(?:^|[?&])id=([^&\s]+)", text)
        offer = match.group(1) if match else manifest.stem
        title = manifest.parent.name
        if offer in seen:
            continue
        seen.add(offer)
        install = ""
        path_match = re.search(r"(?:^|[?&])dipInstallPath=([^&]+)", text)
        if path_match:
            from urllib.parse import unquote
            install = unquote(path_match.group(1))
        exe = ""
        if install and Path(install).is_dir():
            found = pick_executable(Path(install))
            exe = str(found) if found else ""
        games.append(
            make_game(
                title, "ea",
                exe=exe, start_dir=install,
                launcher_exe=launcher,
                launcher_args=f"origin2://game/launch?offerIds={offer}",
                tags=["EA"],
            )
        )
    return games


BATTLENET_CODES = {
    "world of warcraft": "WoW", "diablo ii: resurrected": "OSI", "diablo iii": "D3",
    "diablo iv": "Fen", "overwatch": "Pro", "overwatch 2": "Pro",
    "hearthstone": "WTCG", "heroes of the storm": "Hero", "starcraft ii": "S2",
    "starcraft": "S1", "warcraft iii": "W3", "warcraft rumble": "GRY",
    "call of duty": "AUKS", "crash bandicoot 4": "Wlby",
}


def scan_battlenet(notes: list[str]) -> list[dict]:
    launcher = ""
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if root and (Path(root) / "Battle.net/Battle.net.exe").exists():
            launcher = str(Path(root) / "Battle.net/Battle.net.exe")
            break

    games: list[dict] = []
    for hive, path in (
        ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        for key, values in _reg_iter(hive, path):
            publisher = str(values.get("Publisher", "")).lower()
            title = str(values.get("DisplayName", "")).strip()
            if "blizzard" not in publisher or not title:
                continue
            if title.lower().startswith("battle.net"):
                continue
            install = str(values.get("InstallLocation", "")).strip('"')
            exe = ""
            if install and Path(install).is_dir():
                found = pick_executable(Path(install))
                exe = str(found) if found else ""
            code = ""
            for name, value in BATTLENET_CODES.items():
                if title.lower().startswith(name):
                    code = value
                    break
            games.append(
                make_game(
                    title, "battlenet",
                    exe=exe, start_dir=install,
                    launcher_exe=launcher if code else "",
                    launcher_args=f"battlenet://{code}" if code else "",
                    tags=["Battle.net"],
                )
            )
    return games


def scan_xbox(notes: list[str]) -> list[dict]:
    """Game Pass / Microsoft Store titles installed to a XboxGames folder."""
    games = []
    roots = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/XboxGames")
        if candidate.is_dir():
            roots.append(candidate)
    for root in roots:
        for game_dir in root.iterdir():
            content = game_dir / "Content"
            if not content.is_dir():
                continue
            title = game_dir.name
            config = content / "MicrosoftGame.config"
            if config.exists():
                try:
                    text = config.read_text("utf-8", "replace")
                    match = re.search(r'DefaultDisplayName="([^"]+)"', text)
                    if match:
                        title = match.group(1)
                except OSError:
                    pass
            helper = content / "gamelaunchhelper.exe"
            exe = str(helper) if helper.exists() else ""
            if not exe:
                found = pick_executable(content, max_depth=2)
                exe = str(found) if found else ""
            if not exe:
                continue
            games.append(
                make_game(title, "xbox", exe=exe, start_dir=str(content), tags=["Xbox"])
            )
    if not roots:
        _note(notes, "No XboxGames folder found. Store apps installed elsewhere are not detected.")
    return games


def scan_itch(notes: list[str]) -> list[dict]:
    if IS_WINDOWS:
        db = Path(os.environ.get("APPDATA", "")) / "itch/db/butler.db"
    elif IS_MAC:
        db = Path.home() / "Library/Application Support/itch/db/butler.db"
    else:
        db = Path.home() / ".config/itch/db/butler.db"
    if not db.exists():
        return []

    games = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT g.title AS title, c.verdict AS verdict "
            "FROM caves c JOIN games g ON g.id = c.game_id"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        _note(notes, f"itch.io database could not be read ({exc}).")
        return []

    for row in rows:
        try:
            verdict = json.loads(row["verdict"] or "{}")
        except json.JSONDecodeError:
            continue
        base = verdict.get("basePath") or ""
        candidates = verdict.get("candidates") or []
        if not base or not candidates:
            continue
        exe = str(Path(base) / candidates[0].get("path", ""))
        games.append(
            make_game(row["title"] or Path(base).name, "itch",
                      exe=exe, start_dir=base, tags=["itch.io"])
        )
    return games


def scan_installed_programs(notes: list[str]) -> list[dict]:
    """Everything in Add/Remove Programs that looks like it could be a game.

    Deliberately broad and off by default. Use the filter box to narrow it.
    """
    games: list[dict] = []
    seen: set[str] = set()
    skip_publisher = re.compile(
        r"(?i)microsoft|nvidia|amd|intel|realtek|adobe|google|mozilla|oracle|python|"
        r"apple|dell|hp inc|lenovo|logitech|corsair|razer|vmware|citrix|autodesk"
    )
    for hive, path in (
        ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        for _key, values in _reg_iter(hive, path):
            title = str(values.get("DisplayName", "")).strip()
            install = str(values.get("InstallLocation", "")).strip('"')
            if not title or not install or title.lower() in seen:
                continue
            if values.get("SystemComponent") == 1 or values.get("ParentKeyName"):
                continue
            if skip_publisher.search(str(values.get("Publisher", ""))):
                continue
            if not Path(install).is_dir():
                continue
            found = pick_executable(Path(install))
            if not found:
                continue
            seen.add(title.lower())
            games.append(
                make_game(title, "programs", exe=str(found), start_dir=install)
            )
    return games


# --------------------------------------------------------------------------
# Linux launchers
# --------------------------------------------------------------------------

def scan_legendary(notes: list[str]) -> list[dict]:
    """Epic titles installed through Legendary or Heroic."""
    games = []
    for path in (
        Path.home() / ".config/legendary/installed.json",
        Path.home() / ".config/heroic/legendaryConfig/legendary/installed.json",
    ):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            continue
        for app_name, info in (data or {}).items():
            install = info.get("install_path") or ""
            executable = info.get("executable") or ""
            if not install:
                continue
            games.append(
                make_game(
                    info.get("title") or app_name, "heroic",
                    exe=str(Path(install) / executable) if executable else "",
                    start_dir=install,
                    launcher_exe="xdg-open",
                    launcher_args=f"heroic://launch/legendary/{app_name}",
                    tags=["Epic Games", "Heroic"],
                )
            )
        break
    return games


def scan_heroic_gog(notes: list[str]) -> list[dict]:
    path = Path.home() / ".config/heroic/gog_store/installed.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8", "replace"))
    except (OSError, json.JSONDecodeError):
        return []
    games = []
    for info in data.get("installed", []):
        install = info.get("install_path") or ""
        if not install:
            continue
        title = info.get("title") or Path(install).name
        found = pick_executable(Path(install)) if Path(install).is_dir() else None
        games.append(
            make_game(
                title, "heroic",
                exe=str(found) if found else "", start_dir=install,
                launcher_exe="xdg-open",
                launcher_args=f"heroic://launch/gog/{info.get('appName', '')}",
                tags=["GOG", "Heroic"],
            )
        )
    return games


def scan_lutris(notes: list[str]) -> list[dict]:
    db = Path.home() / ".local/share/lutris/pga.db"
    if not db.exists():
        db = Path.home() / ".var/app/net.lutris.Lutris/data/lutris/pga.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, name, slug, runner, directory FROM games "
            "WHERE installed = 1 AND (hidden IS NULL OR hidden = 0)"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        _note(notes, f"Lutris database could not be read ({exc}).")
        return []
    lutris_bin = "lutris"
    games = []
    for row in rows:
        games.append(
            make_game(
                row["name"], "lutris",
                exe=lutris_bin, args=f"lutris:rungameid/{row['id']}",
                start_dir=row["directory"] or "",
                tags=["Lutris"] + ([row["runner"]] if row["runner"] else []),
            )
        )
    return games


DESKTOP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
]


def _parse_desktop(path: Path) -> dict | None:
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return None
    section = {}
    in_entry = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        section.setdefault(key.strip(), value.strip())
    return section or None


def scan_desktop_entries(notes: list[str]) -> list[dict]:
    """Anything the desktop menu files declare to be a game, Flatpaks included."""
    games = []
    seen: set[str] = set()
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for entry_file in directory.glob("*.desktop"):
            entry = _parse_desktop(entry_file)
            if not entry:
                continue
            categories = entry.get("Categories", "")
            if "Game" not in categories:
                continue
            if entry.get("NoDisplay", "").lower() == "true":
                continue
            if entry.get("Type", "Application") != "Application":
                continue
            title = entry.get("Name") or entry_file.stem
            if title.lower() in seen or "steam" in title.lower():
                continue
            exec_line = re.sub(r"%[fFuUdDnNickvm]", "", entry.get("Exec", "")).strip()
            if not exec_line:
                continue
            parts = exec_line.split(" ", 1)
            seen.add(title.lower())
            games.append(
                make_game(
                    title, "desktop",
                    exe=parts[0], args=parts[1] if len(parts) > 1 else "",
                    start_dir=entry.get("Path", ""),
                    icon=entry.get("Icon", ""),
                    tags=["Desktop"],
                )
            )
    return games


def scan_flatpak(notes: list[str]) -> list[dict]:
    try:
        result = subprocess.run(
            ["flatpak", "list", "--app", "--columns=application,name"],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    games = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        app_id, name = line.split("\t", 1)
        games.append(
            make_game(name.strip() or app_id, "flatpak",
                      exe="flatpak", args=f"run {app_id.strip()}", tags=["Flatpak"])
        )
    return games


# --------------------------------------------------------------------------
# folder scanning, any platform
# --------------------------------------------------------------------------

def scan_folder(root: str, depth: int = 1, notes: list[str] | None = None) -> list[dict]:
    """Treat each subfolder of ``root`` as one game and find its executable."""
    notes = notes if notes is not None else []
    base = Path(root).expanduser()
    if not base.is_dir():
        _note(notes, f"Folder not found: {root}")
        return []

    folders: list[Path] = []

    def collect(current: Path, level: int) -> None:
        try:
            children = [c for c in current.iterdir() if c.is_dir() and not c.name.startswith(".")]
        except OSError:
            return
        for child in children:
            if JUNK_DIR.match(child.name):
                continue
            if level < depth:
                collect(child, level + 1)
            else:
                folders.append(child)

    if depth <= 0:
        folders = [base]
    else:
        collect(base, 1)

    games = []
    for folder in sorted(folders):
        found = pick_executable(folder)
        if not found:
            continue
        games.append(
            make_game(prettify(folder.name), "folder",
                      exe=str(found), start_dir=str(folder), tags=["Folder"])
        )
    return games


def prettify(name: str) -> str:
    """Turn a folder or file name into something that looks like a title."""
    text = re.sub(r"\.(exe|sh|x86_64|app)$", "", name, flags=re.I)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*[-–]\s*(GOG|Repack|Portable|PC)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower().endswith(", the"):
        text = "The " + text[:-5]
    return text


# --------------------------------------------------------------------------
# registry of scanners
# --------------------------------------------------------------------------

SCANNERS: dict[str, dict] = {
    "epic": {"label": "Epic Games", "fn": scan_epic, "os": "Windows", "default": True},
    "gog": {"label": "GOG Galaxy", "fn": scan_gog, "os": "Windows", "default": True},
    "ubisoft": {"label": "Ubisoft Connect", "fn": scan_ubisoft, "os": "Windows", "default": True},
    "ea": {"label": "EA", "fn": scan_ea, "os": "Windows", "default": True},
    "battlenet": {"label": "Battle.net", "fn": scan_battlenet, "os": "Windows", "default": True},
    "xbox": {"label": "Xbox / Game Pass", "fn": scan_xbox, "os": "Windows", "default": True},
    "itch": {"label": "itch.io", "fn": scan_itch, "os": "any", "default": True},
    "programs": {"label": "Installed programs", "fn": scan_installed_programs,
                 "os": "Windows", "default": False},
    "heroic": {"label": "Heroic / Legendary", "fn": scan_legendary, "os": "Linux", "default": True},
    "heroic_gog": {"label": "Heroic GOG", "fn": scan_heroic_gog, "os": "Linux", "default": True},
    "lutris": {"label": "Lutris", "fn": scan_lutris, "os": "Linux", "default": True},
    "flatpak": {"label": "Flatpak", "fn": scan_flatpak, "os": "Linux", "default": False},
    "desktop": {"label": "Desktop menu games", "fn": scan_desktop_entries,
                "os": "Linux", "default": True},
}


def available_sources() -> list[dict]:
    system = platform.system()
    out = []
    for key, meta in SCANNERS.items():
        if meta["os"] not in ("any", system):
            continue
        out.append({"key": key, "label": meta["label"], "default": meta["default"]})
    return out


def run_scanners(keys: list[str]) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    games: list[dict] = []
    for key in keys:
        meta = SCANNERS.get(key)
        if not meta:
            continue
        try:
            games.extend(meta["fn"](notes))
        except Exception as exc:  # a broken launcher must not stop the scan
            _note(notes, f"{meta['label']} scan failed: {exc}")
    return games, notes
