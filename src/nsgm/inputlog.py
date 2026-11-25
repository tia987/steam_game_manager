"""A short record of what the store sees of its controllers, for when nothing works.

When the controller does nothing there is no way to reach Settings and
look, so the same details go to ``input.log`` in the app's config folder
(%APPDATA%\\NonSteamGameManager on Windows) whenever they change: whether the
store runs as administrator, whether it is the window in front, which pads
it reads directly, and whether the page receives them.
"""

from __future__ import annotations

import threading
import time

KEEP_LINES = 300


def details() -> dict:
    from . import padbridge, store
    out = {"admin": store._running_as_admin(), "streaming": store.STREAMING}
    bridge = padbridge.status()
    out.update({"in_front": bridge.get("in_front", False),
                "xinput_pads": bridge.get("xinput", 0),
                "playstation_pads": bridge.get("playstation", 0),
                "readers": ",".join(bridge.get("readers", [])) or "none",
                "page_updates": bridge.get("sent", 0),
                "error": bridge.get("error", "")})
    return out


def _line(info: dict) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return stamp + "  " + "  ".join(f"{k}={v}" for k, v in info.items())


def write(info: dict) -> None:
    from . import library
    path = library.config_dir() / "input.log"
    try:
        lines = path.read_text("utf-8").splitlines() if path.exists() else []
        lines = (lines + [_line(info)])[-KEEP_LINES:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", "utf-8")
    except OSError:
        pass


def start(every: float = 2.0) -> None:
    def run() -> None:
        last = None
        time.sleep(3)
        while True:
            try:
                info = details()
                # page_updates grows with every press; note it only once it
                # starts, not each time.
                key = dict(info, page_updates=bool(info["page_updates"]))
                if key != last:
                    last = key
                    write(info)
            except Exception:
                pass
            time.sleep(every)

    threading.Thread(target=run, name="nsgm-input-log", daemon=True).start()
