"""The store's built-in BitTorrent engine: download, never share afterwards.

Through Moonlight, with only a controller, setting up Transmission or
qBittorrent - switching on its remote access, typing its password - is a
chore at best. So the store carries its own engine and needs nothing else
running: aria2, a small, long-established open-source downloader that
speaks BitTorrent (magnets, DHT, trackers, choosing files) and takes orders
over a local, password-protected connection.

* It is fetched on first use on Windows, from aria2's own GitHub release,
  and checked against a fixed SHA-256 before it is ever run. Elsewhere the
  copy from the package manager is used (``aria2c``).
* It runs hidden, only while the app does (``--stop-with-process``), and
  listens for orders on 127.0.0.1 only, with a random secret.
* **No seeding.** ``--seed-time=0`` stops each torrent the moment it is
  complete. BitTorrent sends pieces to others while downloading - that is
  how the protocol works - and that upload is held to a trickle.
* Files the release marks optional are left out: a magnet's download waits,
  paused, after its details arrive, until the store has chosen its files.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import proc, tools, torrentclient

ARIA2_ZIP = ("https://github.com/aria2/aria2/releases/download/release-1.37.0/"
             "aria2-1.37.0-win-64bit-build1.zip")
ARIA2_SHA256 = "67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288"
ARIA2_MEMBER = "aria2-1.37.0-win-64bit-build1/aria2c.exe"
UPLOAD_LIMIT = "100K"          # bytes per second sent to others while downloading
MAX_ACTIVE = 3                 # torrents downloading at once; the rest queue


class EngineError(RuntimeError):
    pass


def home() -> Path:
    path = tools.tools_dir() / "aria2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find() -> str:
    """An aria2c program to use, or "" if there is none yet."""
    fetched = home() / "aria2c.exe"
    if platform.system() == "Windows" and fetched.is_file():
        return str(fetched)
    found = shutil.which("aria2c")
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = Path(folder) / "aria2c"
        if candidate.is_file():
            return str(candidate)
    return ""


def can_fetch() -> bool:
    return platform.system() == "Windows" and platform.machine().lower() in (
        "amd64", "x86_64", "arm64", "aarch64")


def available() -> bool:
    return bool(find()) or can_fetch()


def fetch(progress=None) -> str:
    """Download aria2 for Windows, check it, and keep just the program."""
    if not can_fetch():
        raise EngineError("Install aria2 with your package manager (it provides aria2c), "
                          "or switch the store to use your own torrent app.")
    target = home() / "aria2c.exe"
    if target.is_file():
        return str(target)
    if progress:
        progress("Getting the built-in downloader (2.5 MB)")
    request = urllib.request.Request(ARIA2_ZIP, headers={"User-Agent": tools.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            blob = response.read(20 * 1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EngineError(f"The built-in downloader could not be fetched ({exc}).") from exc
    if hashlib.sha256(blob).hexdigest() != ARIA2_SHA256:
        # Never run something that is not exactly the published release.
        raise EngineError("The built-in downloader that arrived does not match the official "
                          "release, so it was not used.")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            program = archive.read(ARIA2_MEMBER)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise EngineError("The built-in downloader's archive was not as expected.") from exc
    partial = target.with_suffix(".tmp")
    partial.write_bytes(program)
    partial.replace(target)
    return str(target)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Engine:
    """One aria2 for the whole app, started the first time it is needed."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process = None
        self.client: torrentclient.Aria2 | None = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def get(self, progress=None) -> torrentclient.Aria2:
        with self.lock:
            if self.running() and self.client is not None:
                return self.client
            program = find() or fetch(progress)
            port, secret = _free_port(), secrets.token_urlsafe(24)
            folder = home()
            command = [
                program,
                "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
                f"--rpc-secret={secret}", "--rpc-max-request-size=16M",
                f"--stop-with-process={os.getpid()}",
                # Download, then stop: no seeding.
                "--seed-time=0", "--bt-detach-seed-only=true",
                f"--max-overall-upload-limit={UPLOAD_LIMIT}",
                # Choose files before downloading; leave nothing unchosen behind.
                "--pause-metadata=true", "--follow-torrent=mem",
                "--bt-remove-unselected-file=true",
                # Pick up where a download stopped, under the same names.
                "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=false",
                "--file-allocation=none", "--disk-cache=32M",
                f"--max-concurrent-downloads={MAX_ACTIVE}",
                "--enable-dht=true", f"--dht-file-path={folder / 'dht.dat'}",
                "--bt-enable-lpd=true", "--enable-peer-exchange=true",
                "--bt-tracker-connect-timeout=15", "--bt-tracker-timeout=30",
                "--quiet=true", "--summary-interval=0",
                f"--log={folder / 'aria2.log'}", "--log-level=warn",
            ]
            try:
                self.process = proc.popen(command, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, cwd=str(folder))
            except OSError as exc:
                raise EngineError(f"The built-in downloader would not start ({exc}).") from exc
            client = torrentclient.Aria2(port, secret)
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.process.poll() is not None:
                    raise EngineError("The built-in downloader stopped as soon as it started. "
                                      f"Its log is at {folder / 'aria2.log'}.")
                try:
                    client.version()
                    self.client = client
                    return client
                except torrentclient.TorrentClientError:
                    time.sleep(0.3)
            self.stop_locked()
            raise EngineError("The built-in downloader did not answer in time.")

    def stop_locked(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass
        self.process, self.client = None, None

    def stop(self) -> None:
        with self.lock:
            self.stop_locked()


ENGINE = Engine()
