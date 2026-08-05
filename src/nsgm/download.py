"""Fetch a file, then hand it to the person to install.

Deliberately does not run anything by itself. A downloaded installer is
opened only when the person presses Run, and then it is the installer's
own window that asks where to install and everything else — which is
what "follow the options of the installer" actually means.

Executing a freshly downloaded binary without being asked is how a lot
of machines get compromised, and it would not help here anyway, since
the installer needs to show its interface regardless.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import time

from . import proc
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
USER_AGENT = "NonSteamGameManager/1.0 (+local download helper)"

CHUNK = 256 * 1024

# Extensions worth offering a download button for.
FILE_EXTENSIONS = {
    ".exe", ".msi", ".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".bz2",
    ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".jar", ".love", ".apk",
}
INSTALLER_EXTENSIONS = {".exe", ".msi", ".msix", ".appimage", ".deb", ".rpm", ".dmg", ".pkg"}

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadError(RuntimeError):
    pass


def looks_downloadable(url: str) -> bool:
    """Whether a link points at a file rather than a web page."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in FILE_EXTENSIONS)


def is_installer(path: str | Path) -> bool:
    return Path(path).suffix.lower() in INSTALLER_EXTENSIONS


def default_folder() -> Path:
    for var in ("USERPROFILE", "HOME"):
        base = os.environ.get(var)
        if base and (Path(base) / "Downloads").is_dir():
            return Path(base) / "Downloads"
    return Path.home()


def safe_name(url: str, disposition: str = "") -> str:
    """Work out a file name that cannot escape the destination folder."""
    name = ""
    if disposition:
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        if match:
            name = urllib.parse.unquote(match.group(1))
    if not name:
        name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    name = name.replace("\\", "/").split("/")[-1]
    name = UNSAFE_CHARS.sub("_", name).strip(". ")
    if not name or name in (".", ".."):
        name = "download.bin"
    return name[:150]


def resolve_target(folder: str | Path, name: str) -> Path:
    """Place the file inside the folder, never outside it."""
    base = Path(folder).expanduser()
    if not base.is_dir():
        raise DownloadError(f"That folder does not exist: {base}")
    if not os.access(base, os.W_OK):
        raise DownloadError(f"That folder is not writable: {base}")
    target = (base / name).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise DownloadError("That file name would land outside the chosen folder.")
    return target


def unique_path(target: Path) -> Path:
    """Never silently overwrite something already downloaded."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(2, 500):
        candidate = target.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem} ({int(time.time())}){suffix}")


HTML_SIGNS = (b"<!doctype html", b"<html", b"<head", b"<?xml")


def download(url: str, folder: str | Path, progress=None, timeout: int = 60) -> dict:
    """Stream a file to disk, resuming a partial download where possible."""
    if not url.lower().startswith(("http://", "https://")):
        raise DownloadError("Only http and https links can be downloaded.")

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial = 0
    temp: Path | None = None

    # A .part file from an interrupted attempt can be continued.
    probe_name = safe_name(url)
    probe_temp = Path(folder).expanduser() / (probe_name + ".part")
    if probe_temp.exists():
        partial = probe_temp.stat().st_size
        if partial > 0:
            request.add_header("Range", f"bytes={partial}-")
            temp = probe_temp

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and temp:
            temp.unlink(missing_ok=True)
            raise DownloadError("The partial file was stale; try again.") from exc
        raise DownloadError(f"The server answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DownloadError(f"Could not reach that address ({exc}).") from exc

    with response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type in ("text/html", "application/xhtml+xml"):
            raise DownloadError(
                "That link returns a web page, not a file. Open it in a browser "
                "and copy the direct download link instead."
            )

        resuming = response.status == 206 and temp is not None
        if not resuming:
            partial = 0
            temp = None

        name = safe_name(url, response.headers.get("Content-Disposition", ""))
        total = int(response.headers.get("Content-Length") or 0) + partial

        if temp is None:
            target = unique_path(resolve_target(folder, name))
            temp = target.with_name(target.name + ".part")
        else:
            target = unique_path(resolve_target(folder, name))

        digest = hashlib.sha256()
        done = partial
        checked_head = partial > 0

        mode = "ab" if resuming else "wb"
        try:
            with open(temp, mode) as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    if not checked_head:
                        head = block[:64].lstrip().lower()
                        if any(head.startswith(sign) for sign in HTML_SIGNS):
                            raise DownloadError(
                                "That link returns a web page, not a file."
                            )
                        checked_head = True
                    handle.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
        except OSError as exc:
            raise DownloadError(f"Could not write the file ({exc}).") from exc
        except DownloadError:
            temp.unlink(missing_ok=True)
            raise

    if done == 0:
        temp.unlink(missing_ok=True)
        raise DownloadError("The download was empty.")

    temp.replace(target)
    return {
        "path": str(target),
        "name": target.name,
        "size": target.stat().st_size,
        "sha256": digest.hexdigest() if not resuming else "",
        "installer": is_installer(target),
    }


# --------------------------------------------------------------------------
# handing over to the person
# --------------------------------------------------------------------------

def reveal(path: str | Path) -> dict:
    """Open the folder containing a file in the desktop file manager."""
    target = Path(path)
    if not target.exists():
        return {"ok": False, "message": "That file is no longer there."}
    try:
        if IS_WINDOWS:
            proc.Popen(["explorer", "/select,", str(target)])
        elif platform.system() == "Darwin":
            proc.Popen(["open", "-R", str(target)])
        else:
            proc.Popen(["xdg-open", str(target.parent)])
        return {"ok": True, "message": ""}
    except OSError as exc:
        return {"ok": False, "message": f"Could not open the folder ({exc})."}


def run_installer(path: str | Path, timeout: int = 7200) -> dict:
    """Start an installer and wait for it to close.

    The installer draws its own interface. Everything about where the
    game goes and which components to include is answered there, not
    here.
    """
    target = Path(path)
    if not target.exists():
        return {"ok": False, "message": "That file is no longer there."}
    if not is_installer(target):
        return {"ok": False,
                "message": f"{target.suffix or 'That file'} is not an installer. "
                           "Open the folder and unpack it yourself."}
    if not IS_WINDOWS:
        return {"ok": False, "message": "Running installers is only supported on Windows."}

    started = time.time()
    try:
        if target.suffix.lower() == ".msi":
            completed = subprocess.run(["msiexec", "/i", str(target)], timeout=timeout)
        else:
            completed = subprocess.run([str(target)], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "The installer is still open; leaving it running."}
    except OSError as exc:
        return {"ok": False, "message": f"Could not start the installer ({exc})."}

    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "seconds": round(time.time() - started, 1),
        "message": ("The installer finished." if completed.returncode == 0
                    else f"The installer exited with code {completed.returncode}, "
                         "which usually means it was cancelled."),
    }
