"""Close the store when its Moonlight stream ends.

Sunshine only closes an app (and runs its Undo command) when Moonlight
quits that app. Leaving the stream any other way, such as with
L1 + R1 + Select + Start, is a disconnect: Sunshine keeps the app running
so the stream can be picked up again, and tells nobody but its own log.

So while the store is streamed it reads the end of Sunshine's log. Once
the log says the client went away, and it has not come back within a few
seconds (a network hiccup reconnects by itself), the store closes, which
also ends Sunshine's session so the Steam app can be started next. What
happens to games still downloading is a setting:

    close    close straight away; downloads pick up where they stopped next time
    finish   finish what is downloading and installing first, then close
    stay     keep running until the store is left by hand
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

GONE = "CLIENT DISCONNECTED"
BACK = "CLIENT CONNECTED"
GRACE = 10.0          # seconds a client has to come back before it counts as gone
POLL = 1.0
CHOICES = ("close", "finish", "stay")
DEFAULT = "finish"


def log_candidates() -> list[Path]:
    """Where Sunshine (and Apollo, a Sunshine fork) keep their log, most likely first."""
    out = []
    for var in ("SUNSHINE_LOG",):
        if os.environ.get(var):
            out.append(Path(os.environ[var]))
    for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                              os.environ.get("ProgramFiles(x86)"), r"C:\Program Files")):
        for app in ("Sunshine", "Apollo"):
            out.append(Path(base) / app / "config" / "sunshine.log")
    home = Path.home()
    out += [home / ".config/sunshine/sunshine.log",
            home / ".var/app/dev.lizardbyte.app.Sunshine/config/sunshine/sunshine.log",
            home / "Library/Application Support/Sunshine/sunshine.log"]
    seen, unique = set(), []
    for path in out:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_log(chosen: str = "") -> Path | None:
    if chosen:
        path = Path(chosen)
        return path if path.is_file() else None
    found = [p for p in log_candidates() if p.is_file()]
    if not found:
        return None
    # The one written to most recently is the Sunshine that is streaming.
    return max(found, key=lambda p: p.stat().st_mtime)


class LogTail:
    """New lines of a log file, from where it ended when this started.

    Sunshine starts its log afresh when it restarts: a file that got
    shorter is read again from the top.
    """

    def __init__(self, path: Path):
        self.path = path
        self.position = self._size()
        self.partial = b""

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def lines(self) -> list[str]:
        size = self._size()
        if size < self.position:
            self.position, self.partial = 0, b""
        if size == self.position:
            return []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.position)
                chunk = handle.read(min(size - self.position, 4 * 1024 * 1024))
        except OSError:
            return []
        self.position += len(chunk)
        chunk = self.partial + chunk
        *whole, self.partial = chunk.split(b"\n")
        return [line.decode("utf-8", "replace").rstrip("\r") for line in whole]


class Watcher:
    """Reads the log and decides; ``close`` is called when the store should go.

    ``busy`` says whether downloads or installs are still running, and
    ``choice`` gives the setting each time it is needed, so a change in
    Settings applies at once.
    """

    def __init__(self, tail: LogTail, close, busy, choice, clock=time.time):
        self.tail = tail
        self.close = close
        self.busy = busy
        self.choice = choice
        self.clock = clock
        self.gone_at = 0.0
        self.closed = False

    def state(self) -> dict:
        return {"log": str(self.tail.path), "disconnected": bool(self.gone_at),
                "since": self.gone_at}

    def step(self) -> bool:
        """Read what is new and act on it. True once the store has been closed."""
        for line in self.tail.lines():
            upper = line.upper()
            if GONE in upper:
                self.gone_at = self.gone_at or self.clock()
            elif BACK in upper:
                self.gone_at = 0.0
        if self.closed or not self.gone_at or self.clock() - self.gone_at < GRACE:
            return self.closed
        choice = self.choice()
        if choice == "stay":
            return False
        if choice == "finish" and self.busy():
            return False
        self.closed = True
        self.close()
        return True


WATCHER: Watcher | None = None


def start(close, busy, choice, chosen_log=lambda: "") -> threading.Thread:
    """Read Sunshine's log in the background.

    If the log is not there yet (Sunshine installed somewhere unusual, or
    the setting names a file that does not exist yet) it keeps looking.
    """
    def run() -> None:
        global WATCHER
        while WATCHER is None:
            path = find_log(chosen_log())
            if path is not None:
                WATCHER = Watcher(LogTail(path), close, busy, choice)
                break
            time.sleep(30)
        while not WATCHER.closed:
            try:
                WATCHER.step()
            except Exception:
                pass
            time.sleep(POLL)

    thread = threading.Thread(target=run, name="nsgm-stream-watch", daemon=True)
    thread.start()
    return thread


def status() -> dict:
    return WATCHER.state() if WATCHER else {}
