"""Show the interface in a window of its own instead of a browser tab.

The interface is a local web page, which is what makes it work the same
on every system. That does not mean it has to *look* like a web page: a
native window can host it with no address bar, no tabs and no terminal
behind it.

This uses the webview already built into the operating system - WebView2
on Windows, WebKit elsewhere - rather than shipping a browser engine. If
that is unavailable the default browser is opened instead, which is the
old behaviour and always works.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

TITLE = "Non-Steam Game Manager"


def available() -> bool:
    """Whether a native window can be shown on this machine."""
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


def _hard_exit(code: int = 0) -> None:
    """Leave immediately, without waiting for anything still running."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def _open_browser(url: str) -> None:
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()


def run(url: str, serve, prefer_window: bool = True) -> None:
    """Start the server, then show the interface.

    ``serve`` is called on a background thread. The window has to own the
    main thread: every native toolkit insists on it, and starting one
    anywhere else fails in ways that differ per platform.
    """
    if not prefer_window or not available():
        _open_browser(url)
        serve()
        return

    import webview

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    try:
        webview.create_window(
            TITLE, url,
            width=1280, height=860,
            min_size=(900, 600),
            confirm_close=False,
        )
        webview.start()
        # Closing the window ends the program. The server runs on a daemon
        # thread, but a background job mid-flight can keep the process alive
        # and holding the port, which looked like the app had not quit and
        # left Task Manager as the only way out.
        _hard_exit()
    except Exception as exc:
        # A missing WebView2 runtime, no display, a locked-down desktop:
        # whatever the reason, falling back beats refusing to start.
        print(f"  Could not open a window ({exc}); using your browser instead.")
        _open_browser(url)
        try:
            worker.join()
        except KeyboardInterrupt:
            pass
