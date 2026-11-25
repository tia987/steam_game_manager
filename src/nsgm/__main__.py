"""Entry point: python -m nsgm"""

from __future__ import annotations

import argparse
import os
import socket
import sys

from . import __version__, desktop, embed, library, server, steam, store, winproc


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
    parser.add_argument("--store", action="store_true",
                        help="open straight into the full-screen store, for a controller")
    parser.add_argument("--windowed", action="store_true",
                        help="with --store, use a normal window instead of full screen")
    parser.add_argument("--streaming", action="store_true",
                        help="with --store: the store is its own Moonlight/Sunshine app, so "
                             "leaving it ends the stream")
    parser.add_argument("--version", action="version", version=f"nsgm {__version__}")
    # Run by the store as a separate process: close Steam, write the
    # shortcuts that were waiting, start Steam again.
    parser.add_argument("--steam-refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--steam-background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--relaunch-store", action="store_true", help=argparse.SUPPRESS)
    # Passed by the elevated copy to itself, so declining or failing the
    # prompt can never turn into a loop of relaunches.
    parser.add_argument("--no-elevate", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.steam_refresh:
        # No window, no server: this outlives the store and Steam both.
        return store.run_refresh(relaunch=args.relaunch_store, background=args.steam_background)

    streaming = bool(args.store and (args.streaming or store.started_by_sunshine()))
    store.STREAMING = streaming
    if args.parent_pid:
        # The elevated copy of a streamed store: it ends when its starter does.
        def gone() -> None:
            try:
                embed.MANAGER.release_all()
            finally:
                os._exit(0)
        winproc.exit_with_parent(args.parent_pid, gone)

    # Installers that ask for administrator rights can only be held inside
    # the app's window when the app has them too. With the setting on, the
    # app restarts itself with them before doing anything else; if the
    # prompt is declined it simply carries on without.
    if os.name == "nt" and not args.no_elevate and not winproc.is_admin():
        try:
            wanted = library.load_settings().get("run_as_admin")
        except Exception:
            wanted = False
        extra = ["--no-elevate"]
        if streaming:
            extra += ["--streaming", f"--parent-pid={os.getpid()}"]
        if wanted and streaming:
            # Windows' permission prompt would take over the Moonlight stream
            # and leave the store without input. Sunshine can start the store
            # with administrator rights itself, without asking: that is the
            # way when streaming (see the store's Settings).
            store.ADMIN_SKIPPED = True
        elif wanted and winproc.relaunch_elevated(extra, wait=streaming):
            return 0

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
    if args.store:
        url += "store"
        store.STORE_WINDOW = not args.browser and not args.no_browser

    if args.no_browser:
        server.serve(port, open_browser=False)
        return 0

    # A window of its own by default, the browser if asked or if no
    # webview is available on this machine.
    want_window = not args.browser and desktop.available()
    if want_window:
        print(f"  Opening a window at {url}\n")
        server.STATE["windowed"] = True
        embed.ENABLED = True
        desktop.run(url, lambda: server.serve(port, open_browser=False), True,
                    on_closing=embed.MANAGER.release_all,
                    fullscreen=args.store and not args.windowed,
                    title=desktop.STORE_TITLE if args.store else desktop.TITLE,
                    # The store must be the window the controller reaches.
                    on_shown=embed.take_foreground if args.store else None)
    else:
        server.serve(port, open_browser=True, url=url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
