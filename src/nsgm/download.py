"""Fetch a file to disk, resuming where a previous attempt stopped.

Nothing downloaded is run from here. Installing is ``autoinstall``'s
job, and it only starts by itself when automatic installing has been
switched on; otherwise a person presses Install.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
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


def clean_file_name(name: str) -> str:
    """A file name given by someone else, made safe to create here."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = UNSAFE_CHARS.sub("_", name).strip(". ")
    return name[:150] if name and name not in (".", "..") else "download.bin"


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


def download(url: str, folder: str | Path, progress=None, timeout: int = 60,
             name: str = "", headers: dict | None = None) -> dict:
    """Stream a file to disk, resuming a partial download where possible.

    ``name`` fixes the file's name instead of taking it from the address or
    the server. File hosts hand out a different address on every visit, so
    without it an interrupted download could never be found again to
    resume, and the volumes of a split archive would not keep the names
    they need to open. A named file replaces one of the same name.
    ``headers`` are sent as well, for hosts that want their cookies back.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise DownloadError("Only http and https links can be downloaded.")

    sent = {"User-Agent": USER_AGENT}
    sent.update(headers or {})
    request = urllib.request.Request(url, headers=sent)
    partial = 0
    temp: Path | None = None
    fixed = clean_file_name(name) if name else ""

    # A .part file from an interrupted attempt can be continued.
    probe_name = fixed or safe_name(url)
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

        name = fixed or safe_name(url, response.headers.get("Content-Disposition", ""))
        total = int(response.headers.get("Content-Length") or 0) + partial

        target = resolve_target(folder, name)
        if not fixed:
            target = unique_path(target)
        if temp is None:
            temp = target.with_name(target.name + ".part")

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
# the system file manager, for when the in-app folder view is not enough
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
