"""Entry point: python -m nsgm"""

from __future__ import annotations

import argparse
import socket
import sys

from . import __version__, desktop, server, steam


def free_port(preferred: int) -> int:
    for candidate in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    return preferred


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nsgm",
        description="Add non-Steam games and ROMs to Steam, artwork included.",
    )
    parser.add_argument("--port", type=int, default=8731, help="port to listen on")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open anything; just serve")
    parser.add_argument("--browser", action="store_true",
                        help="use your web browser instead of an app window")
    parser.add_argument("--version", action="version", version=f"nsgm {__version__}")
    args = parser.parse_args()

    # Clear away the copy the last update left behind. It cannot be
    # deleted while it is the program that is running, so it waits here.
    try:
        from . import updater
        updater.cleanup_previous()
    except Exception:
        pass

    print(f"\n  Non-Steam Game Manager {__version__}")
    root = steam.find_steam_root()
    if root:
        print(f"  Steam found at {root}")
    else:
        print("  Steam was not found. Set NSGM_STEAM_ROOT to its folder if it is installed.")
    if steam.steam_is_running():
        print("  Steam is running. Close it before adding games, or your changes will be lost.")

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    if args.no_browser:
        server.serve(port, open_browser=False)
        return 0

    # A window of its own by default, the browser if asked or if no
    # webview is available on this machine.
    want_window = not args.browser and desktop.available()
    if want_window:
        print(f"  Opening a window at {url}\n")
        server.STATE["windowed"] = True
        desktop.run(url, lambda: server.serve(port, open_browser=False), True)
    else:
        server.serve(port, open_browser=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
