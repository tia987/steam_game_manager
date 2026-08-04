"""Entry point: python -m nsgm"""

from __future__ import annotations

import argparse
import socket
import sys

from . import __version__, server, steam


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
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--version", action="version", version=f"nsgm {__version__}")
    args = parser.parse_args()

    print(f"\n  Non-Steam Game Manager {__version__}")
    root = steam.find_steam_root()
    if root:
        print(f"  Steam found at {root}")
    else:
        print("  Steam was not found. Set NSGM_STEAM_ROOT to its folder if it is installed.")
    if steam.steam_is_running():
        print("  Steam is running. Close it before adding games, or your changes will be lost.")

    server.serve(free_port(args.port), open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
