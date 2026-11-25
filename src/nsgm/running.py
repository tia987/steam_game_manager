"""Copies of the app that are running, and closing one from outside.

Each running copy leaves a small note in the config folder: its process,
its port and the token its web page uses. ``nsgm --quit-store`` reads
those notes and asks each running store to close the same way its own
Leave button does, so Sunshine (an app's Undo command, or Apollo's
"on disconnect" command) or any script can end it.

If no note is found (an older copy, or one whose note was lost) the usual
ports are tried: the store's page carries its token, and only a program on
this computer can read it.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PORTS = range(8731, 8751)


def _folder() -> Path:
    from . import library
    return library.config_dir() / "running"


def _note(pid: int | None = None) -> Path:
    return _folder() / f"{pid or os.getpid()}.json"


def announce(port: int, token: str, mode: str) -> None:
    """Leave the note for this copy. ``mode`` is "store" or "manager"."""
    try:
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        _note().write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "token": token, "mode": mode,
             "started": time.time()}), "utf-8")
    except OSError:
        pass


def withdraw() -> None:
    try:
        _note().unlink()
    except OSError:
        pass


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def notes() -> list[dict]:
    """The notes of copies that still answer; the rest are tidied away."""
    out = []
    try:
        files = sorted(_folder().glob("*.json"))
    except OSError:
        return out
    for file in files:
        try:
            note = json.loads(file.read_text("utf-8"))
            port = int(note["port"])
        except (OSError, ValueError, KeyError, TypeError):
            _remove(file)
            continue
        if _listening(port):
            out.append(note)
        else:
            _remove(file)
    return out


def _remove(file: Path) -> None:
    try:
        file.unlink()
    except OSError:
        pass


def _page_details(port: int) -> dict | None:
    """Port, token and mode read from a running copy's own page, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/store", timeout=2) as reply:
            page = reply.read(200_000).decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None
    token = re.search(r'<meta name="nsgm-token" content="([^"]+)"', page)
    if not token:
        return None
    mode = re.search(r'<meta name="nsgm-mode" content="([^"]+)"', page)
    return {"port": port, "token": token.group(1), "mode": mode.group(1) if mode else "",
            "pid": 0}


def find(store_only: bool = True) -> list[dict]:
    found = notes()
    if not found:
        found = [d for d in (_page_details(p) for p in PORTS if _listening(p)) if d]
    if store_only:
        # A copy too old to say which it is is taken to be a store.
        found = [d for d in found if d.get("mode") in ("store", "")]
    return found


def _ask_to_quit(note: dict) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{note['port']}/api/quit", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "X-NSGM-Token": note["token"]})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return 200 <= reply.status < 300
    except urllib.error.HTTPError:
        return False
    except (OSError, urllib.error.URLError):
        # Connection dropped as it closed: that is a yes.
        return not _listening(int(note["port"]))


def _force(pid: int) -> bool:
    """The last resort for a copy that does not answer. Its installers are left alone."""
    if not pid or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            done = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return done.returncode == 0
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def _say(text: str) -> None:
    # A windowed build has no console to print to; started from Sunshine
    # nobody reads it anyway.
    try:
        print(text)
    except (AttributeError, OSError, ValueError):
        pass


def quit_store(wait: float = 10.0, store_only: bool = True) -> int:
    """Close every running store. 0 when none is left running, 1 otherwise."""
    targets = find(store_only)
    if not targets:
        _say("  No store is running.")
        return 0
    for note in targets:
        _ask_to_quit(note)
    left = targets
    deadline = time.time() + wait
    while left and time.time() < deadline:
        time.sleep(0.25)
        left = [n for n in left if _listening(int(n["port"]))]
    for note in left:
        _force(int(note.get("pid") or 0))
    time.sleep(0.5 if left else 0)
    left = [n for n in left if _listening(int(n["port"]))]
    for note in targets:
        if note not in left and note.get("pid"):
            _remove(_note(int(note["pid"])))
    if left:
        _say("  The store did not close. If it runs as administrator, run this as administrator too.")
        return 1
    _say(f"  Closed {len(targets)} store window{'s' if len(targets) != 1 else ''}.")
    return 0
