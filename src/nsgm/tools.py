"""Find, or fetch, the helper program .7z and .rar archives need.

Zip and tar unpack with the standard library. 7z and RAR do not, and
game downloads are very often one of those, so without help an automatic
install would stop at the first repack.

7-Zip is looked for where it is normally installed, which is not on the
PATH: its installer does not add itself there, so a plain ``which`` misses
most copies on Windows. If there is none, the official build can be
fetched from 7-zip.org into the app's own folder. Nothing is installed:
the 7-Zip installer is itself a 7z archive, so the tiny standalone
``7zr.exe`` unpacks the two files that matter out of it, ``7z.exe`` and
``7z.dll``, and those are used in place.
"""

from __future__ import annotations

import os
import platform
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from . import proc

USER_AGENT = "NonSteamGameManager/1.0 (+tool fetch)"
SEVEN_ZR = "https://www.7-zip.org/a/7zr.exe"
SEVEN_ZIP_SETUPS = (
    "https://www.7-zip.org/a/7z2501-x64.exe",
    "https://www.7-zip.org/a/7z2409-x64.exe",
)


class ToolError(RuntimeError):
    pass


def tools_dir() -> Path:
    from .library import config_dir

    path = config_dir() / "tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seven_zip() -> str:
    """A 7-Zip (or compatible) command line program, or "" if there is none."""
    for name in ("7z", "7za", "7zz"):
        found = shutil.which(name)
        if found:
            return found
    candidates: list[Path] = []
    if platform.system() == "Windows":
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "7-Zip" / "7z.exe")
        candidates.append(tools_dir() / "7zip" / "7z.exe")
    else:
        # A program started from the desktop often has a shorter PATH than
        # a terminal, which hides Homebrew and similar locations.
        for folder in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            for name in ("7zz", "7z", "7za"):
                candidates.append(Path(folder) / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def bsdtar() -> str:
    """libarchive's tar, which reads 7z and most RAR files on Linux and macOS."""
    return shutil.which("bsdtar") or ("/usr/bin/bsdtar" if Path("/usr/bin/bsdtar").exists() else "")


def can_fetch() -> bool:
    return platform.system() == "Windows"


def _download(url: str, target: Path, timeout: int = 60) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            blob = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Could not download {url.rsplit('/', 1)[-1]} ({exc}).") from exc
    if blob[:2] != b"MZ" or len(blob) < 100_000:
        raise ToolError(f"{url.rsplit('/', 1)[-1]} did not download as a program.")
    target.write_bytes(blob)


def fetch_seven_zip(progress=None) -> str:
    """Put a working 7-Zip into the app's tools folder and return its path."""
    if not can_fetch():
        raise ToolError("Install 7-Zip (p7zip) with your package manager instead.")
    home = tools_dir() / "7zip"
    home.mkdir(parents=True, exist_ok=True)
    final = home / "7z.exe"
    if final.is_file():
        return str(final)

    if progress:
        progress("Downloading 7zr.exe from 7-zip.org")
    extractor = home / "7zr.exe"
    _download(SEVEN_ZR, extractor)

    setup = home / "7z-setup.exe"
    errors = []
    for url in SEVEN_ZIP_SETUPS:
        try:
            if progress:
                progress(f"Downloading {url.rsplit('/', 1)[-1]}")
            _download(url, setup)
            break
        except ToolError as exc:
            errors.append(str(exc))
    else:
        raise ToolError(" ".join(errors))

    if progress:
        progress("Unpacking 7z.exe and 7z.dll")
    result = proc.run([str(extractor), "x", str(setup), f"-o{home}", "7z.exe", "7z.dll", "-y"],
                      capture_output=True, text=True, timeout=300)
    for leftover in (setup, extractor):
        try:
            leftover.unlink()
        except OSError:
            pass
    if result.returncode != 0 or not final.is_file():
        raise ToolError(f"7-Zip could not be unpacked: {(result.stderr or result.stdout)[:200]}")
    return str(final)
